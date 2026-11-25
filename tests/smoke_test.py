#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟测试：自动拉起服务端，注册管理员、按账号登录并模拟插件同步。

    python tests/smoke_test.py

重点验证 v1.4.0：用户名密码认证、SQLite 多用户隔离、书签自动去重、
管理端增删改查、插件地址自动识别与配置复制。
"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "server.py"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (" · " + str(detail)[:160] if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, path, method="GET", body=None, token=None, headers=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), dict(resp.headers)
            except Exception:
                return resp.status, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, raw, dict(e.headers)


def basic(username, password):
    raw = base64.b64encode(("%s:%s" % (username, password)).encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + raw}


def chrome_tree(extra=None):
    nodes = [
        {"id": "11", "title": "腾讯云", "url": "https://cloud.tencent.com/", "dateAdded": 1700000001000},
        {"id": "12", "title": "开发", "dateAdded": 1700000000000, "children": [
            {"id": "121", "title": "GitHub", "url": "https://github.com/", "dateAdded": 1700000002000},
        ]},
    ]
    if extra:
        nodes.extend(extra)
    return [{
        "id": "0", "title": "", "dateAdded": 1700000000000,
        "children": [
            {"id": "1", "title": "收藏夹栏", "dateAdded": 1700000000000, "children": nodes},
            {"id": "2", "title": "其他收藏夹", "dateAdded": 1700000000000, "children": [
                {"id": "21", "title": "飞牛私有云", "url": "https://www.fnnas.com/",
                 "dateAdded": 1700000003000},
            ]},
        ],
    }]


def payload(client_id="smoke-device-0001", tree=None, **kw):
    p = {
        "clientId": client_id, "deviceName": "冒烟测试机",
        "browser": "Microsoft Edge 131.0", "tree": tree if tree is not None else chrome_tree(),
        "bookmarkCount": 3, "clientTime": "2026-11-17T08:00:00+08:00", "reason": "manual",
    }
    p.update(kw)
    return p


def main():
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="eb-smoke-"))
    data, share = tmp / "data", tmp / "share"
    data.mkdir()
    env = dict(os.environ)
    env.update({"EB_SHARE_DIR": str(share), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--host", "127.0.0.1",
         "--data", str(data), "--config", str(tmp / "config.json"),
         "--web", str(ROOT / "server" / "web")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        for _ in range(40):
            try:
                code, _j, _ = req(port, "/api/v1/health")
                if code == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            print("服务未能启动", proc.stderr.read().decode("utf-8", "replace"))
            return 1

        print("\n— 基础连通与初始化状态 —")
        code, j, h = req(port, "/api/v1/health")
        check("健康检查 200", code == 200 and j.get("ok") is True, j)
        check("版本号 1.4.0", j.get("version") == "1.4.0")
        check("健康检查不泄露用户数据", "users" not in j and "token" not in j)
        check("显示名为 Fn收藏夹同步", j.get("title") == "Fn收藏夹同步")
        check("认证方式为用户名密码", j.get("authMode") == "username-password")
        check("首次打开需要注册管理员", j.get("needsAdminSetup") is True)
        check("CORS 头存在", h.get("Access-Control-Allow-Origin") == "*")

        code, body, _ = req(port, "/")
        check("Web 界面可访问且名称正确", code == 200 and "Fn收藏夹同步" in str(body))
        check("Web 界面含注册管理员入口", "registerBox" in str(body))
        check("Web 界面不再出现访问令牌", "领取访问令牌" not in str(body))

        code, _, _ = req(port, "/api/v1/status")
        check("未登录访问被拒绝(401)", code == 401)

        code, j, _ = req(port, "/api/v1/setup/state")
        check("setup/state 提示注册管理员", code == 200 and j.get("needsAdminSetup") is True)
        check("setup/state 给出插件直连地址", bool(j.get("pluginUrls")), j.get("pluginUrls"))
        check("setup/state 不泄露任何令牌", "token" not in j)
        check("setup/state 标明认证方式", j.get("authMode") == "username-password")

        print("\n— 首次注册管理员 —")
        code, _, _ = req(port, "/api/v1/setup/register", method="POST",
                         body={"username": "a", "password": "123456"})
        check("用户名过短被拒绝(400)", code == 400)
        code, _, _ = req(port, "/api/v1/setup/register", method="POST",
                         body={"username": "admin", "password": "123"})
        check("弱密码被拒绝(400)", code == 400)
        code, j, _ = req(port, "/api/v1/setup/register", method="POST",
                         body={"username": "admin", "password": "Admin@123456",
                               "displayName": "老杨"})
        check("注册管理员成功", code == 200 and bool(j.get("token")), j)
        admin = j.get("token", "")
        check("返回角色为 admin", j.get("user", {}).get("role") == "admin")
        check("返回信息不含密码哈希", "password_hash" not in json.dumps(j))
        code, _, _ = req(port, "/api/v1/setup/register", method="POST",
                         body={"username": "hacker", "password": "Hacker@123"})
        check("管理员只能注册一次(403)", code == 403)
        code, j, _ = req(port, "/api/v1/setup/state")
        check("注册后不再需要初始化", j.get("needsAdminSetup") is False and j.get("userCount") == 1)

        print("\n— 登录与鉴权 —")
        code, _, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "admin", "password": "wrong-pass"})
        check("密码错误被拒绝(401)", code == 401)
        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "ADMIN", "password": "Admin@123456"})
        check("用户名大小写不敏感", code == 200 and bool(j.get("token")))
        check("登录返回会话有效期", j.get("sessionDays") == 30)
        code, j, _ = req(port, "/api/v1/auth/me", token=admin)
        check("auth/me 返回当前用户", code == 200 and j["user"]["username"] == "admin")
        code, j, _ = req(port, "/api/v1/status", headers=basic("admin", "Admin@123456"))
        check("HTTP Basic（用户名密码直连）可用", code == 200 and j.get("ok") is True)
        code, _, _ = req(port, "/api/v1/status", headers=basic("admin", "bad"))
        check("Basic 密码错误被拒绝", code == 401)
        code, j, _ = req(port, "/api/v1/token/check", method="POST", body={}, token=admin)
        check("旧版令牌接口提示升级(410)", code == 410 and "1.4.0" in str(j), j)
        code, j, _ = req(port, "/api/v1/setup/claim", method="POST", body={})
        check("旧版领取令牌接口提示升级(410)", code == 410 and "用户名" in str(j), j)

        print("\n— 插件地址自动识别（无需设置界面） —")
        code, j, _ = req(port, "/api/v1/plugin-config", token=admin)
        check("插件配置接口 200", code == 200 and j.get("ok") is True, j)
        check("自动给出主地址", bool(j.get("serverUrl")), j.get("serverUrl"))
        check("配置文本是合法 JSON", isinstance(json.loads(j.get("configText", "{}")), dict))
        cfg_obj = json.loads(j.get("configText", "{}"))
        check("配置含主地址与用户名",
              cfg_obj.get("serverUrl") == j.get("serverUrl") and cfg_obj.get("username") == "admin")
        check("配置不含密码", "password" not in cfg_obj and "token" not in cfg_obj)
        check("直连访问时不下发 /app/ 网关地址",
              all("/app/edge-bookmarks" not in str(a.get("url")) for a in j.get("addresses", [])))
        check("标明为自动识别", j.get("autoDetected") is True)
        check("地址列表含推荐标记", any(a.get("primary") for a in j.get("addresses", [])))
        code, _, _ = req(port, "/api/v1/plugin-config")
        check("插件配置接口需要登录(401)", code == 401)
        code, j2, _ = req(port, "/api/v1/plugin-config",
                          headers={"X-Forwarded-Prefix": "/eb-api",
                                   "X-Forwarded-Proto": "https",
                                   "X-Forwarded-Host": "nas.example.com:8443"}, token=admin)
        check("反代访问时自动给出统一登录地址 + 路径",
              j2.get("serverUrl") == "https://nas.example.com:8443/eb-api", j2.get("serverUrl"))
        code, j3, _ = req(port, "/api/v1/plugin-config",
                          headers={"Host": 'evil"><script>.com'}, token=admin)
        check("恶意 Host 头不会进入配置", "<script>" not in str(j3.get("configText")))

        # 回归：真实用户经飞牛统一网关打开 https://<域名>:9443/app/edge-bookmarks，
        # NAS 上并没有 /eb-api 反代（实测 404）。主地址必须与地址栏一致。
        code, j4, _ = req(port, "/app/edge-bookmarks/api/v1/plugin-config",
                          headers={"Host": "nas.example.com:9443",
                                   "X-Forwarded-Proto": "https",
                                   "X-Forwarded-Host": "nas.example.com:9443"}, token=admin)
        check("网关路径可正常路由到插件配置接口", code == 200, code)
        check("网关访问时主地址 = 当前管理页地址",
              j4.get("serverUrl") == "https://nas.example.com:9443/app/edge-bookmarks",
              j4.get("serverUrl"))
        check("网关场景不再下发不存在的 /eb-api",
              "/eb-api" not in str(j4.get("configText", "")), j4.get("configText"))
        check("返回 pageUrl 与网关标记",
              j4.get("pageUrl") == j4.get("serverUrl") and j4.get("isGatewayPage") is True)
        check("网关地址出现在候选列表中",
              any("/app/edge-bookmarks" in str(a.get("url")) for a in j4.get("addresses", [])))
        code, _, h4 = req(port, "/api/v1/health",
                          headers={"Origin": "chrome-extension://abcdefg"})
        check("CORS 回显具体 Origin",
              h4.get("Access-Control-Allow-Origin") == "chrome-extension://abcdefg",
              h4.get("Access-Control-Allow-Origin"))
        check("CORS 允许携带凭证",
              str(h4.get("Access-Control-Allow-Credentials", "")).lower() == "true")
        code, _, h5 = req(port, "/api/v1/health")
        check("无 Origin 时仍放开通配 CORS",
              h5.get("Access-Control-Allow-Origin") == "*")

        print("\n— 管理员同步与查询 —")
        code, j, _ = req(port, "/api/v1/sync", method="POST", body=payload(), token=admin)
        check("同步 200", code == 200 and j.get("ok") is True, j)
        check("书签计数正确(3)", j.get("bookmarkCount") == 3)
        check("返回服务端总数", j.get("serverTotal") == 3)
        check("返回设备数", j.get("devices") == 1)
        code, _, _ = req(port, "/api/v1/sync", method="POST", body={"tree": []}, token=admin)
        check("缺少 clientId 返回 400", code == 400)

        code, j, _ = req(port, "/api/v1/status", token=admin)
        check("状态统计正确", j.get("devices") == 1 and j.get("bookmarks") == 3, j)
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        check("聚合书签总数 3", j.get("total") == 3)
        check("生成分组树", len(j.get("tree", [])) == 1)
        titles = [str(x) for x in j.get("items", [])]
        check("保留文件夹路径", any("收藏夹栏" in t for t in titles))
        code, j, _ = req(port, "/api/v1/search?q=GitHub", token=admin)
        check("搜索命中 GitHub", len(j.get("items", [])) == 1, j)
        code, body, h = req(port, "/api/v1/export?format=html", token=admin)
        check("导出 Netscape HTML", code == 200 and "NETSCAPE-Bookmark-file-1" in str(body))
        check("HTML 含书签网址", "https://github.com/" in str(body))
        check("导出带下载文件名", "attachment" in h.get("Content-Disposition", ""))
        code, j, _ = req(port, "/api/v1/export?format=json", token=admin)
        check("导出 JSON", code == 200 and isinstance(j, dict) and len(j.get("items", [])) == 3)
        code, j, _ = req(port, "/api/v1/devices", token=admin)
        check("设备列表", len(j.get("items", [])) == 1 and j["items"][0]["name"] == "冒烟测试机")
        code, j, _ = req(port, "/api/v1/snapshots", token=admin)
        check("生成历史快照", len(j.get("items", [])) == 1)
        admin_snap = j["items"][0]["id"]
        code, j, _ = req(port, "/api/v1/snapshot?id=%d" % admin_snap, token=admin)
        check("快照可读取", code == 200 and isinstance(j.get("tree"), list))
        code, j, _ = req(port, "/api/v1/sync", method="POST",
                         body=payload(client_id="smoke-device-0002", deviceName="第二台电脑"), token=admin)
        check("第二台设备同步成功", code == 200 and j.get("devices") == 2)
        code, j, _ = req(port, "/api/v1/incoming?exclude=smoke-device-0001", token=admin)
        check("拉取其他设备书签", j.get("total") == 3, j.get("total"))

        print("\n— 用户管理 —")
        code, j, _ = req(port, "/api/v1/users", token=admin)
        check("管理员可查看用户列表", code == 200 and len(j.get("items", [])) == 1)
        check("用户列表不含密码哈希", "password_hash" not in json.dumps(j))
        code, _, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "xiaoli", "password": "Li@123456"}, token=admin)
        check("管理员新增用户", code == 200)
        code, _, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "xiaoli", "password": "Li@123456"}, token=admin)
        check("重名用户被拒绝(400)", code == 400)
        code, _, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "weak", "password": "123"}, token=admin)
        check("弱密码用户被拒绝(400)", code == 400)
        code, j, _ = req(port, "/api/v1/users", token=admin)
        check("用户数变为 2", len(j.get("items", [])) == 2)
        role_map = {u["username"]: u["role"] for u in j["items"]}
        check("新用户默认为普通用户", role_map.get("xiaoli") == "user")

        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "xiaoli", "password": "Li@123456"})
        check("普通用户登录成功", code == 200 and bool(j.get("token")))
        user = j.get("token", "")

        print("\n— 多用户数据隔离（关键） —")
        code, j, _ = req(port, "/api/v1/status", token=user)
        check("普通用户初始数据为空", j.get("devices") == 0 and j.get("bookmarks") == 0, j)
        code, _, _ = req(port, "/api/v1/users", token=user)
        check("普通用户不能查看用户列表(403)", code == 403)
        code, _, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "mallory", "password": "Mal@123456"}, token=user)
        check("普通用户不能新增用户(403)", code == 403)
        code, _, _ = req(port, "/api/v1/users?id=1", method="DELETE", token=user)
        check("普通用户不能删除用户(403)", code == 403)
        code, _, _ = req(port, "/api/v1/snapshot?id=%d" % admin_snap, token=user)
        check("普通用户读不到管理员快照(404)", code == 404)
        code, j, _ = req(port, "/api/v1/bookmarks", token=user)
        check("普通用户看不到管理员书签", j.get("total") == 0)

        own_tree = [{
            "id": "0", "title": "", "children": [{
                "id": "1", "title": "小李的收藏", "children": [
                    {"id": "11", "title": "小李的主页", "url": "https://example.com/xiaoli"},
                    {"id": "12", "title": "小李的文档", "url": "https://example.com/docs"},
                ],
            }],
        }]
        code, j, _ = req(port, "/api/v1/sync", method="POST",
                         body=payload(client_id="xiaoli-device", tree=own_tree,
                                      deviceName="小李的电脑"), token=user)
        check("普通用户同步自己的书签", code == 200 and j.get("bookmarkCount") == 2, j)
        code, j, _ = req(port, "/api/v1/bookmarks", token=user)
        check("普通用户只看到自己的 2 条", j.get("total") == 2)
        check("普通用户看不到 github.com",
              all("github.com" not in str(i.get("url")) for i in j["items"]))
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        check("普通用户数据不污染管理员(仍为 3 条)", j.get("total") == 3, j.get("total"))
        code, j, _ = req(port, "/api/v1/search?q=xiaoli", token=admin)
        check("管理员搜不到普通用户书签", len(j.get("items", [])) == 0)
        code, j, _ = req(port, "/api/v1/search?q=github", token=user)
        check("普通用户搜不到管理员书签", len(j.get("items", [])) == 0)
        code, j, _ = req(port, "/api/v1/incoming", token=user)
        check("普通用户 incoming 只含自己的数据", j.get("total") == 2, j.get("total"))
        code, j, _ = req(port, "/api/v1/devices", token=user)
        check("普通用户设备列表只有自己的", len(j.get("items", [])) == 1)
        code, _, _ = req(port, "/api/v1/devices?id=smoke-device-0001", method="DELETE", token=user)
        check("普通用户删管理员设备返回成功但无效", code == 200)
        code, j, _ = req(port, "/api/v1/devices", token=admin)
        check("管理员设备未被跨用户删除", len(j.get("items", [])) == 2, len(j.get("items", [])))

        print("\n— 密码维护 —")
        code, _, _ = req(port, "/api/v1/users/password", method="POST",
                         body={"id": 2, "password": "NewLi@654321"}, token=admin)
        check("管理员重置用户密码", code == 200)
        code, _, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "xiaoli", "password": "Li@123456"})
        check("旧密码立即失效", code == 401)
        code, _, _ = req(port, "/api/v1/status", token=user)
        check("重置密码后旧会话失效", code == 401)
        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "xiaoli", "password": "NewLi@654321"})
        check("新密码可登录", code == 200 and bool(j.get("token")))
        user = j.get("token", "")
        code, _, _ = req(port, "/api/v1/auth/password", method="POST",
                         body={"oldPassword": "wrong", "newPassword": "Nope@12345"}, token=user)
        check("原密码错误不能改密(403)", code == 403)
        code, _, _ = req(port, "/api/v1/auth/password", method="POST",
                         body={"oldPassword": "NewLi@654321", "newPassword": "Final@98765"}, token=user)
        check("用户可修改自己的密码", code == 200)

        print("\n— 删除用户 —")
        # 改密会让该用户旧会话全部失效，这里用新密码重新登录
        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "xiaoli", "password": "Final@98765"})
        check("改密后用新密码可重新登录", code == 200 and bool(j.get("token")))
        user = j.get("token", "")
        code, _, _ = req(port, "/api/v1/users?id=2", method="DELETE", token=user)
        check("普通用户不能删除用户(403)", code == 403)
        code, _, _ = req(port, "/api/v1/users?id=1", method="DELETE", token=admin)
        check("不能删除当前登录账号(400)", code == 400)
        code, _, _ = req(port, "/api/v1/users?id=2", method="DELETE", token=admin)
        check("管理员删除用户", code == 200)
        code, j, _ = req(port, "/api/v1/users", token=admin)
        check("用户数回到 1", len(j.get("items", [])) == 1)
        code, _, _ = req(port, "/api/v1/status", token=user)
        check("被删用户的会话立即失效", code == 401)
        code, j, _ = req(port, "/api/v1/status", token=admin)
        check("管理员去重后数据完好", j.get("bookmarks") == 3, j)
        check("状态给出原始来源数", j.get("rawBookmarks") == 6, j.get("rawBookmarks"))
        check("状态给出去重数量", j.get("duplicatesRemoved") == 3, j.get("duplicatesRemoved"))

        print("\n— 书签自动去重（同一网址只保留一条） —")
        dup_tree = [{
            "id": "0", "title": "", "children": [{
                "id": "1", "title": "收藏夹栏", "children": [
                    {"id": "d1", "title": "腾讯云", "url": "https://cloud.tencent.com/"},
                    {"id": "d2", "title": "腾讯云重复", "url": "http://cloud.tencent.com"},
                    {"id": "d3", "title": "带锚点", "url": "https://cloud.tencent.com/#top"},
                    {"id": "d4", "title": "带跟踪参数",
                     "url": "https://cloud.tencent.com/?utm_source=edge"},
                    {"id": "d5", "title": "GitHub", "url": "https://github.com/"},
                ],
            }],
        }]
        code, j, _ = req(port, "/api/v1/sync", method="POST",
                         body=payload(client_id="dedup-device", tree=dup_tree,
                                      deviceName="去重测试机"), token=admin)
        check("单次上传内部去重(5→2)", code == 200 and j.get("uniqueCount") == 2, j)
        check("同步响应给出去重条数", j.get("duplicatesRemoved") == 5, j.get("duplicatesRemoved"))
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        urls = [i.get("url") for i in j["items"]]
        check("云端只保留 3 条唯一书签", j.get("total") == 3, j.get("total"))
        check("不保留重复网址", len(urls) == len(set(urls)))
        code, j, _ = req(port, "/api/v1/export?format=json", token=admin)
        check("导出也是去重后的数据", len(j.get("items", [])) == 3)
        code, j, _ = req(port, "/api/v1/devices?id=dedup-device", method="DELETE", token=admin)
        check("清理去重测试设备", code == 200)
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        check("删除设备后恢复原有 3 条", j.get("total") == 3, j.get("total"))

        print("\n— 管理端书签增删改查 —")
        code, j, _ = req(port, "/api/v1/bookmarks", method="POST",
                         body={"title": "手动添加", "url": "https://manual.example.com/",
                               "path": ["收藏夹栏", "手动"]}, token=admin)
        check("新增书签成功", code == 200 and bool(j.get("item", {}).get("id")), j)
        bid = j["item"]["id"]
        check("新增书签来源标记为 manual", j["item"].get("source") == "manual")
        check("新增书签路径被保存", j["item"].get("path") == ["收藏夹栏", "手动"])
        code, j, _ = req(port, "/api/v1/bookmarks", method="POST",
                         body={"title": "重复", "url": "http://manual.example.com"}, token=admin)
        check("重复网址被拒绝(409)", code == 409, j)
        code, j, _ = req(port, "/api/v1/bookmarks", method="POST",
                         body={"title": "坏地址", "url": "javascript:alert(1)"}, token=admin)
        check("非法网址被拒绝(400)", code == 400, j)
        code, j, _ = req(port, "/api/v1/bookmarks/item?id=%d" % bid, token=admin)
        check("按 id 查询单条", code == 200 and j["item"]["title"] == "手动添加")
        code, j, _ = req(port, "/api/v1/bookmarks?limit=2&offset=0", token=admin)
        check("分页 limit 生效", j.get("count") == 2 and j.get("total") == 4, j)
        code, j, _ = req(port, "/api/v1/bookmarks?q=manual", token=admin)
        check("列表按关键字过滤", j.get("total") == 1, j.get("total"))
        code, j, _ = req(port, "/api/v1/bookmarks", method="PUT",
                         body={"id": bid, "title": "已改名",
                               "url": "https://manual.example.com/new"}, token=admin)
        check("编辑书签（标题+网址）成功", code == 200 and j["item"]["title"] == "已改名", j)
        check("编辑后转为手动维护", j["item"].get("source") == "manual")
        code, j, _ = req(port, "/api/v1/bookmarks/trash", token=admin)
        check("改网址后旧网址进入回收站", len(j.get("items", [])) >= 1, j.get("items"))
        code, j, _ = req(port, "/api/v1/bookmarks?id=%d" % bid, method="DELETE", token=admin)
        check("删除书签成功", code == 200 and j.get("deleted") == [bid], j)
        code, j, _ = req(port, "/api/v1/bookmarks/item?id=%d" % bid, token=admin)
        check("删除后查询返回 404", code == 404)
        code, j, _ = req(port, "/api/v1/bookmarks/trash", token=admin)
        trash_items = j.get("items", [])
        check("回收站含被删书签", any("manual.example.com" in str(t.get("url")) for t in trash_items))
        target_key = next((t["url_key"] for t in trash_items
                           if "manual.example.com/new" in str(t.get("url"))), "")
        check("回收站记录带 url_key", bool(target_key))
        code, _, _ = req(port, "/api/v1/bookmarks/restore", method="POST",
                         body={"key": target_key}, token=admin)
        check("从回收站恢复", code == 200)
        code, j, _ = req(port, "/api/v1/bookmarks?q=manual.example.com/new", token=admin)
        check("恢复后重新可查", j.get("total") == 1, j.get("total"))
        restored_id = j["items"][0]["id"]
        code, j, _ = req(port, "/api/v1/bookmarks?ids=%d" % restored_id, method="DELETE", token=admin)
        check("批量删除接口可用", code == 200 and j.get("deleted") == [restored_id], j)
        code, j, _ = req(port, "/api/v1/bookmarks/trash?key=%s" % target_key,
                         method="DELETE", token=admin)
        check("彻底移除回收站记录", code == 200)

        # 重新建一个普通用户，验证书签 CRUD 也严格按 user_id 隔离
        code, _, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "isolated", "password": "Iso@123456"}, token=admin)
        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "isolated", "password": "Iso@123456"})
        iso = j.get("token", "")
        code, _, _ = req(port, "/api/v1/bookmarks/item?id=1", token=iso)
        check("普通用户读不到管理员书签(404)", code == 404)
        code, _, _ = req(port, "/api/v1/bookmarks", method="PUT",
                         body={"id": 1, "title": "越权"}, token=iso)
        check("普通用户不能改管理员书签(404)", code == 404)
        code, delj, _ = req(port, "/api/v1/bookmarks?id=1", method="DELETE", token=iso)
        check("普通用户删除他人书签不生效", code == 200 and delj.get("deleted") == [], delj)
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        check("管理员书签未被越权删除", j.get("total") == 3, j.get("total"))


        print("\n— 设备管理页与只读书签浏览 —")

        def leaves(nodes):
            out = []
            for n in nodes or []:
                if n.get("url"):
                    out.append(n)
                else:
                    out.extend(leaves(n.get("children")))
            return out

        code, j, _ = req(port, "/api/v1/devices/tree?clientId=smoke-device-0001", token=admin)
        check("设备详情树 200", code == 200 and j.get("ok") is True, code)
        check("设备详情带设备名", (j.get("device") or {}).get("name") == "冒烟测试机")
        check("设备详情只含本设备 3 条", j.get("total") == 3, j.get("total"))
        dev_leaves = leaves(j.get("tree"))
        check("设备树保留目录结构", any(n.get("title") == "收藏夹栏" for n in j.get("tree", [])))
        check("设备树叶子上带 id 与网址",
              len(dev_leaves) == 3 and all(n.get("id") and n.get("url") for n in dev_leaves))
        code, _, _ = req(port, "/api/v1/devices/tree?clientId=not-exist", token=admin)
        check("未知设备返回 404", code == 404)
        code, _, _ = req(port, "/api/v1/devices/tree?clientId=smoke-device-0001", token=iso)
        check("普通用户读不到他人设备树(404)", code == 404)
        code, _, _ = req(port, "/api/v1/devices/tree", token=admin)
        check("缺少 clientId 返回 400", code == 400)

        code, j, _ = req(port, "/api/v1/browse", token=admin)
        check("书签浏览 200", code == 200 and j.get("ok") is True, code)
        check("浏览返回我的全部设备标签", len(j.get("devices", [])) == 2, j.get("devices"))
        check("设备标签带各自条数",
              all(d.get("count") == 3 for d in j.get("devices", [])), j.get("devices"))
        all_leaves = leaves(j.get("tree"))
        check("浏览树共 3 条（跨设备去重）", len(all_leaves) == 3, len(all_leaves))
        check("书签标记出所属设备",
              all(n.get("devices") and len(n["devices"]) == 2 for n in all_leaves),
              [n.get("devices") for n in all_leaves])

        code, j, _ = req(port, "/api/v1/browse?device=smoke-device-0002", token=admin)
        only_leaves = leaves(j.get("tree"))
        check("按设备筛选浏览", code == 200 and len(only_leaves) == 3)
        check("筛选后标记为该设备",
              all(n.get("devices") == ["第二台电脑"] for n in only_leaves),
              [n.get("devices") for n in only_leaves])
        code, _, _ = req(port, "/api/v1/browse?device=not-exist", token=admin)
        check("浏览未知设备返回 404", code == 404)

        move_id = next(n["id"] for n in dev_leaves if n.get("title") == "GitHub")
        code, j, _ = req(port, "/api/v1/bookmarks/batch", method="POST",
                         body={"ids": [move_id], "path": "工作/归档"}, token=admin)
        check("批量修改文件夹", code == 200 and j.get("moved") == 1, j)
        code, j, _ = req(port, "/api/v1/devices/tree?clientId=smoke-device-0001", token=admin)
        moved = next(n for n in leaves(j.get("tree")) if n.get("title") == "GitHub")
        check("修改后设备树路径已更新",
              any(f.get("title") == "工作" for f in j.get("tree", [])))
        check("修改后书签转为手动维护", moved.get("source") == "manual", moved)
        code, _, _ = req(port, "/api/v1/bookmarks/batch", method="POST",
                         body={"ids": [move_id]}, token=admin)
        check("批量修改缺少 path 返回 400", code == 400)
        code, _, _ = req(port, "/api/v1/bookmarks/batch", method="POST",
                         body={"ids": [], "path": "x"}, token=admin)
        check("批量修改空 ids 返回 400", code == 400)

        code, j, _ = req(port, "/api/v1/bookmarks/trash?all=1", method="DELETE", token=admin)
        check("回收站一键清空", code == 200 and "cleared" in j, j)
        code, j, _ = req(port, "/api/v1/bookmarks/trash", token=admin)
        check("清空后回收站为空", len(j.get("items", [])) == 0, j.get("items"))

        print("\n— v1.3.1：按设备拉取 / 快照管理 —")

        # 再同步两次，凑出足够的快照用于单份 / 批量删除测试
        for _ in range(2):
            req(port, "/api/v1/sync", method="POST", body=payload(), token=admin)

        code, j, _ = req(port, "/api/v1/incoming?exclude=smoke-device-0001&device=smoke-device-0002",
                         token=admin)
        check("按设备拉取返回 200", code == 200 and j.get("ok") is True, code)
        check("按设备拉取只含该设备的 3 条", j.get("total") == 3, j.get("total"))
        check("响应回显所选设备", j.get("device") == "smoke-device-0002", j.get("device"))
        check("响应回显设备显示名", j.get("deviceName") == "第二台电脑", j.get("deviceName"))
        hosts = " ".join(str(i.get("url")) for i in j.get("items", []))
        check("按设备拉取命中该设备的网址",
              all(h in hosts for h in ("cloud.tencent.com", "github.com", "fnnas.com")), hosts)
        code, _, _ = req(port, "/api/v1/incoming?device=not-exist", token=admin)
        check("按设备拉取未知设备返回 404", code == 404)
        code, j, _ = req(port, "/api/v1/incoming?exclude=smoke-device-0001", token=admin)
        check("不带 device 仍是全量拉取", j.get("total") == 3, j.get("total"))
        code, _, _ = req(port, "/api/v1/incoming?device=smoke-device-0001", token=iso)
        check("普通用户不能按管理员设备拉取(404)", code == 404)

        code, j, _ = req(port, "/api/v1/snapshots/manage", token=admin)
        check("快照管理列表 200", code == 200 and j.get("ok") is True, code)
        check("快照管理返回总数", j.get("total", 0) >= 3, j.get("total"))
        check("快照条目带设备名与体积",
              all(s.get("deviceName") and isinstance(s.get("size"), int)
                  for s in j.get("items", [])), j.get("items", [])[:2])
        check("快照管理返回设备筛选标签", len(j.get("devices", [])) == 2, j.get("devices"))
        code, j2, _ = req(port, "/api/v1/snapshots/manage?device=smoke-device-0001", token=admin)
        check("按设备筛选快照", code == 200 and
              all(s.get("client_id") == "smoke-device-0001" for s in j2.get("items", [])))
        code, j3, _ = req(port, "/api/v1/snapshots/manage?limit=1&offset=1", token=admin)
        check("快照分页 limit/offset", code == 200 and len(j3.get("items", [])) == 1,
              len(j3.get("items", [])))
        code, _, _ = req(port, "/api/v1/snapshots/manage?limit=abc", token=admin)
        check("快照分页非法参数返回 400", code == 400)

        all_ids = [s["id"] for s in j.get("items", [])]
        code, snap, _ = req(port, "/api/v1/snapshot?id=%d" % all_ids[0], token=admin)
        check("可读取快照目录树", code == 200 and isinstance(snap.get("tree"), list))
        code, _, _ = req(port, "/api/v1/snapshot?id=%d" % all_ids[0], token=iso)
        check("跨用户读快照返回 404", code == 404)

        code, d1, _ = req(port, "/api/v1/snapshot?id=%d" % all_ids[0],
                          method="DELETE", token=admin)
        check("删除单份快照", code == 200 and d1.get("deleted") == 1, d1)
        code, _, _ = req(port, "/api/v1/snapshot?id=%d" % all_ids[0], token=admin)
        check("删除后该快照不可读(404)", code == 404)
        code, d2, _ = req(port, "/api/v1/snapshot?ids=%s" %
                          ",".join(str(x) for x in all_ids[1:3]),
                          method="DELETE", token=admin)
        check("批量删除快照", code == 200 and d2.get("deleted") == 2, d2)
        code, _, _ = req(port, "/api/v1/snapshot", method="DELETE", token=admin)
        check("删除快照缺参数返回 400", code == 400)
        code, j4, _ = req(port, "/api/v1/snapshots/manage", token=admin)
        check("删除后快照总数减少 3", j4.get("total") == j.get("total") - 3,
              (j.get("total"), j4.get("total")))
        code, d3, _ = req(port, "/api/v1/snapshot?all=1", method="DELETE", token=iso)
        check("普通用户清空快照不影响管理员", code == 200 and d3.get("deleted") == 0, d3)
        code, j5, _ = req(port, "/api/v1/snapshots/manage", token=admin)
        check("管理员快照数未被跨用户清空", j5.get("total") == j4.get("total"),
              (j4.get("total"), j5.get("total")))

        print("\n— v1.3.2：HTML / CSV / JSON 三种导出 —")

        # 再同步一次，确保至少有一份可导出的快照
        req(port, "/api/v1/sync", method="POST", body=payload(), token=admin)

        code, csv_body, ch = req(port, "/api/v1/export?format=csv", token=admin)
        check("导出 CSV 返回 200", code == 200, code)
        check("CSV Content-Type 正确", "text/csv" in ch.get("Content-Type", ""),
              ch.get("Content-Type"))
        check("CSV 带 UTF-8 BOM", str(csv_body).startswith("\ufeff"))
        check("CSV 含中文表头", "标题" in str(csv_body) and "网址" in str(csv_body))
        check("CSV 含去重后书签网址", "github.com" in str(csv_body))
        check("CSV 保留原始目录", "收藏夹栏" in str(csv_body))
        check("CSV 带 .csv 下载文件名", ".csv" in ch.get("Content-Disposition", ""))
        code, empty_csv, _ = req(port, "/api/v1/export?format=csv&device=not-exist", token=admin)
        check("CSV 按设备过滤生效", code == 200 and "github.com" not in str(empty_csv))
        code, html2, _ = req(port, "/api/v1/export?format=html", token=admin)
        check("导出 HTML 仍可用(回归)", code == 200 and "NETSCAPE-Bookmark-file-1" in str(html2))
        code, js2, _ = req(port, "/api/v1/export?format=json", token=admin)
        check("导出 JSON 仍可用(回归)", code == 200 and isinstance(js2, dict)
              and len(js2.get("items", [])) >= 1)
        code, _, _ = req(port, "/api/v1/export?format=xml", token=admin)
        check("export 非法格式返回 400", code == 400)
        code, _, _ = req(port, "/api/v1/export?format=csv")
        check("未登录导出被拒 401", code == 401)

        code, sm, _ = req(port, "/api/v1/snapshots/manage", token=admin)
        snap_id = (sm.get("items") or [{}])[0].get("id")
        check("取得可用快照 id", bool(snap_id), snap_id)
        code, preview, _ = req(port, "/api/v1/snapshot?id=%d" % snap_id, token=admin)
        check("不带 format 仍是预览 JSON(回归)", code == 200 and isinstance(preview.get("tree"), list))
        code, s_html, sh = req(port, "/api/v1/snapshot?id=%d&format=html" % snap_id, token=admin)
        check("快照 HTML 200 且为 Netscape 格式",
              code == 200 and "NETSCAPE-Bookmark-file-1" in str(s_html))
        check("快照 HTML 含网址", "github.com" in str(s_html))
        check("快照 HTML Content-Type / 文件名",
              "text/html" in sh.get("Content-Type", "") and ".html" in sh.get("Content-Disposition", ""))
        code, s_csv, sc = req(port, "/api/v1/snapshot?id=%d&format=csv" % snap_id, token=admin)
        check("快照 CSV 200 且带 BOM", code == 200 and str(s_csv).startswith("\ufeff"))
        check("快照 CSV 含表头与网址", "网址" in str(s_csv) and "github.com" in str(s_csv))
        check("快照 CSV 保留目录层级", "收藏夹栏" in str(s_csv))
        check("快照 CSV 文件名", ".csv" in sc.get("Content-Disposition", ""))
        code, s_json, sj = req(port, "/api/v1/snapshot?id=%d&format=json" % snap_id, token=admin)
        check("快照 JSON 附件含完整树", code == 200 and isinstance(s_json, dict)
              and isinstance(s_json.get("tree"), list))
        check("快照 JSON 文件名", ".json" in sj.get("Content-Disposition", ""))
        code, _, _ = req(port, "/api/v1/snapshot?id=%d&format=xml" % snap_id, token=admin)
        check("快照非法格式返回 400", code == 400)
        code, _, _ = req(port, "/api/v1/snapshot?id=999999&format=csv", token=admin)
        check("快照不存在返回 404", code == 404)
        code, _, _ = req(port, "/api/v1/snapshot?id=%d&format=csv" % snap_id, token=iso)
        check("跨用户下载快照返回 404", code == 404)

        print("\n— CORS 预检 —")
        r = urllib.request.Request("http://127.0.0.1:%d/api/v1/sync" % port, method="OPTIONS",
                                   headers={"Origin": "chrome-extension://abcdef",
                                            "Access-Control-Request-Method": "POST",
                                            "Access-Control-Request-Headers": "authorization,content-type"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            check("OPTIONS 预检 204", resp.status == 204)
            check("预检允许 Authorization", "Authorization" in resp.headers.get("Access-Control-Allow-Headers", ""))

        print("\n— 自动备份 —")
        own = data / "exports" / "bookmarks-latest.html"
        for _ in range(20):
            if (share / "bookmarks-latest.html").exists() and own.exists():
                break
            time.sleep(0.2)
        check("写入应用自有数据目录", own.exists())
        check("写入可选共享目录", (share / "bookmarks-latest.html").exists())
        if own.exists():
            text = own.read_text(encoding="utf-8")
            check("备份内容含书签", "github.com" in text or "example.com" in text)
        check("SQLite 数据库文件已生成", (data / "bookmarks.db").exists())

        print("\n— 网关 / 反代前缀 —")
        P = "/app/edge-bookmarks"
        code, body, _ = req(port, P + "/")
        check("前缀首页 200", code == 200 and "Fn收藏夹同步" in str(body))
        check("注入网关 base", '<base href="/app/edge-bookmarks/">' in str(body))
        check("占位符已全部替换", "__BASE__" not in str(body))
        code, body, _ = req(port, P)
        check("无尾斜杠前缀也返回首页", code == 200 and '<base href="/app/edge-bookmarks/">' in str(body))
        code, j, _ = req(port, P + "/api/v1/health")
        check("前缀健康检查 200", code == 200 and j.get("ok") is True, j)
        code, j, _ = req(port, P + "/api/v1/status", token=admin)
        check("前缀鉴权接口可用", code == 200 and j.get("devices") == 2)
        code, body, _ = req(port, P + "/app.js")
        check("前缀静态资源可访问", code == 200 and "function" in str(body))
        code, body, _ = req(port, "/")
        check("直连首页 base 为根", code == 200 and '<base href="/">' in str(body))
        code, _, _ = req(port, "/eb-api/api/v1/status")
        check("反代路径下未登录仍被拒绝", code == 401)
        code, j, _ = req(port, "/eb-api/api/v1/status", token=admin)
        check("反代路径下带会话可访问", code == 200 and j.get("ok") is True)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
