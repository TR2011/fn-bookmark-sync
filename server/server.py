#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fn收藏夹同步 · 飞牛 OS 服务端（v1.4.0）

零第三方依赖，仅使用 Python 标准库（http.server / sqlite3 / hashlib）。

v1.1.0 起：取消随机访问令牌，改为用户名 + 密码；多用户按 user_id 隔离。
v1.2.0 起：
  * 书签自动去重：同一用户下按规范化网址唯一存储，跨设备重复只保留一条；
  * 管理端可对书签增删改查（含批量删除、回收站恢复）；
  * 「连接浏览器插件」地址由服务端按当前访问方式自动推算，界面只读展示 +
    一键复制配置，不再需要填写反代路径 / 备用地址。

三条访问通道（同一进程、同一份数据）：
    TCP 8765            浏览器插件 / 局域网直连
    Unix Socket app.sock 飞牛统一网关，内外网复用 NAS 域名
    TCP 8765 + 反代前缀  用户自建反向代理，插件用统一登录地址 + 自定义路径

反向代理两种写法都支持：
    A 保留前缀转发 → 零配置自动识别 /eb-api/api/... 或设置 EB_PROXY_PREFIXES
    B 剥离前缀转发 → 反代追加 X-Forwarded-Prefix: /eb-api

启动示例：
    python3 server.py --port 8765 --data ./data --config ./config.json --web ./web \
        --gateway-socket /var/apps/edge-bookmarks/target/app.sock
"""

import argparse
import base64
import csv
import hashlib
import hmac
import html
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.4.0"
APP_NAME = "edge-bookmarks"       # 内部标识 / 安装目录名，保持不变以兼容升级
APP_TITLE = "Fn收藏夹同步"          # 显示名称
AUTH_MODE = "username-password"
# v1.0.x 的令牌接口：旧插件/旧书签访问时给出明确升级提示，而不是笼统的 401
LEGACY_TOKEN_PATHS = {
    "/api/v1/token", "/api/v1/token/check", "/api/v1/token/reset",
    "/api/v1/setup/claim",
}
LEGACY_TOKEN_MESSAGE = ("该接口已在 v1.1.0 移除：本版本改用「用户名 + 密码」登录，"
                        "请把浏览器插件升级到 1.4.0 并在设置页填写账号密码")
MAX_BODY = 32 * 1024 * 1024       # 单次同步最大 32MB
KEEP_SNAPSHOTS = 10               # 每台设备保留的历史快照数
SESSION_DAYS = 30                 # 登录会话有效期
PBKDF2_ROUNDS = 120000            # 密码哈希迭代次数
USERNAME_RE = re.compile(r"^[\w.@-]{2,32}$", re.UNICODE)
MAX_URL_LEN = 2000

# 统一网关前缀，与 app/ui/config 的 gatewayPrefix、cmd/main 的
# EB_GATEWAY_PREFIX 保持一致；端口直连时请求不带此前缀。
GATEWAY_PREFIX = os.environ.get("EB_GATEWAY_PREFIX", "/app/" + APP_NAME).rstrip("/") or ("/app/" + APP_NAME)

# 自建反向代理的外部路径前缀（逗号分隔）。插件可以直接填
# https://<统一登录地址>:<端口>/eb-api —— 与桌面同一个地址，不必再记 IP:8765。
PREFIX_RE = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")

# 去重时忽略的跟踪参数（不影响页面本身）
TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "spm", "_ga", "_gl", "msclkid"}


class ApiError(Exception):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def safe_prefix(raw, max_len=128):
    """校验外部路径前缀。X-Forwarded-Prefix 会写进 HTML 的 <base href>，
    必须严格过滤，否则可构造 `"/><script>…` 注入。"""
    if not raw or len(raw) > max_len:
        return ""
    p = str(raw).strip().rstrip("/")
    if not p or p == "/" or ".." in p or not PREFIX_RE.fullmatch(p):
        return ""
    return p


def configured_prefixes():
    """所有已知外部前缀：统一网关 + 自建反代（EB_PROXY_PREFIXES）。"""
    out = [GATEWAY_PREFIX]
    for item in os.environ.get("EB_PROXY_PREFIXES", "").split(","):
        p = safe_prefix(item)
        if p and p not in out:
            out.append(p)
    return out


STATIC_SUFFIXES = (".css", ".js", ".html", ".png", ".svg", ".ico", ".json", ".map")
RESERVED_FIRST = {"api", "app", "bin", "web", "ui", "static", "assets"}


def auto_prefix(raw):
    """反代 A 零配置兜底：把 /eb-api/api/v1/... 或 /eb-api/app.js 的
    第一段识别为外部前缀，无需 EB_PROXY_PREFIXES 或 X-Forwarded-Prefix。"""
    parts = [p for p in raw.split("/") if p]
    if not parts or parts[0].lower() in RESERVED_FIRST:
        return "", raw
    if len(parts) == 1:
        if parts[0].endswith(STATIC_SUFFIXES):
            return "", raw
        prefix = "/" + parts[0]
        return (prefix, "/") if safe_prefix(prefix) else ("", raw)
    rest = "/" + "/".join(parts[1:])
    if rest.startswith("/api/") or rest.endswith(STATIC_SUFFIXES):
        prefix = "/" + parts[0]
        return (prefix, rest) if safe_prefix(prefix) else ("", raw)
    return "", raw


# fnOS 统一网关要求应用监听 ${TRIM_APPDEST}/<gatewaySocket> 这个 Unix Socket。
# Windows 上的 Python 可能没有 AF_UNIX：此时自动降级为仅 TCP，并可用
# --gateway-test-port 起一个标记为「网关通道」的 TCP 端口供本地回归测试。
HAS_AF_UNIX = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "ThreadingUnixStreamServer")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg):
    print("[%s] %s" % (now_iso(), msg), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 网址规范化与自动去重
# ---------------------------------------------------------------------------

def normalize_url(raw):
    """把网址规范化为去重用的唯一字符串。

    忽略：协议差异(http/https)、主机名大小写、默认端口、末尾斜杠、#锚点、
    utm_* 等跟踪参数、查询参数顺序。保留：路径、有意义的查询参数、非默认端口。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s):
        s = "http://" + s
    try:
        p = urllib.parse.urlsplit(s)
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError:
        return s.lower()
    if not host:
        return s.lower()
    netloc = "[%s]" % host if ":" in host and not host.startswith("[") else host
    if port and not ((p.scheme.lower() == "http" and port == 80) or
                     (p.scheme.lower() == "https" and port == 443)):
        netloc = "%s:%d" % (netloc, port)
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    path = re.sub(r"/{2,}", "/", path)
    pairs = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False):
        lk = k.lower()
        if lk in TRACKING_PARAMS or lk.startswith("utm_"):
            continue
        pairs.append((k, v))
    pairs.sort()
    query = urllib.parse.urlencode(pairs)
    if p.scheme.lower() in ("http", "https"):
        return netloc + path + (("?" + query) if query else "")
    return p.scheme.lower() + "://" + netloc + path + (("?" + query) if query else "")


def url_key(raw):
    return hashlib.sha1(normalize_url(raw).encode("utf-8")).hexdigest()


def valid_bookmark_url(raw):
    s = str(raw or "").strip()
    if not s or len(s) > MAX_URL_LEN or re.search(r"[\s\x00-\x1f]", s):
        return False
    # 显式写了 javascript: / data: / file: 等协议时直接拒绝，
    # 避免被补全成 http://javascript:... 而误判为合法主机。
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", s)
    if m and m.group(1).lower() not in ("http", "https", "ftp"):
        return False
    try:
        p = urllib.parse.urlsplit(s if "://" in s else "http://" + s)
        return p.scheme.lower() in ("http", "https", "ftp") and bool(p.hostname)
    except ValueError:
        return False


def normalize_path(value):
    """接受数组或「A / B」形式的字符串，输出安全的目录路径数组。"""
    if isinstance(value, str):
        parts = [x.strip() for x in value.split("/")]
    elif isinstance(value, list):
        parts = [str(x).strip() for x in value]
    else:
        parts = []
    return [p[:120] for p in parts if p][:12]


def clean_title(value, fallback=""):
    s = str(value or "").strip()[:300]
    return s or fallback


# ---------------------------------------------------------------------------
# 插件地址自动推算（v1.2.0：界面不再需要任何地址输入框）
# ---------------------------------------------------------------------------

def safe_host(raw):
    """Host / X-Forwarded-Host 来自请求头，展示前必须严格过滤。"""
    v = str(raw or "").split(",")[0].strip()
    if not v or len(v) > 255 or not HOST_RE.fullmatch(v):
        return ""
    return v


def host_and_port(host):
    if host.startswith("["):
        i = host.find("]")
        if i > 0:
            rest = host[i + 1:]
            return host[:i + 1], (rest[1:] if rest.startswith(":") else "")
    if ":" in host:
        name, _, port = host.rpartition(":")
        if port.isdigit():
            return name, port
    return host, ""


def external_origin(headers, gateway=False):
    """返回 (外部 origin, 主机名, 端口)。优先信任反代转发头。"""
    proto = str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https" if gateway else "http"
    host = safe_host(headers.get("X-Forwarded-Host") or headers.get("Host") or "")
    if not host:
        return "", "", ""
    name, port = host_and_port(host)
    return proto + "://" + host, name, port


def plugin_addresses(app, base, headers, gateway=False):
    """根据当前请求的访问方式，列出插件可用的全部地址。

    v1.2.1 修正：过去在「飞牛统一网关 /app/...」下打开管理页时，会把
    文档推荐的 /eb-api 猜测路径当成主地址显示；但多数 NAS 根本没有配置
    这个反代（实测返回 404），用户看到的地址是错的、插件必然连不上。

    现在的规则：
    1. 主地址恒为「当前管理页的真实地址」——用户地址栏里看到什么，
       连接界面就显示什么；不再凭空捏造任何路径。
    2. 统一网关 /app/... 也作为候选地址：插件会带上浏览器里已登录的
       飞牛会话 Cookie 去探测，可用即走同一域名；网关拒绝时探测失败，
       自动降级到局域网直连，不会卡住。
    3. /eb-api 之类的反代路径，只有「当前请求确实经由该前缀」或管理员
       显式配置 EB_PROXY_PREFIXES 时才下发，绝不作为猜测值。
    """
    port = int(app.get("port", 8765))
    origin, hostname, hostport = external_origin(headers, gateway)
    lan = lan_addresses(port)
    direct_host = "http://%s:%d" % (hostname, port) if hostname else ""
    proxies = [p for p in configured_prefixes() if p != GATEWAY_PREFIX]

    page_prefix = (base or "").rstrip("/")
    if page_prefix in ("/", ""):
        page_prefix = ""
    fwd = safe_prefix(headers.get("X-Forwarded-Prefix", ""))
    current_proxy = page_prefix or fwd

    # 当前管理页完整地址：浏览器地址栏里的那一个（去掉尾斜杠）
    page_url = (origin + current_proxy) if (origin and current_proxy) else origin
    is_gateway_page = (gateway or page_prefix == GATEWAY_PREFIX or
                       fwd == GATEWAY_PREFIX)
    is_direct_page = bool(origin) and not current_proxy and hostport == str(port)

    if page_url:
        primary = page_url
        if is_gateway_page:
            primary_label = "当前管理页地址 · 飞牛统一网关"
        elif current_proxy:
            primary_label = "当前管理页地址 · 反向代理路径 " + current_proxy
        elif is_direct_page:
            primary_label = "当前管理页地址 · 直连端口 %d" % port
        else:
            primary_label = "推荐地址"
    elif lan:
        primary, primary_label = lan[0], "局域网直连"
    elif direct_host:
        primary, primary_label = direct_host, "当前 NAS 主机 · 直连端口 %d" % port
    else:
        primary, primary_label = origin, "当前地址"

    # 候选池：插件会逐个探测 /api/v1/health，任一返回 JSON 即用
    ordered = []
    labels = {}

    def add(url, label):
        u = str(url or "").rstrip("/")
        if not u or u in ordered:
            return
        ordered.append(u)
        labels[u] = label

    add(primary, primary_label)
    for u in lan:
        add(u, "局域网直连 · 端口 %d（最稳定，无需 NAS 会话）" % port)
    if direct_host:
        add(direct_host, "当前 NAS 主机 · 直连端口 %d" % port)
    # 只有明确配置过的反代路径才会进入候选，不再自动生成 /eb-api
    for p in proxies:
        if origin:
            add(origin + p, "已配置反代路径 " + p)
    if is_gateway_page and origin:
        add(origin + GATEWAY_PREFIX, "飞牛统一网关入口（复用浏览器登录态）")

    alt = ""
    for u in ordered:
        if u != primary:
            alt = u
            break

    addresses = [{"url": u, "label": labels.get(u, "备用地址"),
                  "primary": u == primary, "alternate": u == alt}
                 for u in ordered]
    return {"origin": origin, "primary": primary, "alternate": alt,
            "urls": ordered, "addresses": addresses, "directPort": port,
            "pageBase": base, "pageUrl": page_url,
            "gatewayUrl": (origin + GATEWAY_PREFIX) if origin else "",
            "gatewayPrefix": GATEWAY_PREFIX, "proxyPrefixes": proxies,
            "isGatewayPage": bool(is_gateway_page),
            "channel": "gateway" if gateway else "direct"}


def plugin_config(info, username=""):
    """生成可直接粘贴进插件设置页的 JSON 配置（不含密码）。"""
    cfg = {"serverUrl": info["primary"]}
    if info["alternate"]:
        cfg["serverUrlAlt"] = info["alternate"]
    extra = [u for u in info["urls"] if u not in (info["primary"], info["alternate"])]
    if extra:
        cfg["serverUrls"] = extra
    if username:
        cfg["username"] = username
    return {"config": cfg,
            "configText": json.dumps(cfg, ensure_ascii=False, indent=2)}


# ---------------------------------------------------------------------------
# 配置（不再包含访问令牌）
# ---------------------------------------------------------------------------

class Config:
    """config.json 的读写。只保存运行参数；账号与会话全部在 SQLite 里。"""

    def __init__(self, path):
        self.path = Path(path)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SystemExit("配置文件解析失败 %s: %s" % (self.path, exc))
        else:
            self.data = {"version": VERSION, "created_at": now_iso(), "port": 8765}
            self.save()
            log("已生成配置文件：%s" % self.path)
        self.data["version"] = VERSION
        self.data.setdefault("created_at", now_iso())
        self.data.setdefault("port", 8765)
        for legacy in ("token", "claimed", "claimed_at", "claimed_from", "reset_at"):
            self.data.pop(legacy, None)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    @property
    def port(self):
        return int(self.data.get("port", 8765))


def is_private_ip(addr):
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def lan_addresses(port):
    """枚举本机局域网 IPv4，供 Web 界面给出可直接填进插件的地址。"""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not is_private_ip(ip) or ip.startswith("127."):
                continue
            u = "http://%s:%d" % (ip, port)
            if u not in found:
                found.append(u)
    except Exception:
        pass
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
            if is_private_ip(ip):
                found.append("http://%s:%d" % (ip, port))
        except Exception:
            pass
    return found


# ---------------------------------------------------------------------------
# 账号与密码
# ---------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        algo, rounds, salt_hex, hash_hex = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def valid_username(value):
    return bool(USERNAME_RE.fullmatch(str(value or "").strip()))


def valid_password(value):
    return isinstance(value, str) and 6 <= len(value) <= 128


def public_user(row):
    """对外输出的用户信息，绝不包含 password_hash。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"] or "",
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"] or "",
        "lastLogin": row["last_login"] or "",
    }


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def flatten_tree(tree):
    """把 chrome.bookmarks.getTree() 的嵌套结构拍平，保留文件夹路径。"""
    result = []

    def walk(nodes, path, depth):
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "")
            url = node.get("url")
            if url:
                result.append({
                    "node_id": str(node.get("id") or ""),
                    "title": title,
                    "url": str(url),
                    "path": list(path),
                    "date_added": int(node.get("dateAdded") or 0),
                })
            else:
                children = node.get("children") or []
                next_path = path + [title] if title and depth > 0 else list(path)
                walk(children, next_path, depth + 1)

    walk(tree, [], 0)
    return result


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    last_login    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT,
    expires_at INTEGER NOT NULL,
    source     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS devices (
    user_id        INTEGER NOT NULL,
    client_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    browser        TEXT,
    last_sync      TEXT,
    bookmark_count INTEGER DEFAULT 0,
    revision       INTEGER DEFAULT 0,
    created_at     TEXT,
    PRIMARY KEY (user_id, client_id)
);
CREATE TABLE IF NOT EXISTS bookmarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    client_id   TEXT NOT NULL,
    node_id     TEXT,
    title       TEXT,
    url         TEXT,
    path        TEXT,
    date_added  INTEGER DEFAULT 0,
    UNIQUE(user_id, client_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_bm_user ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_bm_url ON bookmarks(url);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    client_id  TEXT NOT NULL,
    created_at TEXT,
    count      INTEGER,
    tree       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_user ON snapshots(user_id);
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT,
    level      TEXT,
    user_id    INTEGER NOT NULL DEFAULT 0,
    client_id  TEXT,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_user ON logs(user_id);
"""

# v1.2.0：书签表改为「每个用户每个规范化网址唯一」，来源单独记到
# bookmark_sources，删除过的网址记到 tombstones（同步时跳过，可恢复）。
# 唯一索引必须在回填 url_key、合并重复行之后创建，因此不放在这里。
SCHEMA_V3_BASE = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    last_login    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT,
    expires_at INTEGER NOT NULL,
    source     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS devices (
    user_id        INTEGER NOT NULL,
    client_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    browser        TEXT,
    last_sync      TEXT,
    bookmark_count INTEGER DEFAULT 0,
    revision       INTEGER DEFAULT 0,
    created_at     TEXT,
    PRIMARY KEY (user_id, client_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    client_id  TEXT NOT NULL,
    created_at TEXT,
    count      INTEGER,
    tree       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_user ON snapshots(user_id);
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT,
    level      TEXT,
    user_id    INTEGER NOT NULL DEFAULT 0,
    client_id  TEXT,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_user ON logs(user_id);
"""

SCHEMA_V3_BOOKMARKS = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    client_id   TEXT NOT NULL DEFAULT 'manual',
    node_id     TEXT,
    title       TEXT,
    url         TEXT,
    path        TEXT,
    date_added  INTEGER DEFAULT 0,
    url_key     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'sync',
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bm_user ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_bm_url ON bookmarks(url);
CREATE TABLE IF NOT EXISTS bookmark_sources (
    user_id    INTEGER NOT NULL,
    url_key    TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    title      TEXT,
    path       TEXT,
    date_added INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, url_key, client_id)
);
CREATE INDEX IF NOT EXISTS idx_bms_user ON bookmark_sources(user_id);
CREATE TABLE IF NOT EXISTS tombstones (
    user_id    INTEGER NOT NULL,
    url_key    TEXT NOT NULL,
    url        TEXT,
    title      TEXT,
    deleted_at TEXT,
    PRIMARY KEY (user_id, url_key)
);
"""

BM_SELECT = """
    SELECT b.*, d.name AS device_name,
           (SELECT COUNT(*) FROM bookmark_sources s
             WHERE s.user_id=b.user_id AND s.url_key=b.url_key) AS source_count
    FROM bookmarks b
    LEFT JOIN devices d ON d.user_id=b.user_id AND d.client_id=b.client_id
"""


class Store:
    """SQLite 存储：账号、会话、按用户隔离的设备/书签/快照/日志。"""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = self.dir / "bookmarks.db"
        self.lock = threading.RLock()
        self.init_schema()

    def connect(self):
        conn = sqlite3.connect(str(self.db), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self):
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= 3:
                return
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" not in tables:
                if "devices" in tables:
                    self._migrate_v1(conn)
                else:
                    conn.executescript(SCHEMA_V3_BASE + SCHEMA_V3_BOOKMARKS)
            else:
                conn.executescript(SCHEMA_V3_BASE)
            self._migrate_v3(conn)
            conn.execute("PRAGMA user_version=3")
            log("SQLite 数据库结构：v3（多用户隔离 + 网址自动去重）")

    @staticmethod
    def _migrate_v1(conn):
        """把 v1.0.x 的单用户数据平滑迁移到 v2：历史数据先归到 user_id=0，
        注册首个管理员时再自动归属给他，不会丢书签。"""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS devices_v2 (
                user_id INTEGER NOT NULL, client_id TEXT NOT NULL,
                name TEXT NOT NULL, browser TEXT, last_sync TEXT,
                bookmark_count INTEGER DEFAULT 0, revision INTEGER DEFAULT 0,
                created_at TEXT, PRIMARY KEY (user_id, client_id));
            INSERT OR IGNORE INTO devices_v2
                SELECT 0, client_id, name, browser, last_sync,
                       bookmark_count, revision, created_at FROM devices;
            DROP TABLE devices;
            ALTER TABLE devices_v2 RENAME TO devices;

            CREATE TABLE IF NOT EXISTS bookmarks_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL, node_id TEXT, title TEXT, url TEXT,
                path TEXT, date_added INTEGER DEFAULT 0,
                UNIQUE(user_id, client_id, node_id));
            INSERT OR IGNORE INTO bookmarks_v2
                (id,user_id,client_id,node_id,title,url,path,date_added)
                SELECT id,0,client_id,node_id,title,url,path,date_added FROM bookmarks;
            DROP TABLE bookmarks;
            ALTER TABLE bookmarks_v2 RENAME TO bookmarks;
            CREATE INDEX IF NOT EXISTS idx_bm_user ON bookmarks(user_id);
            CREATE INDEX IF NOT EXISTS idx_bm_url ON bookmarks(url);
        """)
        for table in ("snapshots", "logs"):
            names = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            if "user_id" not in names:
                conn.execute("ALTER TABLE %s ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0" % table)
        conn.executescript(SCHEMA_V2)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_user ON snapshots(user_id)")

    def _migrate_v3(self, conn):
        """v2 → v3：重建书签表、回填去重键、合并重复项、建立来源与回收站。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bookmarks)")}
        if "url_key" not in cols or "source" not in cols:
            conn.executescript("""
                CREATE TABLE bookmarks_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL, client_id TEXT NOT NULL DEFAULT 'manual',
                    node_id TEXT, title TEXT, url TEXT, path TEXT,
                    date_added INTEGER DEFAULT 0, url_key TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'sync', created_at TEXT, updated_at TEXT);
                INSERT INTO bookmarks_v3
                    (id,user_id,client_id,node_id,title,url,path,date_added)
                    SELECT id,user_id,client_id,node_id,title,url,path,date_added
                    FROM bookmarks;
                DROP TABLE bookmarks;
                ALTER TABLE bookmarks_v3 RENAME TO bookmarks;
            """)
        conn.executescript(SCHEMA_V3_BOOKMARKS)

        rows = conn.execute(
            "SELECT id,user_id,client_id,url,title,path,date_added FROM bookmarks ORDER BY id"
        ).fetchall()
        groups = {}
        ts = now_iso()
        for r in rows:
            key = url_key(r["url"] or "")
            conn.execute("UPDATE bookmarks SET url_key=?, "
                         "created_at=COALESCE(created_at,?), updated_at=COALESCE(updated_at,?) "
                         "WHERE id=?", (key, ts, ts, r["id"]))
            conn.execute("INSERT OR IGNORE INTO bookmark_sources"
                         "(user_id,url_key,client_id,title,path,date_added) VALUES(?,?,?,?,?,?)",
                         (r["user_id"], key, r["client_id"], r["title"],
                          r["path"], r["date_added"] or 0))
            groups.setdefault((r["user_id"], key), []).append(r["id"])
        removed = 0
        for ids in groups.values():
            for extra in ids[1:]:
                conn.execute("DELETE FROM bookmarks WHERE id=?", (extra,))
                removed += 1
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_bm_user_key "
                     "ON bookmarks(user_id,url_key)")
        if removed:
            log("升级 v3：合并历史重复书签 %d 条" % removed)

    # ---- 用户 ----

    def user_count(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def find_user(self, username):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                               (str(username or "").strip(),)).fetchone()
        return dict(row) if row else None

    def get_user(self, uid):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with self.connect() as conn:
            return [public_user(r) for r in conn.execute(
                "SELECT * FROM users ORDER BY id")]

    def create_user(self, username, password, role="user", display_name=""):
        username = str(username or "").strip()
        display_name = str(display_name or "").strip()[:64]
        if not valid_username(username):
            raise ValueError("用户名需为 2–32 位字母、数字、中文或 _ . - @")
        if not valid_password(password):
            raise ValueError("密码长度需为 6–128 位")
        if role not in ("admin", "user"):
            raise ValueError("角色只能是 admin 或 user")
        with self.lock, self.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE",
                            (username,)).fetchone():
                raise ValueError("用户名已存在")
            cur = conn.execute("""
                INSERT INTO users(username,display_name,password_hash,role,enabled,created_at)
                VALUES(?,?,?,?,1,?)
            """, (username, display_name, hash_password(password), role, now_iso()))
            uid = int(cur.lastrowid)
            if uid == 1 and conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1:
                for table in ("devices", "bookmarks", "snapshots", "logs",
                              "bookmark_sources", "tombstones"):
                    conn.execute("UPDATE %s SET user_id=? WHERE user_id=0" % table, (uid,))
            conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                         (now_iso(), "INFO", uid, "创建用户 %s（%s）" % (username, role)))
        return public_user(self.get_user(uid))

    def set_password(self, uid, password):
        if not valid_password(password):
            raise ValueError("密码长度需为 6–128 位")
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(password), uid))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                         (now_iso(), "WARN", uid, "密码已修改，所有登录会话失效"))

    def delete_user(self, uid):
        """删除用户并清空其全部收藏夹数据。"""
        with self.lock, self.connect() as conn:
            for table in ("bookmarks", "bookmark_sources", "tombstones",
                          "snapshots", "devices", "logs", "sessions"):
                conn.execute("DELETE FROM %s WHERE user_id=?" % table, (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))

    def login(self, username, password, source="web"):
        row = self.find_user(username)
        if not row or not row["enabled"] or not verify_password(password, row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + SESSION_DAYS * 86400
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
            conn.execute("INSERT INTO sessions(token,user_id,created_at,expires_at,source) "
                         "VALUES(?,?,?,?,?)", (token, row["id"], now_iso(), expires, source))
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), row["id"]))
        return {"token": token, "expiresAt": expires,
                "sessionDays": SESSION_DAYS, "user": public_user(row)}

    def session_user(self, token):
        if not token:
            return None
        with self.connect() as conn:
            row = conn.execute("""
                SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token=? AND s.expires_at>? AND u.enabled=1
            """, (token, int(time.time()))).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ---- 日志 ----

    def add_log(self, level, message, client_id="", user_id=0):
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO logs(at,level,user_id,client_id,message) VALUES(?,?,?,?,?)",
                         (now_iso(), level, user_id, client_id, message[:500]))
            conn.execute("DELETE FROM logs WHERE user_id=? AND id NOT IN "
                         "(SELECT id FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 300)",
                         (user_id, user_id))

    # ---- 同步：全量快照 + 自动去重 ----

    def save_snapshot(self, payload, user_id):
        client_id = str(payload.get("clientId") or "").strip()
        if not client_id:
            raise ValueError("缺少 clientId")
        if len(client_id) > 128:
            raise ValueError("clientId 过长")
        name = str(payload.get("deviceName") or "未命名设备")[:120]
        browser = str(payload.get("browser") or "")[:200]
        tree = payload.get("tree")
        if not isinstance(tree, list):
            raise ValueError("tree 必须是数组")
        flat = flatten_tree(tree)
        raw = json.dumps(tree, ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_BODY:
            raise ValueError("书签数据超出大小限制")

        # ① 单次上传内部去重：同一网址只保留第一次出现，标题取更完整的那个
        unique = {}
        for i, b in enumerate(flat):
            if not b["url"]:
                continue
            key = url_key(b["url"])
            item = {"key": key, "node_id": b["node_id"] or ("auto_%d" % i),
                    "title": b["title"], "url": b["url"],
                    "path": json.dumps(b["path"], ensure_ascii=False),
                    "date_added": b["date_added"]}
            if key not in unique:
                unique[key] = item
            elif not unique[key]["title"] and item["title"]:
                unique[key]["title"] = item["title"]
        inner_dups = len(flat) - len(unique)

        with self.lock, self.connect() as conn:
            tombstones = {r[0] for r in conn.execute(
                "SELECT url_key FROM tombstones WHERE user_id=?", (user_id,))}
            items = [it for k, it in unique.items() if k not in tombstones]
            skipped = len(unique) - len(items)

            row = conn.execute("SELECT revision FROM devices WHERE user_id=? AND client_id=?",
                               (user_id, client_id)).fetchone()
            revision = (row["revision"] if row else 0) + 1
            ts = now_iso()
            conn.execute("""
                INSERT INTO devices(user_id,client_id,name,browser,last_sync,
                                    bookmark_count,revision,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,client_id) DO UPDATE SET
                    name=excluded.name, browser=excluded.browser, last_sync=excluded.last_sync,
                    bookmark_count=excluded.bookmark_count, revision=excluded.revision
            """, (user_id, client_id, name, browser, ts, len(flat), revision, ts))

            # ② 更新这台设备提供的网址来源
            conn.execute("DELETE FROM bookmark_sources WHERE user_id=? AND client_id=?",
                         (user_id, client_id))
            existing = {r[0] for r in conn.execute(
                "SELECT url_key FROM bookmarks WHERE user_id=?", (user_id,))}
            cross_dups = sum(1 for it in items if it["key"] in existing)
            conn.executemany("""
                INSERT OR REPLACE INTO bookmark_sources
                    (user_id,url_key,client_id,title,path,date_added)
                VALUES(?,?,?,?,?,?)
            """, [(user_id, it["key"], client_id, it["title"], it["path"], it["date_added"])
                  for it in items])

            # ③ 已存在的不覆盖（保留管理端改过的标题/网址），只刷新时间
            conn.executemany("""
                UPDATE bookmarks SET updated_at=?
                WHERE user_id=? AND url_key=? AND source='sync'
            """, [(ts, user_id, it["key"]) for it in items if it["key"] in existing])
            conn.executemany("""
                INSERT INTO bookmarks
                    (user_id,client_id,node_id,title,url,path,date_added,
                     url_key,source,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?, 'sync', ?, ?)
            """, [(user_id, client_id, it["node_id"], it["title"], it["url"],
                   it["path"], it["date_added"], it["key"], ts, ts)
                  for it in items if it["key"] not in existing])

            # ④ 浏览器里已删除的书签，云端同步镜像移除（手动添加的不动）
            conn.execute("""
                DELETE FROM bookmarks WHERE user_id=? AND source='sync'
                AND url_key NOT IN (SELECT url_key FROM bookmark_sources WHERE user_id=?)
            """, (user_id, user_id))

            conn.execute("""
                INSERT INTO snapshots(user_id,client_id,created_at,count,tree)
                VALUES(?,?,?,?,?)
            """, (user_id, client_id, ts, len(flat), raw))
            conn.execute("""
                DELETE FROM snapshots WHERE user_id=? AND client_id=? AND id NOT IN
                (SELECT id FROM snapshots WHERE user_id=? AND client_id=?
                 ORDER BY id DESC LIMIT ?)
            """, (user_id, client_id, user_id, client_id, KEEP_SNAPSHOTS))
            conn.execute("""
                INSERT INTO logs(at,level,user_id,client_id,message) VALUES(?,?,?,?,?)
            """, (ts, "INFO", user_id, client_id,
                  "同步 %d 条，去重 %d 条，云端保留 %d 条（%s）"
                  % (len(flat), inner_dups + cross_dups,
                     conn.execute("SELECT COUNT(*) c FROM bookmarks WHERE user_id=?",
                                  (user_id,)).fetchone()["c"], name)))

        return {"revision": revision, "bookmarkCount": len(flat),
                "uniqueCount": len(items),
                "duplicatesRemoved": inner_dups + cross_dups,
                "skippedDeleted": skipped, "serverTime": ts}

    def devices(self, user_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM devices WHERE user_id=? ORDER BY last_sync DESC", (user_id,))]

    def _decorate(self, rows):
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["path"] = json.loads(d.get("path") or "[]")
            except Exception:
                d["path"] = []
            out.append(d)
        return out

    def all_bookmarks(self, user_id, exclude_client=None, source_client=None,
                      q=None, limit=None, offset=0):
        sql = BM_SELECT + " WHERE b.user_id=?"
        args = [user_id]
        if exclude_client:
            # 只要有「别的设备」或「管理端手动添加」的来源就允许回灌
            sql += (" AND (b.source='manual' OR EXISTS (SELECT 1 FROM bookmark_sources s "
                    "WHERE s.user_id=b.user_id AND s.url_key=b.url_key AND s.client_id<>?))")
            args.append(exclude_client)
        if source_client:
            # v1.3.1：插件按设备标签拉取时，只返回该设备提供过的书签
            sql += (" AND EXISTS (SELECT 1 FROM bookmark_sources s2 "
                    "WHERE s2.user_id=b.user_id AND s2.url_key=b.url_key AND s2.client_id=?)")
            args.append(source_client)
        if q:
            sql += " AND (b.title LIKE ? OR b.url LIKE ?)"
            like = "%" + q + "%"
            args.extend([like, like])
        sql += " ORDER BY b.id DESC"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            args.extend([int(limit), int(offset)])
        with self.connect() as conn:
            return self._decorate(conn.execute(sql, args).fetchall())

    def get_bookmark(self, user_id, bid):
        with self.connect() as conn:
            row = conn.execute(BM_SELECT +
                               " WHERE b.user_id=? AND b.id=?", (user_id, bid)).fetchone()
        return self._decorate([row])[0] if row else None

    def source_keys(self, user_id, client_id):
        with self.connect() as conn:
            return {r[0] for r in conn.execute(
                "SELECT url_key FROM bookmark_sources WHERE user_id=? AND client_id=?",
                (user_id, client_id))}

    def status(self, user_id):
        with self.connect() as conn:
            devices = conn.execute("SELECT COUNT(*) c FROM devices WHERE user_id=?",
                                   (user_id,)).fetchone()["c"]
            marks = conn.execute("SELECT COUNT(*) c FROM bookmarks WHERE user_id=?",
                                 (user_id,)).fetchone()["c"]
            raw = conn.execute("SELECT COUNT(*) c FROM bookmark_sources WHERE user_id=?",
                               (user_id,)).fetchone()["c"]
            synced = conn.execute(
                "SELECT COUNT(*) c FROM bookmarks b WHERE b.user_id=? AND "
                "EXISTS (SELECT 1 FROM bookmark_sources s WHERE s.user_id=b.user_id "
                "AND s.url_key=b.url_key)", (user_id,)).fetchone()["c"]
            snaps = conn.execute("SELECT COUNT(*) c FROM snapshots WHERE user_id=?",
                                 (user_id,)).fetchone()["c"]
            last = conn.execute("SELECT MAX(last_sync) t FROM devices WHERE user_id=?",
                                (user_id,)).fetchone()["t"]
            trash = conn.execute("SELECT COUNT(*) c FROM tombstones WHERE user_id=?",
                                 (user_id,)).fetchone()["c"]
        return {"devices": devices, "bookmarks": marks, "rawBookmarks": raw,
                "duplicatesRemoved": max(0, raw - synced), "trash": trash,
                "snapshots": snaps, "lastSync": last,
                "serverTime": now_iso(), "version": VERSION}

    def search(self, user_id, q, limit=300):
        return self.all_bookmarks(user_id, q=q, limit=limit)

    def logs(self, user_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,))]

    def snapshot_list(self, user_id, client_id=None):
        sql = "SELECT id,client_id,created_at,count FROM snapshots WHERE user_id=?"
        args = [user_id]
        if client_id:
            sql += " AND client_id=?"
            args.append(client_id)
        sql += " ORDER BY id DESC LIMIT 100"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def snapshot_get(self, user_id, sid):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE id=? AND user_id=?",
                               (sid, user_id)).fetchone()
        return json.loads(row["tree"]) if row else None

    # ---- 快照管理（v1.3.1）----

    def snapshot_manage(self, user_id, device=None, limit=100, offset=0):
        """管理端列表：附带设备名与体积，支持设备筛选 / 分页。"""
        limit = max(1, min(200, int(limit or 100)))
        offset = max(0, int(offset or 0))
        with self.connect() as conn:
            devs = [dict(r) for r in conn.execute(
                "SELECT client_id, name FROM devices WHERE user_id=? ORDER BY last_sync DESC",
                (user_id,))]
            names = {d["client_id"]: d["name"] for d in devs}
            where = "WHERE user_id=?"
            args = [user_id]
            if device:
                where += " AND client_id=?"
                args.append(device)
            total = conn.execute("SELECT COUNT(*) c FROM snapshots " + where,
                                 args).fetchone()["c"]
            rows = conn.execute(
                "SELECT id, client_id, created_at, count, LENGTH(tree) AS size "
                "FROM snapshots " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        items = [dict(r) for r in rows]
        for it in items:
            it["deviceName"] = names.get(it["client_id"]) or "已删除的设备"
            it["size"] = it.get("size") or 0
        return {
            "total": total, "items": items,
            "devices": [{"clientId": d["client_id"], "name": d["name"]} for d in devs],
        }

    def delete_snapshots(self, user_id, ids=None, device=None, all_records=False):
        """删除快照。仅删当前用户的记录，跨用户 id 静默跳过。"""
        with self.lock, self.connect() as conn:
            if all_records or device:
                sql = "DELETE FROM snapshots WHERE user_id=?"
                args = [user_id]
                if device:
                    sql += " AND client_id=?"
                    args.append(device)
                n = conn.execute(sql, args).rowcount or 0
            else:
                clean = [int(x) for x in (ids or []) if str(x).isdigit()]
                if not clean:
                    return 0
                n = conn.execute(
                    "DELETE FROM snapshots WHERE user_id=? AND id IN (%s)" %
                    ",".join("?" * len(clean)), [user_id] + clean).rowcount or 0
            if n:
                what = "全部快照" if all_records else (
                    "设备 %s 的快照" % device if device else "%d 份快照" % n)
                conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                             (now_iso(), "WARN", user_id,
                              "删除%s（%d 份），不影响当前书签" % (what, n)))
        return n

    def delete_device(self, user_id, client_id):
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM bookmark_sources WHERE user_id=? AND client_id=?",
                         (user_id, client_id))
            conn.execute("DELETE FROM bookmarks WHERE user_id=? AND source='sync' "
                         "AND url_key NOT IN (SELECT url_key FROM bookmark_sources WHERE user_id=?)",
                         (user_id, user_id))
            conn.execute("DELETE FROM snapshots WHERE user_id=? AND client_id=?",
                         (user_id, client_id))
            conn.execute("DELETE FROM devices WHERE user_id=? AND client_id=?",
                         (user_id, client_id))
            conn.execute("INSERT INTO logs(at,level,user_id,client_id,message) VALUES(?,?,?,?,?)",
                         (now_iso(), "WARN", user_id, client_id, "删除设备及其全部书签快照"))

    # ---- 设备管理页 / 书签浏览（v1.3.0）----

    def device_rows(self, user_id, client_id):
        """某台设备自己同步上来的书签：路径取该设备的来源记录，
        标题与网址取书签库当前值（管理端改过则显示改后的值）。"""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT b.id AS id, b.url AS url, b.url_key AS url_key,
                       b.source AS source,
                       COALESCE(NULLIF(s.title, ''), b.title) AS title,
                       s.path AS path, s.date_added AS date_added
                FROM bookmark_sources s
                JOIN bookmarks b ON b.user_id=s.user_id AND b.url_key=s.url_key
                WHERE s.user_id=? AND s.client_id=?
                ORDER BY s.rowid
            """, (user_id, client_id)).fetchall()
        return self._decorate(rows)

    def device_detail(self, user_id, client_id):
        dev = None
        for d in self.devices(user_id):
            if d["client_id"] == client_id:
                dev = d
                break
        if not dev:
            return None
        rows = self.device_rows(user_id, client_id)
        for r in rows:
            r["device_name"] = dev["name"]
            r["devices"] = [dev["name"]]
        return {"device": dev, "items": rows, "total": len(rows),
                "tree": tree_from_rows(rows),
                "snapshots": self.snapshot_list(user_id, client_id)}

    def browse(self, user_id, client_id=None):
        """只读书签浏览：返回设备标签 + 目录树，叶子上标记来源设备。"""
        devs = self.devices(user_id)
        names = {d["client_id"]: d["name"] for d in devs}
        with self.connect() as conn:
            counts = {r["client_id"]: r["c"] for r in conn.execute(
                "SELECT client_id, COUNT(*) c FROM bookmark_sources "
                "WHERE user_id=? GROUP BY client_id", (user_id,))}
            src = {}
            if not client_id:
                for r in conn.execute(
                        "SELECT url_key, client_id FROM bookmark_sources WHERE user_id=?",
                        (user_id,)):
                    src.setdefault(r["url_key"], []).append(
                        names.get(r["client_id"], r["client_id"]))
        if client_id:
            if client_id not in names:
                return None
            rows = self.device_rows(user_id, client_id)
            for r in rows:
                r["device_name"] = names.get(client_id, client_id)
                r["devices"] = [r["device_name"]]
        else:
            rows = self.all_bookmarks(user_id)
            rows.reverse()          # 按入库顺序展示，目录树阅读顺序更自然
            for r in rows:
                r["devices"] = src.get(r["url_key"]) or (
                    [r["device_name"]] if r["device_name"] else [])
        return {
            "devices": [{"clientId": d["client_id"], "name": d["name"],
                         "count": counts.get(d["client_id"], 0),
                         "lastSync": d["last_sync"]} for d in devs],
            "tree": tree_from_rows(rows),
            "total": len(rows),
        }

    def move_bookmarks(self, user_id, ids, path):
        """批量修改书签所在文件夹。改动后转为「手动维护」，同步不会覆盖。"""
        p = json.dumps(normalize_path(path), ensure_ascii=False)
        ts = now_iso()
        moved = 0
        with self.lock, self.connect() as conn:
            for bid in ids:
                row = conn.execute("SELECT * FROM bookmarks WHERE id=? AND user_id=?",
                                   (bid, user_id)).fetchone()
                if not row:
                    continue
                conn.execute("UPDATE bookmarks SET path=?, source='manual', updated_at=? "
                             "WHERE id=? AND user_id=?", (p, ts, bid, user_id))
                conn.execute("UPDATE bookmark_sources SET path=? "
                             "WHERE user_id=? AND url_key=?", (p, user_id, row["url_key"]))
                moved += 1
            if moved:
                where = " / ".join(normalize_path(path)) or "根目录"
                conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                             (ts, "INFO", user_id, "移动 %d 条书签到「%s」" % (moved, where)))
        return moved

    def clear_tombstones(self, user_id):
        with self.lock, self.connect() as conn:
            n = conn.execute("DELETE FROM tombstones WHERE user_id=?",
                             (user_id,)).rowcount or 0
            if n:
                conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                             (now_iso(), "WARN", user_id,
                              "清空回收站（%d 条），这些网址下次同步会重新带回" % n))
        return n

    # ---- 管理端书签增删改查 ----

    def create_bookmark(self, user_id, title, url, path=None):
        url = str(url or "").strip()
        if not valid_bookmark_url(url):
            raise ApiError(400, "请填写合法的网址（需以 http:// 或 https:// 开头）")
        key = url_key(url)
        p = json.dumps(normalize_path(path), ensure_ascii=False)
        with self.lock, self.connect() as conn:
            dup = conn.execute("SELECT id FROM bookmarks WHERE user_id=? AND url_key=?",
                               (user_id, key)).fetchone()
            if dup:
                raise ApiError(409, "该网址已存在于书签库（#%d），无需重复添加" % dup["id"])
            conn.execute("DELETE FROM tombstones WHERE user_id=? AND url_key=?",
                         (user_id, key))
            ts = now_iso()
            cur = conn.execute("""
                INSERT INTO bookmarks
                    (user_id,client_id,node_id,title,url,path,date_added,
                     url_key,source,created_at,updated_at)
                VALUES(?, 'manual', '', ?, ?, ?, 0, ?, 'manual', ?, ?)
            """, (user_id, clean_title(title, url), url, p, key, ts, ts))
            conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                         (ts, "INFO", user_id, "手动新增书签：%s" % url))
        return self.get_bookmark(user_id, int(cur.lastrowid))

    def update_bookmark(self, user_id, bid, patch):
        row = self.get_bookmark(user_id, bid)
        if not row:
            raise ApiError(404, "书签不存在或不属于当前用户")
        url = str(patch.get("url", row["url"]) or "").strip()
        if not valid_bookmark_url(url):
            raise ApiError(400, "请填写合法的网址（需以 http:// 或 https:// 开头）")
        key = url_key(url)
        title = clean_title(patch.get("title", row["title"]), url)
        p = json.dumps(normalize_path(patch["path"]) if "path" in patch else row["path"],
                       ensure_ascii=False)
        with self.lock, self.connect() as conn:
            dup = conn.execute(
                "SELECT id FROM bookmarks WHERE user_id=? AND url_key=? AND id<>?",
                (user_id, key, bid)).fetchone()
            if dup:
                raise ApiError(409, "已存在相同网址（#%d），请修改后保存" % dup["id"])
            if key != row["url_key"]:
                # 改过网址后，把旧网址放进回收站，防止下次同步又把它带回来；
                # 该条转为「手动维护」，编辑结果不会被浏览器端覆盖。
                conn.execute("INSERT OR REPLACE INTO tombstones"
                             "(user_id,url_key,url,title,deleted_at) VALUES(?,?,?,?,?)",
                             (user_id, row["url_key"], row["url"], row["title"], now_iso()))
                conn.execute("DELETE FROM bookmark_sources WHERE user_id=? AND url_key=?",
                             (user_id, row["url_key"]))
            conn.execute("""
                UPDATE bookmarks SET title=?, url=?, url_key=?, path=?,
                    source='manual', updated_at=? WHERE id=? AND user_id=?
            """, (title, url, key, p, now_iso(), bid, user_id))
            conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                         (now_iso(), "INFO", user_id, "编辑书签 #%d：%s" % (bid, url)))
        return self.get_bookmark(user_id, bid)

    def delete_bookmarks(self, user_id, ids):
        deleted = []
        with self.lock, self.connect() as conn:
            for bid in ids:
                row = conn.execute("SELECT * FROM bookmarks WHERE id=? AND user_id=?",
                                   (bid, user_id)).fetchone()
                if not row:
                    continue
                conn.execute("INSERT OR REPLACE INTO tombstones"
                             "(user_id,url_key,url,title,deleted_at) VALUES(?,?,?,?,?)",
                             (user_id, row["url_key"], row["url"], row["title"], now_iso()))
                conn.execute("DELETE FROM bookmarks WHERE id=? AND user_id=?", (bid, user_id))
                conn.execute("DELETE FROM bookmark_sources WHERE user_id=? AND url_key=?",
                             (user_id, row["url_key"]))
                deleted.append(bid)
            if deleted:
                conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                             (now_iso(), "WARN", user_id,
                              "删除 %d 条书签（同步时自动跳过，可在回收站恢复）" % len(deleted)))
        return deleted

    def list_tombstones(self, user_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT url_key,url,title,deleted_at FROM tombstones "
                "WHERE user_id=? ORDER BY deleted_at DESC, url_key", (user_id,))]

    def restore_bookmark(self, user_id, key=None, url=None):
        key = key or (url_key(url) if url else "")
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM tombstones WHERE user_id=? AND url_key=?",
                               (user_id, key)).fetchone()
            if not row:
                raise ApiError(404, "回收站里没有这条记录")
            conn.execute("DELETE FROM tombstones WHERE user_id=? AND url_key=?",
                         (user_id, key))
            exists = conn.execute("SELECT id FROM bookmarks WHERE user_id=? AND url_key=?",
                                  (user_id, key)).fetchone()
            if not exists:
                ts = now_iso()
                conn.execute("""
                    INSERT INTO bookmarks
                        (user_id,client_id,node_id,title,url,path,date_added,
                         url_key,source,created_at,updated_at)
                    VALUES(?, 'manual', '', ?, ?, '[]', 0, ?, 'manual', ?, ?)
                """, (user_id, row["title"] or row["url"] or "", row["url"] or "",
                      key, ts, ts))
            conn.execute("INSERT INTO logs(at,level,user_id,message) VALUES(?,?,?,?)",
                         (now_iso(), "INFO", user_id, "从回收站恢复书签：%s" % (row["url"] or key)))
        return {"ok": True, "restored": row["url"] or ""}

    def purge_tombstone(self, user_id, key):
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM tombstones WHERE user_id=? AND url_key=?",
                         (user_id, key))


def tree_from_rows(rows):
    """按 path 重建目录树（不按设备分组），给每个叶子打上设备标记。

    v1.3.0：设备管理页与「书签浏览」共用同一棵树。叶子带 id，前端据此
    勾选、批量移动文件夹或打开编辑弹窗；文件夹节点只有 children。
    """
    root = {"title": "", "children": [], "_folders": {}}
    for r in rows:
        node = root
        for seg in (r.get("path") or []):
            seg = str(seg).strip()
            if not seg:
                continue
            node = node["_folders"].setdefault(
                seg, {"title": seg, "children": [], "_folders": {}})
        leaf = {
            "id": r.get("id"),
            "title": r.get("title") or r.get("url") or "",
            "url": r.get("url") or "",
            "dateAdded": r.get("date_added") or 0,
            "device": r.get("device_name") or "",
            "devices": r.get("devices") or ([r["device_name"]] if r.get("device_name") else []),
            "source": r.get("source") or "sync",
        }
        node["children"].append(leaf)

    def clean(node):
        # 子文件夹在前、书签在后，阅读顺序稳定
        children = [clean(x) for x in node["_folders"].values()] + node["children"]
        return {"title": node["title"], "children": children}

    return [clean(x) for x in root["_folders"].values()] + root["children"]


def build_tree(rows):
    """把扁平书签重建为按设备分组的嵌套树，供导出使用。"""
    groups = {}
    for r in rows:
        dev = r.get("device_name") or r["client_id"]
        g = groups.setdefault(dev, {"title": dev, "children": [], "_folders": {}})
        node = g
        for seg in r.get("path") or []:
            node = node["_folders"].setdefault(
                seg, {"title": seg, "children": [], "_folders": {}})
        node["children"].append({
            "title": r["title"], "url": r["url"],
            "dateAdded": r.get("date_added") or 0,
            "device": dev,
        })

    def clean(node):
        out = {"title": node["title"], "children": []}
        for c in node["children"]:
            out["children"].append(c)
        for sub in node["_folders"].values():
            out["children"].append(clean(sub))
        return out

    return [clean(g) for g in groups.values()]


def maybe_export_share(store, user_id, username=""):
    """每次同步后自动写一份 HTML 备份，只导出当前用户自己的书签。"""
    targets = []
    share = os.environ.get("EB_SHARE_DIR", "").strip()
    if share:
        targets.append(Path(share))
    targets.append(Path(store.dir) / "exports")
    try:
        data = netscape_html(store.all_bookmarks(user_id))
    except Exception as exc:
        log("生成备份文件失败（不影响同步）：%s" % exc)
        return
    stamp = datetime.now().strftime("%Y%m%d")
    safe_user = re.sub(r"[^\w.-]", "_", str(username or "user"))[:40]
    for p in targets:
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / "bookmarks-latest.html").write_text(data, encoding="utf-8")
            (p / ("bookmarks-%s.html" % stamp)).write_text(data, encoding="utf-8")
            (p / ("bookmarks-%s-latest.html" % safe_user)).write_text(data, encoding="utf-8")
            log("已备份用户 %s 的书签至：%s" % (username, p))
        except Exception as exc:
            log("写入备份目录 %s 失败（不影响同步）：%s" % (p, exc))


def netscape_html_tree(tree):
    """把嵌套目录树输出为可被 Chromium 系浏览器 / Firefox 直接导入的 Netscape 书签文件。"""
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<!-- This is an automatically generated file. -->',
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]

    def emit(nodes, indent):
        pad = "    " * indent
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if "url" in node:
                lines.append('%s<DT><A HREF="%s" ADD_DATE="%s">%s</A>' % (
                    pad, html.escape(str(node["url"]), quote=True),
                    str(node.get("dateAdded", 0))[:10],
                    html.escape(str(node.get("title") or node["url"]))))
            else:
                lines.append("%s<DT><H3>%s</H3>" % (
                    pad, html.escape(str(node.get("title") or "未命名"))))
                lines.append("%s<DL><p>" % pad)
                emit(node.get("children") or [], indent + 1)
                lines.append("%s</DL><p>" % pad)

    emit(tree or [], 1)
    lines.append("</DL><p>")
    return "\r\n".join(lines) + "\r\n"


def netscape_html(rows):
    """扁平书签行 → Netscape HTML（按设备分组，保留原始目录）。"""
    return netscape_html_tree(build_tree(rows))


def _csv_time(value):
    try:
        n = int(value or 0)
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S") if n > 0 else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def bookmarks_csv(rows):
    """通用 CSV：标题 / 网址 / 目录 / 来源设备 / 添加时间；UTF-8 BOM 便于 Excel 直接打开。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["标题", "网址", "目录", "来源设备", "添加时间"])
    for r in rows:
        w.writerow([
            r.get("title") or "",
            r.get("url") or "",
            "/".join(str(x) for x in (r.get("path") or [])),
            r.get("device_name") or r.get("device") or r.get("client_id") or "",
            _csv_time(r.get("date_added")),
        ])
    return "\ufeff" + buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "FnBookmarks/" + VERSION
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def _headers(self, extra=None):
        # 插件走飞牛统一网关时要携带浏览器里的 NAS 会话 Cookie，
        # CORS 必须回显具体 Origin 并允许 credentials（通配符 * 不能与
        # credentials 同时使用）。本服务鉴权只认 Bearer 会话令牌，不依赖
        # Cookie，因此回显任意 Origin 不会引入 CSRF 风险。
        origin = str(self.headers.get("Origin") or "").strip()
        h = {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Auth-Token",
            "Access-Control-Max-Age": "86400",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if origin:
            h["Access-Control-Allow-Credentials"] = "true"
            h["Vary"] = "Origin"
        if extra:
            h.update(extra)
        return h

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = self._headers(extra)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _error(self, code, message):
        self._json({"ok": False, "error": message}, code)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大（上限 32MB）")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def address_string(self):
        addr = getattr(self, "client_address", None)
        if isinstance(addr, tuple) and addr:
            return str(addr[0])
        return "unix-gateway"

    def _gateway_identity(self):
        """仅用于界面提示：网关通道携带的 NAS 登录用户名。

        授权完全基于应用内账号密码，身份头不参与鉴权，因此无论从哪个通道
        伪造 X-Trim-* 都无法越权。
        """
        if not getattr(self.server, "gateway", False):
            return None
        uid = (self.headers.get("X-Trim-Userid") or "").strip()
        name = (self.headers.get("X-Trim-Username") or "").strip()
        if not uid or not name:
            return None
        return {"uid": uid, "name": name,
                "admin": (self.headers.get("X-Trim-Isadmin") or "").strip().lower()
                in ("true", "1", "yes")}

    def _given_token(self, query):
        """会话令牌来源：Authorization: Bearer / X-Auth-Token。"""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if self.headers.get("X-Auth-Token"):
            return self.headers.get("X-Auth-Token").strip()
        return (query.get("token") or [""])[0]

    def _basic_credentials(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("basic "):
            return None
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
            username, password = raw.split(":", 1)
            return username, password
        except Exception:
            return None

    def _auth(self, query, admin=False):
        """返回 (用户字典, None) 或 (None, (状态码, 错误信息))。"""
        store = self.app["store"]
        user = None
        basic = self._basic_credentials()
        if basic:
            result = store.login(basic[0], basic[1], "basic")
            user = dict(store.get_user(result["user"]["id"])) if result else None
            if result:
                store.logout(result["token"])   # Basic 每次校验，不留会话
        else:
            user = store.session_user(self._given_token(query))
        if not user:
            return None, (401, "请先登录：需要本应用的用户名与密码")
        if admin and user["role"] != "admin":
            return None, (403, "需要管理员权限")
        return user, None

    def _route(self):
        """剥离网关 / 反代前缀，返回 (parsed, 应用内路径, 页面 base)。"""
        parsed = urllib.parse.urlparse(self.path)
        raw = parsed.path
        fwd = safe_prefix(self.headers.get("X-Forwarded-Prefix", ""))
        prefixes = configured_prefixes()
        if fwd:
            prefixes = [fwd] + [p for p in prefixes if p != fwd]
        inner = base = None
        for p in prefixes:
            pre = p + "/"
            if raw.startswith(pre):
                inner, base = "/" + raw[len(pre):], pre
                break
            if raw.rstrip("/") == p:
                inner, base = "/", pre
                break
        if inner is None:
            prefix, rest = auto_prefix(raw)
            if prefix:
                inner, base = rest, prefix + "/"
            elif fwd:
                inner, base = raw, fwd + "/"
            else:
                inner, base = raw, "/"
        return parsed, inner.rstrip("/") or "/", base

    def _plugin_info(self, base):
        return plugin_addresses(self.app, base, self.headers,
                                getattr(self.server, "gateway", False))

    def _plugin_payload(self, base, username=""):
        info = self._plugin_info(base)
        cfg = plugin_config(info, username)
        return {"ok": True, "app": APP_NAME, "title": APP_TITLE,
                "version": VERSION, "username": username,
                "serverUrl": info["primary"], "serverUrlAlt": info["alternate"],
                "serverUrls": info["urls"], "addresses": info["addresses"],
                "config": cfg["config"], "configText": cfg["configText"],
                "pageOrigin": info["origin"], "pageBase": info["pageBase"],
                "pageUrl": info["pageUrl"], "gatewayUrl": info["gatewayUrl"],
                "isGatewayPage": info["isGatewayPage"],
                "channel": info["channel"], "gatewayPrefix": info["gatewayPrefix"],
                "proxyPrefixes": info["proxyPrefixes"], "directPort": info["directPort"],
                "autoDetected": True,
                "hint": "主地址就是当前管理页地址，界面无需填写；插件会自动逐个探测，"
                        "任一可达即可。经飞牛统一网关访问时会复用浏览器登录态，"
                        "失败则自动切换到局域网直连。密码不会包含在下发的配置中。"}

    def _setup_state(self, base):
        store = self.app["store"]
        ident = self._gateway_identity()
        info = self._plugin_info(base)
        return {
            "ok": True,
            "app": APP_NAME,
            "title": APP_TITLE,
            "version": VERSION,
            "authMode": AUTH_MODE,
            "needsAdminSetup": store.user_count() == 0,
            "userCount": store.user_count(),
            "gatewayAuth": bool(ident),
            "gatewayUser": ident["name"] if ident else "",
            "gatewayPrefix": GATEWAY_PREFIX,
            "proxyPrefixes": configured_prefixes(),
            "pageBase": base,
            "directPort": int(self.app.get("port", 8765)),
            "pluginUrls": info["urls"],
            "pluginUrl": info["primary"],
            "pluginUrlAlt": info["alternate"],
            "gatewaySocket": bool(self.app.get("gatewaySocket")),
            "sessionDays": SESSION_DAYS,
        }

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed, path, base = self._route()
        q = urllib.parse.parse_qs(parsed.query)
        store = self.app["store"]

        if path == "/api/v1/health":
            return self._json({"ok": True, "app": APP_NAME, "title": APP_TITLE,
                               "version": VERSION, "authMode": AUTH_MODE,
                               "needsAdminSetup": store.user_count() == 0,
                               "time": now_iso(), "base": base,
                               "channel": "gateway" if self.server.gateway else "direct",
                               "prefixes": configured_prefixes()})

        if path == "/api/v1/setup/state":
            return self._json(self._setup_state(base))

        if not path.startswith("/api/"):
            return self._static(path, base)

        if path in LEGACY_TOKEN_PATHS:
            return self._error(410, LEGACY_TOKEN_MESSAGE)

        user, err = self._auth(q)
        if err:
            return self._error(*err)
        uid = user["id"]

        try:
            if path == "/api/v1/auth/me":
                return self._json({"ok": True, "user": public_user(user)})

            if path == "/api/v1/plugin-config":
                return self._json(self._plugin_payload(base, user["username"]))

            if path == "/api/v1/status":
                return self._json({"ok": True, **store.status(uid)})

            if path == "/api/v1/bookmarks":
                limit = (q.get("limit") or ["0"])[0]
                try:
                    limit = max(0, min(1000, int(limit)))
                except ValueError:
                    limit = 0
                try:
                    offset = max(0, int((q.get("offset") or ["0"])[0]))
                except ValueError:
                    offset = 0
                term = (q.get("q") or [""])[0].strip() or None
                rows = store.all_bookmarks(uid, q=term, limit=limit or None, offset=offset)
                total = len(store.all_bookmarks(uid, q=term))
                return self._json({"ok": True, "total": total, "count": len(rows),
                                   "items": rows, "tree": build_tree(rows),
                                   "dedup": True})

            if path == "/api/v1/bookmarks/item":
                try:
                    bid = int((q.get("id") or ["0"])[0])
                except ValueError:
                    return self._error(400, "id 非法")
                item = store.get_bookmark(uid, bid)
                if not item:
                    return self._error(404, "书签不存在或不属于当前用户")
                return self._json({"ok": True, "item": item})

            if path == "/api/v1/bookmarks/trash":
                return self._json({"ok": True, "items": store.list_tombstones(uid)})

            if path == "/api/v1/incoming":
                exclude = (q.get("exclude") or [""])[0]
                device = (q.get("device") or [""])[0]
                devs = store.devices(uid)
                if device and device not in {d["client_id"] for d in devs}:
                    return self._error(404, "设备不存在或不属于当前用户")
                dev_name = next((d["name"] for d in devs
                                 if d["client_id"] == device), "") if device else ""
                rows = store.all_bookmarks(uid, exclude_client=exclude or None,
                                           source_client=device or None)
                return self._json({"ok": True, "total": len(rows), "items": rows,
                                   "device": device or "", "deviceName": dev_name})

            if path == "/api/v1/search":
                term = (q.get("q") or [""])[0].strip()
                if not term:
                    return self._json({"ok": True, "items": []})
                return self._json({"ok": True, "items": store.search(uid, term)})

            if path == "/api/v1/devices/tree":
                cid = (q.get("clientId") or [""])[0]
                if not cid:
                    return self._error(400, "缺少 clientId")
                data = store.device_detail(uid, cid)
                if data is None:
                    return self._error(404, "设备不存在或不属于当前用户")
                return self._json({"ok": True, **data})

            if path == "/api/v1/browse":
                data = store.browse(uid, (q.get("device") or [""])[0] or None)
                if data is None:
                    return self._error(404, "设备不存在或不属于当前用户")
                return self._json({"ok": True, **data})

            if path == "/api/v1/devices":
                return self._json({"ok": True, "items": store.devices(uid)})

            if path == "/api/v1/logs":
                return self._json({"ok": True, "items": store.logs(uid)})

            if path == "/api/v1/snapshots/manage":
                try:
                    limit = int((q.get("limit") or ["100"])[0])
                    offset = int((q.get("offset") or ["0"])[0])
                except ValueError:
                    return self._error(400, "limit / offset 非法")
                data = store.snapshot_manage(
                    uid, (q.get("device") or [""])[0] or None, limit, offset)
                return self._json({"ok": True, **data})

            if path == "/api/v1/snapshots":
                return self._json({"ok": True, "items": store.snapshot_list(
                    uid, (q.get("clientId") or [None])[0])})

            if path == "/api/v1/snapshot":
                try:
                    sid = int((q.get("id") or ["0"])[0])
                except ValueError:
                    return self._error(400, "id 非法")
                data = store.snapshot_get(uid, sid)
                if data is None:
                    return self._error(404, "快照不存在或不属于当前用户")
                fmt = (q.get("format") or [""])[0]
                if not fmt:
                    # 预览弹窗用：返回原始目录树 JSON（向后兼容）
                    return self._json({"ok": True, "tree": data})
                if fmt not in ("html", "json", "csv"):
                    return self._error(400, "format 仅支持 html / json / csv")
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                if fmt == "json":
                    payload = json.dumps({"app": APP_NAME, "version": VERSION,
                                          "snapshotId": sid, "exportedAt": now_iso(),
                                          "tree": data}, ensure_ascii=False, indent=2)
                    return self._send(200, payload, "application/json; charset=utf-8",
                                      {"Content-Disposition":
                                       'attachment; filename="snapshot-%s-%s.json"' % (sid, ts)})
                if fmt == "csv":
                    payload = bookmarks_csv(flatten_tree(data))
                    return self._send(200, payload, "text/csv; charset=utf-8",
                                      {"Content-Disposition":
                                       'attachment; filename="snapshot-%s-%s.csv"' % (sid, ts)})
                payload = netscape_html_tree(data)
                return self._send(200, payload, "text/html; charset=utf-8",
                                  {"Content-Disposition":
                                   'attachment; filename="snapshot-%s-%s.html"' % (sid, ts)})

            if path == "/api/v1/export":
                fmt = (q.get("format") or ["html"])[0]
                if fmt not in ("html", "json", "csv"):
                    return self._error(400, "format 仅支持 html / json / csv")
                device = (q.get("device") or [""])[0]
                rows = store.all_bookmarks(uid)
                if device:
                    keys = store.source_keys(uid, device)
                    rows = [r for r in rows if r["url_key"] in keys]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                if fmt == "json":
                    payload = json.dumps({"app": APP_NAME, "version": VERSION,
                                          "user": user["username"], "exportedAt": now_iso(),
                                          "items": rows, "tree": build_tree(rows)},
                                         ensure_ascii=False, indent=2)
                    return self._send(200, payload, "application/json; charset=utf-8",
                                      {"Content-Disposition":
                                       'attachment; filename="bookmarks-%s.json"' % ts})
                if fmt == "csv":
                    payload = bookmarks_csv(rows)
                    return self._send(200, payload, "text/csv; charset=utf-8",
                                      {"Content-Disposition":
                                       'attachment; filename="bookmarks-%s.csv"' % ts})
                data = netscape_html(rows)
                return self._send(200, data, "text/html; charset=utf-8",
                                  {"Content-Disposition":
                                   'attachment; filename="bookmarks-%s.html"' % ts})

            if path == "/api/v1/users":
                if user["role"] != "admin":
                    return self._error(403, "需要管理员权限")
                return self._json({"ok": True, "items": store.list_users()})

            return self._error(404, "接口不存在")
        except ApiError as exc:
            return self._error(exc.code, exc.message)
        except Exception as exc:
            log("GET %s 出错: %s" % (path, exc))
            return self._error(500, "服务端处理失败：%s" % exc)

    def do_POST(self):
        parsed, path, base = self._route()
        q = urllib.parse.parse_qs(parsed.query)
        store = self.app["store"]

        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        if body is None:
            return self._error(400, "请求体为空")
        if not isinstance(body, dict):
            return self._error(400, "请求体必须是 JSON 对象")

        # ---- 公开接口：首次注册管理员 / 登录 ----
        if path == "/api/v1/setup/register":
            if store.user_count() > 0:
                return self._error(403, "管理员已注册，请由管理员在「用户管理」中添加用户")
            try:
                created = store.create_user(body.get("username"), body.get("password"),
                                            role="admin",
                                            display_name=body.get("displayName") or "")
            except ValueError as exc:
                return self._error(400, str(exc))
            except Exception as exc:
                return self._error(500, "创建管理员失败：%s" % exc)
            result = store.login(created["username"], body.get("password"), "setup")
            store.add_log("INFO", "注册首位管理员 %s" % created["username"],
                          user_id=created["id"])
            log("已注册首位管理员：%s" % created["username"])
            return self._json({"ok": True, "registered": True, **result})

        if path == "/api/v1/auth/login":
            result = store.login(body.get("username"), body.get("password"), "login")
            if not result:
                return self._error(401, "用户名或密码错误")
            store.add_log("INFO", "用户 %s 登录" % result["user"]["username"],
                          user_id=result["user"]["id"])
            return self._json({"ok": True, **result})

        if path in LEGACY_TOKEN_PATHS:
            return self._error(410, LEGACY_TOKEN_MESSAGE)

        # ---- 以下接口均需登录 ----
        user, err = self._auth(q)
        if err:
            return self._error(*err)
        uid = user["id"]

        try:
            if path == "/api/v1/auth/logout":
                store.logout(self._given_token(q))
                return self._json({"ok": True})

            if path == "/api/v1/auth/password":
                old = body.get("oldPassword") or ""
                if not verify_password(old, user["password_hash"]):
                    return self._error(403, "原密码不正确")
                try:
                    store.set_password(uid, body.get("newPassword"))
                except ValueError as exc:
                    return self._error(400, str(exc))
                return self._json({"ok": True, "hint": "密码已修改，请重新登录"})

            if path == "/api/v1/users":
                if user["role"] != "admin":
                    return self._error(403, "需要管理员权限")
                try:
                    created = store.create_user(body.get("username"), body.get("password"),
                                                role=body.get("role") or "user",
                                                display_name=body.get("displayName") or "")
                except ValueError as exc:
                    return self._error(400, str(exc))
                except Exception as exc:
                    return self._error(500, "创建用户失败：%s" % exc)
                store.add_log("INFO", "新增用户 %s" % created["username"], user_id=uid)
                return self._json({"ok": True, "user": created})

            if path == "/api/v1/users/password":
                if user["role"] != "admin":
                    return self._error(403, "需要管理员权限")
                try:
                    target = int(body.get("id") or 0)
                except (TypeError, ValueError):
                    return self._error(400, "id 非法")
                if not store.get_user(target):
                    return self._error(404, "用户不存在")
                try:
                    store.set_password(target, body.get("password"))
                except ValueError as exc:
                    return self._error(400, str(exc))
                store.add_log("WARN", "管理员重置用户 #%d 的密码" % target, user_id=uid)
                return self._json({"ok": True})

            if path == "/api/v1/bookmarks":
                item = store.create_bookmark(uid, body.get("title"), body.get("url"),
                                             body.get("path"))
                return self._json({"ok": True, "item": item})

            if path == "/api/v1/bookmarks/restore":
                return self._json(store.restore_bookmark(
                    uid, body.get("key"), body.get("url")))

            if path == "/api/v1/bookmarks/delete":
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids:
                    return self._error(400, "ids 必须是非空数组")
                return self._json({"ok": True, "deleted": store.delete_bookmarks(
                    uid, [int(x) for x in ids if str(x).isdigit()])})

            if path == "/api/v1/bookmarks/batch":
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids:
                    return self._error(400, "ids 必须是非空数组")
                clean = [int(x) for x in ids if str(x).isdigit()]
                if not clean:
                    return self._error(400, "ids 里没有合法编号")
                if "path" not in body:
                    return self._error(400, "缺少 path（用空字符串表示移到根目录）")
                moved = store.move_bookmarks(uid, clean, body.get("path"))
                return self._json({"ok": True, "moved": moved,
                                   "hint": "已转为手动维护，浏览器下次同步不会覆盖"})

            if path == "/api/v1/sync":
                try:
                    result = store.save_snapshot(body, uid)
                except ValueError as exc:
                    return self._error(400, str(exc))
                except Exception as exc:
                    log("同步失败: %s" % exc)
                    return self._error(500, "保存失败：%s" % exc)
                resp = {"ok": True, **result, "devices": len(store.devices(uid)),
                        "serverTotal": store.status(uid)["bookmarks"]}
                threading.Thread(target=maybe_export_share,
                                 args=(store, uid, user["username"]), daemon=True).start()
                if body.get("pull"):
                    rows = store.all_bookmarks(uid, exclude_client=str(body.get("clientId") or ""))
                    resp["incoming"] = {"total": len(rows), "items": rows}
                return self._json(resp)

            return self._error(404, "接口不存在")
        except ApiError as exc:
            return self._error(exc.code, exc.message)
        except (ValueError, TypeError) as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            log("POST %s 出错: %s" % (path, exc))
            return self._error(500, "服务端处理失败：%s" % exc)

    def do_PUT(self):
        return self._mutate("PUT")

    def do_PATCH(self):
        return self._mutate("PATCH")

    def _mutate(self, method):
        parsed, path, base = self._route()
        q = urllib.parse.parse_qs(parsed.query)
        store = self.app["store"]
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        if not isinstance(body, dict):
            return self._error(400, "请求体必须是 JSON 对象")
        user, err = self._auth(q)
        if err:
            return self._error(*err)
        if path != "/api/v1/bookmarks":
            return self._error(404, "接口不存在")
        try:
            bid = int(body.get("id") or (q.get("id") or ["0"])[0])
        except (TypeError, ValueError):
            return self._error(400, "id 非法")
        patch = {k: body[k] for k in ("title", "url", "path") if k in body}
        if not patch:
            return self._error(400, "没有需要修改的字段")
        try:
            item = store.update_bookmark(user["id"], bid, patch)
        except ApiError as exc:
            return self._error(exc.code, exc.message)
        except Exception as exc:
            return self._error(500, "保存失败：%s" % exc)
        return self._json({"ok": True, "method": method, "item": item})

    def do_DELETE(self):
        parsed, path, base = self._route()
        q = urllib.parse.parse_qs(parsed.query)
        store = self.app["store"]
        user, err = self._auth(q)
        if err:
            return self._error(*err)
        uid = user["id"]

        if path == "/api/v1/bookmarks":
            raw_ids = (q.get("id") or [""])[0] or (q.get("ids") or [""])[0]
            ids = [int(x) for x in re.split(r"[,]+", raw_ids) if x.strip().isdigit()]
            if not ids:
                return self._error(400, "缺少 id（可用 ids=1,2,3 批量删除）")
            deleted = store.delete_bookmarks(uid, ids)
            return self._json({"ok": True, "deleted": deleted,
                               "hint": "已删除的网址会在下次同步自动跳过，可在回收站恢复"})

        if path == "/api/v1/bookmarks/trash":
            if (q.get("all") or [""])[0] in ("1", "true", "yes"):
                return self._json({"ok": True, "cleared": store.clear_tombstones(uid),
                                   "hint": "回收站已清空，这些网址下次同步会重新带回"})
            key = (q.get("key") or [""])[0]
            if not key:
                return self._error(400, "缺少 key（或用 all=1 清空回收站）")
            store.purge_tombstone(uid, key)
            return self._json({"ok": True, "hint": "已彻底移除，下次同步可重新带回该网址"})

        if path == "/api/v1/snapshot":
            all_flag = (q.get("all") or [""])[0] in ("1", "true", "yes")
            device = (q.get("device") or [""])[0]
            raw_ids = (q.get("id") or [""])[0] or (q.get("ids") or [""])[0]
            ids = [int(x) for x in re.split(r"[,]+", raw_ids) if x.strip().isdigit()]
            if not all_flag and not device and not ids:
                return self._error(400, "缺少 id（可用 ids=1,2,3 / device= / all=1）")
            n = store.delete_snapshots(uid, ids=ids, device=device or None,
                                       all_records=all_flag)
            return self._json({"ok": True, "deleted": n,
                               "hint": "快照已删除；当前书签不受影响，设备下次同步会生成新快照"})

        if path == "/api/v1/devices":
            cid = (q.get("id") or [""])[0]
            if not cid:
                return self._error(400, "缺少 id")
            store.delete_device(uid, cid)
            return self._json({"ok": True})

        if path == "/api/v1/users":
            if user["role"] != "admin":
                return self._error(403, "需要管理员权限")
            try:
                target = int((q.get("id") or ["0"])[0])
            except ValueError:
                return self._error(400, "id 非法")
            if target == uid:
                return self._error(400, "不能删除当前登录的账号")
            victim = store.get_user(target)
            if not victim:
                return self._error(404, "用户不存在")
            if victim["role"] == "admin":
                admins = [u for u in store.list_users() if u["role"] == "admin"]
                if len(admins) <= 1:
                    return self._error(400, "不能删除最后一个管理员")
            store.delete_user(target)
            store.add_log("WARN", "删除用户 %s 及其全部书签数据" % victim["username"],
                          user_id=uid)
            return self._json({"ok": True})

        return self._error(404, "接口不存在")

    def _static(self, path, base="/"):
        web = self.app["web"]
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (web / rel).resolve()
        try:
            if not str(target).startswith(str(web.resolve())) or not target.is_file():
                raise FileNotFoundError
        except (OSError, ValueError):
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        types = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        data = target.read_bytes()
        if rel == "index.html":
            data = data.replace(b"__BASE__", base.encode("utf-8"))
        self._send(200, data, types.get(target.suffix, "application/octet-stream"))


class AppServerMixin:
    """把共享状态与「是否网关通道」标记挂到 server 实例上。"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, app=None, gateway=False, **kwargs):
        self.app = app
        self.gateway = gateway
        super().__init__(*args, **kwargs)


class DirectServer(AppServerMixin, ThreadingHTTPServer):
    """浏览器插件 / 局域网直连通道。"""


class GatewayTcpServer(AppServerMixin, ThreadingHTTPServer):
    """本地回归用：在没有 AF_UNIX 的机器上模拟统一网关通道。"""


if HAS_AF_UNIX:
    class GatewayUnixServer(AppServerMixin, socketserver.ThreadingUnixStreamServer):
        """fnOS 统一网关通道：NAS 校验登录态后把请求转发到此 Unix Socket。"""


def build_servers(app, host, port, gateway_socket=None, gateway_test_port=None):
    """构建所有监听通道，返回 [(server, 描述)]。TCP 直连恒为第一个。"""
    servers = [(DirectServer((host, port), Handler, app=app, gateway=False),
                "TCP 直连 %s:%d" % (host, port))]

    if gateway_socket:
        if not HAS_AF_UNIX:
            log("警告：当前 Python 不支持 AF_UNIX，跳过 Unix Socket（仅 TCP 直连可用）")
        else:
            p = Path(gateway_socket)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                p.unlink()
            srv = GatewayUnixServer(str(p), Handler, app=app, gateway=True)
            try:
                os.chmod(str(p), 0o660)
            except Exception as exc:
                log("设置 socket 权限失败（不影响启动）：%s" % exc)
            servers.append((srv, "Unix Socket %s" % p))

    if gateway_test_port:
        servers.append((GatewayTcpServer((host, int(gateway_test_port)), Handler,
                                         app=app, gateway=True),
                        "TCP 网关模拟 %s:%s" % (host, gateway_test_port)))
    return servers


def main():
    parser = argparse.ArgumentParser(description="Fn收藏夹同步 · 飞牛 OS 服务端")
    parser.add_argument("--host", default=os.environ.get("EB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EB_PORT", "8765")))
    parser.add_argument("--data", default=os.environ.get("EB_DATA_DIR", "./data"))
    parser.add_argument("--config", default=os.environ.get("EB_CONFIG", "./config.json"))
    parser.add_argument("--web", default=os.environ.get("EB_WEB_DIR", str(Path(__file__).parent / "web")))
    parser.add_argument("--gateway-socket", default=os.environ.get("EB_GATEWAY_SOCKET", ""),
                        help="fnOS 统一网关 Unix Socket 路径（${TRIM_APPDEST}/app.sock）")
    parser.add_argument("--gateway-test-port", type=int,
                        default=int(os.environ.get("EB_GATEWAY_TEST_PORT", "0") or 0),
                        help="仅供本地回归：用 TCP 端口模拟网关通道")
    parser.add_argument("--print-setup", action="store_true",
                        help="输出初始化状态 JSON 后退出（诊断用）")
    args = parser.parse_args()

    config = Config(args.config)
    store = Store(args.data)
    if args.print_setup:
        print(json.dumps({"ok": True, "title": APP_TITLE, "version": VERSION,
                          "authMode": AUTH_MODE, "needsAdminSetup": store.user_count() == 0,
                          "userCount": store.user_count()}, ensure_ascii=False))
        return

    web = Path(args.web).resolve()
    if not (web / "index.html").exists():
        log("警告：未找到 Web 界面 %s" % web)

    app = {"config": config, "store": store, "web": web, "version": VERSION,
           "port": args.port, "gatewaySocket": args.gateway_socket}
    servers = build_servers(app, args.host, args.port,
                            args.gateway_socket, args.gateway_test_port)
    log("%s v%s 已启动（认证方式：%s，书签自动去重已开启）"
        % (APP_TITLE, VERSION, AUTH_MODE))
    for _srv, label in servers:
        log("监听通道：%s" % label)
    if app["gatewaySocket"] and HAS_AF_UNIX:
        log("统一网关前缀：%s（内外网均通过飞牛当前域名访问）" % GATEWAY_PREFIX)
    log("数据目录：%s" % Path(args.data).resolve())
    log("配置文件：%s" % Path(args.config).resolve())
    if store.user_count() == 0:
        log("提示：首次打开 Web 界面请注册管理员账号（用户名 + 密码）")

    for srv, _label in servers[1:]:
        threading.Thread(target=srv.serve_forever,
                         kwargs={"poll_interval": 0.5}, daemon=True).start()
    try:
        servers[0][0].serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("正在停止…")
    finally:
        for srv, _label in servers:
            try:
                srv.server_close()
            except Exception:
                pass
        if args.gateway_socket and HAS_AF_UNIX:
            try:
                os.unlink(args.gateway_socket)
            except Exception:
                pass


if __name__ == "__main__":
    main()
