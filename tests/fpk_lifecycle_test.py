#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟 fnOS 的 FPK 安装 / 启动 / 停止生命周期（跨平台）。

重点验证 v1.4.0：
  - 所有安装期钩子返回 0，且在「没有 Python 3」的环境下依然返回 0；
  - 安装期不在 $TRIM_PKGETC / $TRIM_PKGVAR 留下任何写入；
  - 初始化推迟到 main start：生成 config.json 与 SQLite 账号库；
  - 不再生成 token.txt，升级时会清理 v1.0.x 遗留的令牌文件；
  - main status 运行中 0、未运行 3（fnOS 约定）。

用 Python 的 tarfile 解包、subprocess 调 bash，避开 Windows Git Bash 下
tar -C 把盘符当远程主机名的路径问题。
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPK = ROOT / "dist" / "edge-bookmarks_1.4.0_all.fpk"
PORT = 18769
PASSED = 0
FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    suffix = " · " + str(detail) if detail else ""
    if ok:
        PASSED += 1
        print("  [OK]   %s%s" % (name, suffix))
    else:
        FAILED += 1
        print("  [FAIL] %s%s" % (name, suffix))


def cygpath(p):
    """转成 Git Bash 可识别的 /d/... 形式。"""
    try:
        return subprocess.check_output(["cygpath", "-u", str(p)],
                                       text=True).strip()
    except Exception:
        return str(p).replace("\\", "/")


def main():
    if not FPK.exists():
        print("缺少 %s，请先运行 python tools/build_fpk.py" % FPK)
        return 1
    bash = shutil.which("bash")
    if not bash:
        print("未找到 bash，跳过生命周期模拟")
        return 0

    tmp = Path(ROOT) / (".tmp-lifecycle-%d" % os.getpid())
    if tmp.exists():
        shutil.rmtree(tmp)
    pkg, app, etc, var, bindir = (tmp / n for n in ("pkg", "app", "etc", "var", "bin"))
    for d in (pkg, app, etc, var, bindir):
        d.mkdir(parents=True)

    def extract(src, dest):
        with tarfile.open(src, "r:gz") as t:
            try:
                t.extractall(dest, filter="data")   # Python 3.12+
            except TypeError:
                t.extractall(dest)                   # 兼容旧版

    extract(FPK, pkg)
    extract(pkg / "app.tgz", app)

    print("— 包结构与单图标入口 —")
    manifest = (pkg / "manifest").read_text(encoding="utf-8")
    check("manifest 版本 1.4.0", "version=1.4.0" in manifest)
    check("manifest 显示名为 Fn收藏夹同步", "display_name=Fn收藏夹同步" in manifest)
    ui_cfg = json.loads((pkg / "ui" / "config").read_text(encoding="utf-8"))[".url"]
    check("包内桌面仅一个入口", len(ui_cfg) == 1, list(ui_cfg.keys()))
    check("入口点击打开新页面", ui_cfg["edge-bookmarks.main"].get("type") == "url")
    icons = [pkg / "ICON.PNG", pkg / "ICON_256.PNG",
             app / "ui" / "images" / "icon_64.png",
             app / "ui" / "images" / "icon_256.png"]
    check("新版 LOGO 已打进包内",
          all(p.is_file() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in icons),
          [p.name for p in icons])

    # 让 find_python 的最后一个候选 command -v python3 命中
    (bindir / "python3").write_text('#!/bin/bash\nexec python "$@"\n', encoding="utf-8")
    (bindir / "python3").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = cygpath(bindir) + os.pathsep + env.get("PATH", "")
    env["TRIM_APPDEST"] = cygpath(app)
    env["TRIM_PKGETC"] = cygpath(etc)
    env["TRIM_PKGVAR"] = cygpath(var)
    env["TRIM_SERVICE_PORT"] = str(PORT)

    def run(script, *args, override=None):
        e = dict(env)
        if override:
            e.update(override)
        return subprocess.run([bash, str(pkg / "cmd" / script), *args],
                              env=e, capture_output=True, text=True, timeout=90)

    print("\n— 安装 / 升级期钩子（含无 Python 3 场景） —")
    install_hooks = ["install_init", "install_callback", "upgrade_init", "upgrade_callback"]
    other_hooks = ["uninstall_init", "uninstall_callback", "config_init", "config_callback"]
    for h in install_hooks:
        r = run(h)
        check("%s 返回 0" % h, r.returncode == 0,
              "exit=%d %s" % (r.returncode, r.stderr.strip()[:80]) if r.returncode else "")

    no_py = {"PATH": "/usr/bin:/bin"}
    for h in ("install_init", "install_callback"):
        r = run(h, override=no_py)
        check("%s 无 Python 3 时仍返回 0" % h, r.returncode == 0,
              "exit=%d" % r.returncode)

    check("安装期不写 $TRIM_PKGETC", not any(etc.iterdir()),
          "已有 %s" % [p.name for p in etc.iterdir()] if any(etc.iterdir()) else "目录为空")
    check("安装期不写 $TRIM_PKGVAR", not any(var.iterdir()),
          "已有 %s" % [p.name for p in var.iterdir()] if any(var.iterdir()) else "目录为空")

    print("\n— 其余钩子同样零副作用 —")
    for h in other_hooks:
        r = run(h)
        check("%s 返回 0" % h, r.returncode == 0,
              "exit=%d %s" % (r.returncode, r.stderr.strip()[:80]) if r.returncode else "")
    check("全部钩子执行后数据目录仍为空", not any(var.iterdir()) and not any(etc.iterdir()))

    # 模拟从 v1.0.x 升级：etc 里残留一个旧令牌文件
    (etc / "token.txt").write_text("a" * 48, encoding="utf-8")

    print("\n— 启动阶段（初始化全部推迟到这里） —")
    r = run("main", "start")
    check("main start 返回 0", r.returncode == 0,
          r.stderr.strip()[:160] if r.returncode else "")
    check("生成 config.json", (etc / "config.json").exists())
    cfg_text = (etc / "config.json").read_text(encoding="utf-8") if (etc / "config.json").exists() else ""
    check("config.json 不含访问令牌", '"token"' not in cfg_text)
    status_file = var / "STARTUP_STATUS.txt"
    status_text = status_file.read_text(encoding="utf-8", errors="replace") if status_file.exists() else ""
    check("启动留痕记录网关 Socket 路径", "网关Socket=" in status_text and "app.sock" in status_text)
    check("不再生成 token.txt", not (etc / "token.txt").exists())
    check("升级时清理旧 token.txt", not (etc / "token.txt").exists())
    check("生成 SQLite 数据库", (var / "bookmarks.db").exists())

    health = None
    for _ in range(10):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/v1/health" % PORT,
                                        timeout=2) as resp:
                if resp.status == 200:
                    health = json.loads(resp.read().decode("utf-8"))
                    break
        except Exception:
            time.sleep(0.3)
    check("健康检查 200", health is not None and health.get("ok") is True)
    check("首次打开提示注册管理员", bool(health and health.get("needsAdminSetup") is True))
    check("认证方式为用户名密码", bool(health and health.get("authMode") == "username-password"))

    r = run("main", "status")
    check("main status 运行中返回 0", r.returncode == 0, "exit=%d" % r.returncode)

    print("\n— 停止 —")
    r = run("main", "stop")
    check("main stop 返回 0", r.returncode == 0)
    check("停止后清理网关 Socket 残留", not (app / "app.sock").exists())
    r = run("main", "status")
    check("main status 未运行返回 3", r.returncode == 3, "exit=%d" % r.returncode)

    print("\n===== 生命周期结果：%d 通过 / %d 失败 =====" % (PASSED, FAILED))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
