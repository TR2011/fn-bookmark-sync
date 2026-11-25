#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一网关 / 反代前缀 / 多用户鉴权专项回归测试。

本机 Windows 的 Python 通常没有 AF_UNIX，因此服务端提供 --gateway-test-port：
用第二个 TCP 端口标记成「网关通道」，在不需要 Unix Socket 的环境里完整验证
前缀路由、<base> 注入、身份头防伪与账号体系。

    python tests/gateway_test.py
"""
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
PREFIX = "/app/edge-bookmarks"
PASS, FAIL = [], []

ADMIN_HEADERS = {"X-Trim-Userid": "1000", "X-Trim-Username": "admin", "X-Trim-Isadmin": "true"}
USER_HEADERS = {"X-Trim-Userid": "1001", "X-Trim-Username": "yang", "X-Trim-Isadmin": "false"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name +
          (" · " + str(detail)[:160] if detail else ""))


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


def wait_ready(port, proc, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/v1/health" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def static_checks():
    print("\n— FPK 桌面入口与命名（v1.3.0 单图标） —")
    cfg = json.loads((ROOT / "fpk/app/ui/config").read_text(encoding="utf-8"))[".url"]
    check("桌面只保留一个图标", len(cfg) == 1, list(cfg.keys()))
    main_entry = cfg["edge-bookmarks.main"]
    check("图标名称为 Fn收藏夹同步", main_entry.get("title") == "Fn收藏夹同步")
    check("点开自动弹出新页面(type=url)", main_entry.get("type") == "url")
    check("声明 gatewayPrefix", main_entry.get("gatewayPrefix") == PREFIX)
    check("声明 gatewaySocket", main_entry.get("gatewaySocket") == "app.sock")
    check("protocol 置空", main_entry.get("protocol") == "")
    check("不声明独立 port", "port" not in main_entry)
    check("入口 url 与前缀一致", main_entry.get("url") == PREFIX)
    check("移除内嵌与直连兜底入口",
          "edge-bookmarks.newwin" not in cfg and "edge-bookmarks.direct" not in cfg)

    manifest = (ROOT / "fpk/manifest").read_text(encoding="utf-8")
    check("manifest 版本 1.4.0", "version=1.4.0" in manifest)
    check("manifest 显示名 Fn收藏夹同步", "display_name=Fn收藏夹同步" in manifest)
    check("desktop_applaunchname 指向唯一入口",
          "desktop_applaunchname=edge-bookmarks.main" in manifest)
    check("保留 service_port 给插件直连", "service_port=8765" in manifest)
    check("未声明 install_dep_apps", "install_dep_apps" not in manifest)

    cmd = (ROOT / "fpk/cmd/main").read_text(encoding="utf-8")
    check("cmd/main 不再生成 token.txt", "--print-token" not in cmd)
    check("cmd/main 清理旧令牌文件", "rm -f \"$ETC/token.txt\"" in cmd)
    check("cmd/main 导出 EB_GATEWAY_SOCKET", "export EB_GATEWAY_SOCKET" in cmd)
    check("cmd/main 导出 EB_GATEWAY_PREFIX", "export EB_GATEWAY_PREFIX" in cmd)
    check("cmd/main 停止时清理 socket", cmd.count("rm -f \"$GATEWAY_SOCKET\"") >= 2)

    src = (ROOT / "server/server.py").read_text(encoding="utf-8")
    check("服务端版本 1.4.0", 'VERSION = "1.4.0"' in src)
    check("网址规范化函数", "def normalize_url(" in src and "def url_key(" in src)
    check("书签来源表（去重）", "CREATE TABLE IF NOT EXISTS bookmark_sources" in src)
    check("回收站表（删除后跳过同步）", "CREATE TABLE IF NOT EXISTS tombstones" in src)
    check("书签唯一索引", "uq_bm_user_key" in src)
    check("插件配置自动识别接口", 'path == "/api/v1/plugin-config"' in src)
    check("书签新增接口", 'path == "/api/v1/bookmarks"' in src)
    check("回收站接口", 'path == "/api/v1/bookmarks/trash"' in src)
    check("账号体系：users 表", "CREATE TABLE IF NOT EXISTS users" in src)
    check("会话体系：sessions 表", "CREATE TABLE IF NOT EXISTS sessions" in src)
    check("密码 PBKDF2 加盐哈希", "hashlib.pbkdf2_hmac" in src)
    check("首次注册管理员接口", 'path == "/api/v1/setup/register"' in src)
    check("账号登录接口", 'path == "/api/v1/auth/login"' in src)
    check("用户管理接口", 'path == "/api/v1/users"' in src)
    check("数据按 user_id 隔离", "WHERE user_id=?" in src and "user_id, client_id" in src)
    check("保留 GatewayUnixServer", "class GatewayUnixServer" in src)
    check("保留 build_servers 双通道工厂", "def build_servers(" in src)
    check("保留前缀自动识别", "def auto_prefix" in src)
    check("严格校验 X-Forwarded-Prefix", "def safe_prefix" in src)
    check("服务端不再生成随机访问令牌", "secrets.token_hex" not in src)

    resource = json.loads((ROOT / "fpk/config/resource").read_text(encoding="utf-8"))
    pf = (resource.get("port-config") or {}).get("protocol-file")
    check("resource 声明 port-config", bool(pf), pf)
    sc = ROOT / "fpk" / pf if pf else None
    check(".sc 存在并放行 8765/tcp",
          bool(sc and sc.is_file() and 'src.ports="8765/tcp"' in sc.read_text(encoding="utf-8")))

    extmanifest = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))
    check("插件版本 1.4.0", extmanifest.get("version") == "1.4.0")
    check("插件名称 Fn收藏夹同步", extmanifest.get("name") == "Fn收藏夹同步")
    shared = (ROOT / "extension/shared.js").read_text(encoding="utf-8")
    check("插件使用账号密码登录", "/api/v1/auth/login" in shared)
    check("插件保存会话令牌", "sessionToken" in shared)
    check("插件支持会话失效自动重登", "authFailed" in shared)
    check("插件不再要求填写访问令牌", "'token': ''" not in shared)
    check("插件支持一键导入配置", "export function parsePluginConfig" in shared)
    check("插件支持多候选地址", "serverUrls: []" in shared and "'extra'" in shared)

    print("\n— 管理界面：连接卡片只读 + 书签管理 —")
    page = (ROOT / "server/web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "server/web/app.js").read_text(encoding="utf-8")
    css = (ROOT / "server/web/style.css").read_text(encoding="utf-8")
    check("连接卡片不再有反代路径输入框", 'id="proxyPath"' not in page)
    check("连接卡片不再有可编辑备用地址", '<input id="altAddr" readonly>' in page)
    check("主地址只读", '<input id="serverAddr" readonly>' in page)
    check("保留复制配置按钮", 'id="copyConfigBtn"' in page)
    check("展示自动识别的配置预览", 'id="configPreview"' in page)
    check("新增书签管理面板", 'id="panel-manage"' in page and 'data-tab="manage"' in page)
    check("书签管理含增删改查入口",
          all(k in page for k in ("addBookmarkBtn", "batchDeleteBtn", "trashList", "bmModal")))
    check("前端调用插件配置接口", "api/v1/plugin-config" in js)
    check("前端调用书签 CRUD 接口", "api/v1/bookmarks" in js and "'PUT'" in js and "'DELETE'" in js)
    check("前端不再读取本地反代设置", "fn_proxy_path" not in js and "PROXY_KEY" not in js)
    check("界面样式含书签表格与弹窗", ".bm-table" in css and ".modal-card" in css)

    print("\n— v1.3.0：紧凑布局 / 设备管理页 / 只读浏览 —")
    check("连接卡片默认折叠", '<details class="connect"' in page)
    check("设备列表可进入详情页",
          'id="deviceDetail"' in page and "data-open-device" in js)
    check("设备目录树支持多选与全选",
          'id="dtCheckAll"' in page and "data-folder-check" in js)
    check("设备树文件夹可勾选整组", "folder.querySelectorAll('[data-bm]')" in js)
    check("支持批量修改文件夹", 'id="dtMoveBtn"' in page and "api/v1/bookmarks/batch" in js)
    check("单条书签可编辑", "data-edit-bm" in js and "openBmModal(item)" in js)
    check("回收站支持一键清空",
          'id="clearTrash"' in page and "bookmarks/trash?all=1" in js)
    check("设备详情调用新接口", "api/v1/devices/tree" in js)
    check("书签浏览按设备标签筛选",
          'id="browseDevices"' in page and "api/v1/browse" in js)
    check("书签浏览只读（不渲染勾选框）", "renderTreeNodes(j.tree, 0, false)" in js)
    check("书签浏览标记所属设备", "dev-badge" in js and "n.devices" in js)
    check("紧凑样式：树行与设备标签", ".tree-row" in css and ".dev-badge" in css)

    print("\n— v1.3.0：插件紧凑化与本机备份还原 —")
    bg = (ROOT / "extension/background.js").read_text(encoding="utf-8")
    pop = (ROOT / "extension/popup.html").read_text(encoding="utf-8")
    check("插件可列出本机历史备份", "LIST_SNAPSHOTS" in bg and "listSnapshots" in bg)
    check("插件支持一键还原快照", "RESTORE_SNAPSHOT" in bg and "restoreSnapshot" in bg)
    check("还原后跳过已存在网址", "known.has(n.url)" in bg and "removeTree" in bg)
    check("弹窗含历史备份下拉与按钮",
          'id="snapSelect"' in pop and 'id="restoreBtn"' in pop)
    check("弹窗次要操作折叠", '<details class="more"' in pop)

    print("\n— 插件设置页：粘贴配置优先 —")
    opts = (ROOT / "extension/options.html").read_text(encoding="utf-8")
    optsjs = (ROOT / "extension/options.js").read_text(encoding="utf-8")
    check("设置页首步为粘贴配置", "① 粘贴插件配置" in opts)
    check("地址手填项收进高级折叠区", 'id="advancedBox"' in opts and 'id="serverUrl"' in opts)
    check("设置页调用配置解析", "parsePluginConfig" in optsjs)
    check("粘贴后自动解析", "addEventListener('paste'" in optsjs)

    print("\n— v1.3.1：设备标签拉取 / 策略自动保存 / 快照管理 —")
    popjs = (ROOT / "extension/popup.js").read_text(encoding="utf-8")
    popcss = (ROOT / "extension/popup.css").read_text(encoding="utf-8")

    check("插件可列出本账号的其他设备",
          "LIST_DEVICES" in bg and "'/api/v1/devices'" in bg)
    check("插件设备列表排除本机", "d.client_id !== cfg.clientId" in bg)
    check("插件按设备过滤 incoming", "&device=" in bg and "msg.clientId" in bg)
    check("弹窗含设备标签容器", 'id="pullDevices"' in pop)
    check("弹窗加载并渲染设备标签",
          "loadPullDevices" in popjs and "data-device" in popjs)
    check("拉取时带上所选设备", "type: 'PULL_NOW', clientId: pullDevice" in popjs)
    check("设备标签复用 chip 视觉", ".pull-chips .chip" in popcss)

    check("同步策略有独立保存消息", "SAVE_STRATEGY" in bg)
    check("策略保存后立即重排定时任务",
          "case 'SAVE_STRATEGY'" in bg and "await reschedule()" in bg)
    check("策略卡片内有保存按钮与状态", 'id="saveStrategyBtn"' in opts and 'id="strategyState"' in opts)
    check("策略改动自动保存",
          "STRATEGY_IDS" in optsjs and "saveStrategy(true)" in optsjs)
    check("策略保存不覆盖连接信息", "collectStrategy" in optsjs)

    check("管理端新增快照菜单",
          'data-tab="snapshots"' in page and 'id="panel-snapshots"' in page)
    check("快照列表含表格与批量操作",
          'id="snapTable"' in page and 'id="snapDeleteSelBtn"' in page)
    check("快照支持查看与下载",
          'id="snapModal"' in page and "downloadSnapshot" in js)
    check("快照预览复用只读目录树", "renderTreeNodes(tree, 0, false)" in js)
    check("顶栏快照数可点击直达", 'data-jump="snapshots"' in page)
    check("设置页旧快照下拉已移除", "snapshotSelect" not in page)
    check("前端调用快照管理接口",
          "api/v1/snapshots/manage" in js and "api/v1/snapshot" in js)
    check("服务端实现快照管理与按设备拉取",
          "/api/v1/snapshots/manage" in src and "delete_snapshots" in src and
          "source_client" in src)

    print("\n— v1.3.2：去掉健康检查 / HTML·CSV·JSON 多格式导出 —")
    check("设置页不再有健康检查入口",
          "健康检查" not in page and 'href="api/v1/health"' not in page)
    check("顶栏不再有恒绿状态灯",
          'id="healthBadge"' not in page and "healthBadge" not in js)
    check("服务端 health 探针仍保留(插件地址探测依赖)",
          'path == "/api/v1/health"' in src)
    check("不再定义 badge 样式", ".badge.ok" not in css)

    check("四处导出都用统一格式下拉",
          all(('id="%s"' % k) in page for k in
              ("allFmt", "browseFmt", "deviceFmt", "snapModalFmt")))
    check("格式下拉含 HTML / CSV / JSON 三选项",
          page.count('<option value="html">HTML</option>') >= 3 and
          '<option value="csv">CSV</option>' in page and
          '<option value="json">JSON</option>' in page)
    check("前端有统一导出函数", "function exportWith(" in js and "exportWith('allFmt')" in js)
    check("快照下载带 format 参数",
          "api/v1/snapshot?id=' + id + '&format=' + encodeURIComponent(fmt || 'html')" in js)
    check("快照表格行含格式下拉", "data-snap-fmt" in js)
    check("格式下拉样式已定义", ".fmt-select" in css)

    check("服务端新增 CSV 生成(带 BOM)",
          "def bookmarks_csv(" in src and "import csv" in src and "\\ufeff" in src)
    check("服务端复用树拍平并新增树转 HTML",
          "bookmarks_csv(flatten_tree(data))" in src and "def netscape_html_tree(" in src)
    check("export 支持 csv 且校验格式",
          'fmt not in ("html", "json", "csv")' in src and 'if fmt == "csv":' in src)
    check("snapshot 支持按 format 下载附件",
          "snapshot-%s-%s.csv" in src and "snapshot-%s-%s.html" in src)
    check("snapshot 不带 format 仍返回预览 JSON",
          'return self._json({"ok": True, "tree": data})' in src)
    check("版本号已统一 1.4.0", 'VERSION = "1.4.0"' in src and "v1.4.0" in page)

    print("\n— v1.4.0：品牌中立与多浏览器支持 —")
    check("插件版本门槛降到 91",
          extmanifest.get("minimum_chrome_version") == "91",
          extmanifest.get("minimum_chrome_version"))
    check("插件描述改为 Chromium 系",
          "Chromium 系浏览器" in extmanifest.get("description", "") and
          "Microsoft Edge 的收藏夹" not in extmanifest.get("description", ""))
    check("插件按 UA 识别品牌（detectBrowser）",
          "export function detectBrowser(" in shared and "'Chromium 浏览器'" in shared)
    check("默认设备名不再写死 Edge",
          "return 'Edge · ' + os" not in shared and
          "detectBrowser(ua).brand + ' · ' + os" in shared)
    check("background 用 detectBrowser 上报浏览器",
          "detectBrowser(navigator.userAgent || '').label" in bg and
          "/Edg\\/([\\d.]+)/" not in bg)
    check("管理页文案改为「连接浏览器插件」",
          "连接浏览器插件" in page and "连接 Edge 插件" not in page)
    check("登录提示改为「浏览器插件」",
          "浏览器插件用同一组账号登录" in page)
    check("设备列表兜底不再写 Microsoft Edge",
          "d.browser || '未知浏览器'" in js and "j.device.browser || '未知浏览器'" in js)
    check("插件设置页给出多浏览器扩展入口",
          "chrome://extensions" in opts and "vivaldi://extensions" in opts and
          "opera://extensions" in opts)
    check("网关提示改为「当前浏览器」",
          "请先在当前浏览器中登录飞牛 NAS" in shared)
    check("服务端升级提示版本同步", "浏览器插件升级到 1.4.0" in src)
    check("新增多浏览器支持文档", (ROOT / "docs/多浏览器支持.md").is_file())

    print("\n— 前缀工具函数 —")
    old = os.environ.get("EB_PROXY_PREFIXES")
    os.environ["EB_PROXY_PREFIXES"] = "/eb-api,/nested/proxy"
    try:
        from importlib.machinery import SourceFileLoader
        m = SourceFileLoader("eb_prefix_mod", str(SERVER)).load_module()
        check("configured_prefixes 含自定义前缀",
              "/eb-api" in m.configured_prefixes() and
              "/nested/proxy" in m.configured_prefixes())
        check("auto_prefix 识别反代 API 路径",
              m.auto_prefix("/eb-api/api/v1/health") == ("/eb-api", "/api/v1/health"))
        check("auto_prefix 识别反代首页",
              m.auto_prefix("/eb-api/") == ("/eb-api", "/"))
        check("auto_prefix 识别无尾斜杠首页",
              m.auto_prefix("/eb-api") == ("/eb-api", "/"))
        check("auto_prefix 不误判直连 API", m.auto_prefix("/api/v1/health")[0] == "")
        check("auto_prefix 不误判直连静态资源", m.auto_prefix("/style.css")[0] == "")
        check("auto_prefix 不误判 app.js", m.auto_prefix("/app.js")[0] == "")
        check("safe_prefix 拒绝注入", m.safe_prefix('/\"><script>') == "")
        check("safe_prefix 拒绝路径穿越", m.safe_prefix("/../../etc") == "")
        check("密码哈希可校验", m.verify_password("abc123456",
              m.hash_password("abc123456")) is True)
        check("错误密码校验失败", m.verify_password("wrong",
              m.hash_password("abc123456")) is False)
        check("中文用户名合法", m.valid_username("老杨") is True)
        check("非法用户名被拒绝", m.valid_username("a") is False)
        check("规范化网址忽略协议与末尾斜杠",
              m.normalize_url("HTTPS://GitHub.com/") == m.normalize_url("http://github.com#x"))
        check("规范化网址忽略 utm 跟踪参数",
              m.normalize_url("https://a.com/?utm_source=x") == m.normalize_url("https://a.com/"))
        check("不同查询参数不去重",
              m.url_key("https://a.com/?a=1") != m.url_key("https://a.com/?a=2"))
        check("拒绝 javascript 伪协议", m.valid_bookmark_url("javascript:alert(1)") is False)
        check("接受普通 https 网址", m.valid_bookmark_url("https://a.com/x") is True)
        # 回归：用户从 https://<域名>:9443/app/edge-bookmarks 打开管理页，
        # 但 NAS 上并没有 /eb-api 反代（实测 404）。旧版会把 /eb-api 当主地址。
        os.environ.pop("EB_PROXY_PREFIXES", None)
        info = m.plugin_addresses({"port": 8765}, "/app/edge-bookmarks/",
                                  {"Host": "nas.example.com:9443",
                                   "X-Forwarded-Proto": "https"}, True)
        check("统一网关访问：主地址就是当前管理页地址",
              info["primary"] == "https://nas.example.com:9443/app/edge-bookmarks",
              info["primary"])
        check("pageUrl 与 primary 一致", info["pageUrl"] == info["primary"])
        check("isGatewayPage 为真", info["isGatewayPage"] is True)
        check("不再凭空下发未配置的 /eb-api 路径",
              all("/eb-api" not in u for u in info["urls"]), info["urls"])
        check("网关地址进入候选池供插件探测",
              any("/app/edge-bookmarks" in u for u in info["urls"]), info["urls"])
        check("局域网直连作为兜底候选",
              any(u.startswith("http://") and u.endswith(":8765") for u in info["urls"]))
        check("复制配置不含密码",
              "password" not in m.plugin_config(info, "admin")["config"])
        os.environ["EB_PROXY_PREFIXES"] = "/eb-api"
        info2 = m.plugin_addresses({"port": 8765}, "/app/edge-bookmarks/",
                                   {"Host": "nas.example.com:9443",
                                    "X-Forwarded-Proto": "https"}, True)
        check("仅显式配置反代时才下发该路径",
              any(u.endswith("/eb-api") for u in info2["urls"]), info2["urls"])
        check("恶意 Host 头被过滤", m.safe_host('evil"><script>.com') == "")
    finally:
        if old is None:
            os.environ.pop("EB_PROXY_PREFIXES", None)
        else:
            os.environ["EB_PROXY_PREFIXES"] = old


def main():
    static_checks()
    port, gw = free_port(), free_port()
    tmp = tempfile.TemporaryDirectory(prefix="eb-gateway-")
    root = Path(tmp.name)
    sock = root / "app.sock"
    env = dict(os.environ)
    env["EB_GATEWAY_PREFIX"] = PREFIX
    cmd = [sys.executable, str(SERVER), "--host", "127.0.0.1",
           "--port", str(port), "--gateway-test-port", str(gw),
           "--gateway-socket", str(sock), "--data", str(root / "data"),
           "--config", str(root / "config.json"), "--web", str(ROOT / "server" / "web")]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env=env)
    try:
        if not wait_ready(port, proc):
            err = proc.stderr.read().decode("utf-8", "replace")[-800:] if proc.stderr else ""
            check("服务启动", False, err)
            raise SystemExit(1)
        check("服务启动（含无 AF_UNIX 降级）", True)

        print("\n— 通道与登录态 —")
        code, j, _ = req(port, "/api/v1/setup/state")
        check("直连 setup/state 不认定网关身份", code == 200 and j.get("gatewayAuth") is False)
        check("首次打开需要注册管理员", j.get("needsAdminSetup") is True)
        check("直连 setup/state 不下发令牌", "token" not in j)
        check("直连给出插件 TCP 地址", bool(j.get("pluginUrls")), j.get("pluginUrls"))
        check("返回网关前缀", j.get("gatewayPrefix") == PREFIX)
        check("返回认证方式", j.get("authMode") == "username-password")

        code, j, _ = req(gw, PREFIX + "/api/v1/setup/state", headers=ADMIN_HEADERS)
        check("网关 setup/state 识别 NAS 用户", code == 200 and j.get("gatewayAuth") is True
              and j.get("gatewayUser") == "admin")
        check("网关通道不再下发访问令牌", "token" not in j)

        code, _, _ = req(gw, PREFIX + "/api/v1/status")
        check("网关通道无账号时拒绝", code == 401)
        code, _, _ = req(gw, PREFIX + "/api/v1/status", headers=ADMIN_HEADERS)
        check("网关身份头不能替代账号密码（关键）", code == 401)
        code, _, _ = req(port, "/api/v1/status", headers=ADMIN_HEADERS)
        check("TCP 直连伪造 X-Trim-* 头被拒绝（关键）", code == 401)

        print("\n— 注册 / 登录 / 权限 —")
        code, j, _ = req(port, "/api/v1/setup/register", method="POST",
                         body={"username": "admin", "password": "Admin@123456"})
        check("注册管理员成功", code == 200 and bool(j.get("token")))
        admin = j["token"]
        code, j, _ = req(port, "/api/v1/users", method="POST",
                         body={"username": "yang", "password": "Yang@123456"}, token=admin)
        check("管理员新增普通用户", code == 200)
        code, j, _ = req(port, "/api/v1/auth/login", method="POST",
                         body={"username": "yang", "password": "Yang@123456"})
        check("普通用户登录成功", code == 200 and bool(j.get("token")))
        yang = j["token"]

        code, j, _ = req(gw, PREFIX + "/api/v1/status", token=admin)
        check("网关通道带会话可读状态", code == 200 and j.get("ok") is True)
        code, _, _ = req(gw, PREFIX + "/api/v1/users", token=yang, headers=USER_HEADERS)
        check("普通用户在网关通道也不能管用户", code == 403)
        code, _, _ = req(gw, PREFIX + "/api/v1/users", token=admin, headers=ADMIN_HEADERS)
        check("管理员在网关通道可管用户", code == 200)
        code, _, _ = req(gw, PREFIX + "/api/v1/users?id=2", method="DELETE", token=yang)
        check("普通用户不能删除用户", code == 403)

        print("\n— 前缀路由与前端 base —")
        code, html, _ = req(gw, PREFIX)
        check("网关首页 200", code == 200)
        check("首页注入网关 base", '<base href="%s/">' % PREFIX in html)
        check("首页显示新名称", "Fn收藏夹同步" in html)
        code, direct, _ = req(port, "/")
        check("直连首页 200 且 base=/", code == 200 and '<base href="/">' in direct)
        code, js, _ = req(gw, PREFIX + "/app.js")
        check("网关前缀下静态资源 200", code == 200 and "api/v1/status" in js)
        check("前端接口为相对路径", "'/api/v1/" not in js)
        code, css, _ = req(gw, PREFIX + "/style.css")
        check("网关前缀下 CSS 200", code == 200 and ".connect-body" in css)
        code, j, _ = req(gw, PREFIX + "/api/v1/health")
        check("网关前缀下 health 200", code == 200 and j.get("ok") is True)
        check("health 标明 gateway 通道",
              isinstance(j, dict) and j.get("channel") == "gateway")

        print("\n— 插件地址自动识别（只读展示，无需设置） —")
        code, j, _ = req(port, "/api/v1/plugin-config", token=admin)
        check("直连通道给出自动识别配置", code == 200 and j.get("autoDetected") is True)
        cfg_obj = json.loads(j.get("configText", "{}"))
        check("配置文本是合法 JSON 且带账号",
              cfg_obj.get("serverUrl") == j.get("serverUrl") and cfg_obj.get("username") == "admin")
        check("配置不含密码与令牌",
              "password" not in cfg_obj and "token" not in cfg_obj)
        check("地址列表不含网关前缀",
              all("/app/edge-bookmarks" not in str(a.get("url")) for a in j.get("addresses", [])))
        code, j, _ = req(gw, PREFIX + "/api/v1/plugin-config", token=admin,
                         headers=ADMIN_HEADERS)
        check("网关通道给出配置", code == 200 and j.get("channel") == "gateway")
        check("网关通道主地址就是当前管理页地址",
              str(j.get("serverUrl", "")).endswith("/app/edge-bookmarks"), j.get("serverUrl"))
        check("网关通道 pageUrl 与主地址一致", j.get("pageUrl") == j.get("serverUrl"))
        check("网关通道不再猜测未配置的 /eb-api",
              "/eb-api" not in str(j.get("configText")), j.get("configText"))
        code, j, _ = req(port, "/eb-api/api/v1/plugin-config", token=admin)
        check("零配置反代 A 自动带出 /eb-api 地址",
              j.get("serverUrl", "").endswith("/eb-api"), j.get("serverUrl"))
        code, j, _ = req(port, "/api/v1/plugin-config", token=admin,
                         headers={"X-Forwarded-Prefix": "/eb-api"})
        check("模式 B 反代头补回地址",
              j.get("serverUrl", "").endswith("/eb-api"), j.get("serverUrl"))
        code, j, _ = req(port, "/api/v1/plugin-config", token=admin,
                         headers={"X-Forwarded-Prefix": '/"><script>alert(1)</script>'})
        check("恶意前缀不进入插件配置", "<script>" not in str(j.get("configText")))
        code, _, _ = req(port, "/api/v1/plugin-config")
        check("插件配置接口需要登录", code == 401)
        code, j, _ = req(port, "/api/v1/setup/state")
        check("初始化状态也带插件地址", bool(j.get("pluginUrls")), j.get("pluginUrls"))

        print("\n— 两条通道共享同一份数据 —")
        tree = [{
            "id": "0", "title": "", "children": [
                {"id": "1", "title": "收藏夹栏", "children": [
                    {"id": "11", "title": "飞牛", "url": "https://www.fnnas.com/"},
                    {"id": "12", "title": "GitHub", "url": "https://github.com/"},
                ]},
            ],
        }]
        p = {"clientId": "gateway-test-0001", "deviceName": "网关测试机",
             "browser": "Microsoft Edge", "bookmarkCount": 2, "tree": tree}
        code, j, _ = req(gw, PREFIX + "/api/v1/sync", method="POST", body=p, token=admin)
        check("网关通道写入同步数据", code == 200 and j.get("ok") is True, j)
        code, j, _ = req(port, "/api/v1/bookmarks", token=admin)
        check("直连通道读到同一份数据", code == 200 and j.get("total") == 2, j.get("total"))
        code, j, _ = req(port, "/api/v1/bookmarks", token=yang)
        check("另一用户书签为空", j.get("total") == 0)
        code, body, _ = req(gw, PREFIX + "/api/v1/export?format=html", token=admin)
        check("网关前缀导出 Netscape HTML", code == 200 and "NETSCAPE-Bookmark-file" in body)

        print("\n— Unix Socket 能力降级 —")
        import importlib.util
        spec = importlib.util.spec_from_file_location("eb_server_mod", str(SERVER))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        check("本机 HAS_AF_UNIX 检测结果明确", isinstance(mod.HAS_AF_UNIX, bool), mod.HAS_AF_UNIX)
        if not mod.HAS_AF_UNIX:
            check("无 AF_UNIX 时不创建残留 socket 文件", not sock.exists())
            servers = mod.build_servers({}, "127.0.0.1", free_port(), str(sock), None)
            check("降级后仍保留 TCP 直连通道", len(servers) == 1)
            for s, _ in servers:
                s.server_close()
        else:
            check("有 AF_UNIX 时 socket 文件已建立", sock.exists())

        print("\n— 反向代理前缀（插件复用统一登录地址） —")
        code, j, _ = req(port, "/eb-api/api/v1/health")
        check("零配置反代 A：health 可达", code == 200 and j.get("ok") is True, code)
        check("零配置反代 A：base 注入 /eb-api/",
              isinstance(j, dict) and j.get("base") == "/eb-api/", j.get("base") if isinstance(j, dict) else j)
        code, page, _ = req(port, "/eb-api/")
        check("零配置反代 A：首页 <base> 带前缀",
              code == 200 and '<base href="/eb-api/">' in str(page))
        code, _, _ = req(port, "/eb-api/style.css")
        check("零配置反代 A：静态资源可达", code == 200)
        code, page, _ = req(port, "/eb-api")
        check("无尾斜杠的反代首页也返回界面",
              code == 200 and '<base href="/eb-api/">' in str(page))

        code, j, _ = req(port, "/api/v1/health", headers={"X-Forwarded-Prefix": "/eb-api"})
        check("模式 B：health 经请求头补回 base",
              isinstance(j, dict) and j.get("base") == "/eb-api/")
        code, page, _ = req(port, "/", headers={"X-Forwarded-Prefix": "/eb-api"})
        check("模式 B：首页 <base> 带前缀",
              code == 200 and '<base href="/eb-api/">' in str(page))

        for bad, label in (('/"><script>alert(1)</script>', "HTML 注入"),
                           ("/../../etc", "路径穿越"),
                           ("/a b", "空白字符")):
            code, j, _ = req(port, "/api/v1/health", headers={"X-Forwarded-Prefix": bad})
            check("拒绝恶意前缀：" + label,
                  isinstance(j, dict) and j.get("base") == "/")
        code, page, _ = req(port, "/", headers={
            "X-Forwarded-Prefix": '/"><script>alert(1)</script>'})
        check("恶意前缀不进入首页 HTML",
              "<script>" not in str(page) and '<base href="/"' in str(page))

        code, j, _ = req(port, "/api/v1/health")
        check("直连路径不被误判为前缀", isinstance(j, dict) and j.get("base") == "/")
        code, _, _ = req(port, "/style.css")
        check("直连静态资源可达", code == 200)
        code, j, _ = req(port, PREFIX + "/api/v1/health")
        check("统一网关前缀仍正常", isinstance(j, dict) and j.get("base") == PREFIX + "/")

        code, _, _ = req(port, "/eb-api/api/v1/status")
        check("反代路径下未登录仍被拒绝", code == 401)
        code, j, _ = req(port, "/eb-api/api/v1/status", token=admin)
        check("反代路径下带会话可访问", code == 200 and j.get("ok") is True)
        code, j, _ = req(port, "/eb-api/api/v1/auth/login", method="POST",
                         body={"username": "admin", "password": "Admin@123456"})
        check("反代路径下可直接登录", code == 200 and bool(j.get("token")))

        code, j, _ = req(port, "/api/v1/setup/state")
        check("setup/state 返回 proxyPrefixes / pageBase",
              isinstance(j, dict) and "proxyPrefixes" in j and "pageBase" in j)
        code, j, _ = req(port, "/api/v1/health")
        check("health 返回 channel / prefixes",
              isinstance(j, dict) and j.get("channel") == "direct" and
              isinstance(j.get("prefixes"), list))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmp.cleanup()

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
