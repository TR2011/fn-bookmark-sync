#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 OS FPK 构建器（跨平台，零第三方依赖）

两种用法：
  1) 本机直接产出 .fpk（不需要 fnpack）：
        python tools/build_fpk.py
  2) 只把服务端源码同步进 fpk/app，交给官方 fnpack 打包（推荐用于正式发布）：
        python tools/build_fpk.py --sync-only
        然后：cd fpk && fnpack build

产物：dist/fn-bookmark-sync_<version>_<platform>.fpk
      dist/fn-bookmark-sync-extension_<version>.zip
"""
import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPK = ROOT / "fpk"
SERVER = ROOT / "server"
EXT = ROOT / "extension"
DIST = ROOT / "dist"
# 分发包名：只用于产物文件名，与 fnOS 内部标识 appname 解耦。
# appname=edge-bookmarks 是应用识别/升级兼容的关键字段，绝不能改；文件名可自由品牌化。
PACKAGE_NAME = "fn-bookmark-sync"
IGNORE = {"__pycache__", ".DS_Store", "Thumbs.db"}
REQUIRED_MANIFEST = ["appname", "version", "display_name", "desc",
                     "platform", "source", "service_port", "desktop_uidir",
                     "desktop_applaunchname"]


def log(msg):
    print("[build] " + msg, flush=True)


def die(msg):
    print("[build] 错误：" + msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def norm_lf(path):
    """把文本文件统一为 LF（fnOS 上的 shell 脚本与 manifest 不能使用 CRLF）。"""
    raw = Path(path).read_bytes()
    fixed = raw.replace(b"\r\n", b"\n")
    if fixed != raw:
        Path(path).write_bytes(fixed)


def copy_tree(src, dst, text_exts=(".py", ".html", ".js", ".css", ".json", ".sc", "")):
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    def ignore(directory, names):
        return [n for n in names if n in IGNORE or n.endswith(".pyc")]

    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
    for p in dst.rglob("*"):
        if p.is_file() and (p.suffix in text_exts or p.name in ("manifest", "config", "privilege", "resource")):
            norm_lf(p)
    for p in dst.rglob("*"):
        if p.is_file() and p.suffix == ".py":
            p.chmod(0o644)


def sync_sources():
    """把唯一的源码（server/）同步到 fpk/app/，避免维护两份。"""
    if not (SERVER / "server.py").exists():
        die("缺少 server/server.py")
    app = FPK / "app"
    (app / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SERVER / "server.py", app / "bin" / "server.py")
    norm_lf(app / "bin" / "server.py")
    copy_tree(SERVER / "web", app / "web")
    log("已同步服务端源码 → fpk/app/bin、fpk/app/web")


def read_manifest():
    path = FPK / "manifest"
    if not path.exists():
        die("缺少 fpk/manifest")
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    for key in REQUIRED_MANIFEST:
        if not data.get(key):
            die("manifest 缺少必填字段：%s" % key)
    return data


def rewrite_manifest(meta, checksum, platform):
    """生成 staging/manifest：注入 app.tgz 的 MD5，可选覆盖 platform。"""
    lines = []
    seen = set()
    for line in (FPK / "manifest").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            key = m.group(1)
            seen.add(key)
            if key == "checksum":
                lines.append("checksum=%s" % checksum)
                continue
            if key == "platform" and platform:
                lines.append("platform=%s" % platform)
                continue
        lines.append(line)
    if "checksum" not in seen:
        lines.append("checksum=%s" % checksum)
    return ("\n".join(lines) + "\n").encode("utf-8")


def validate(meta):
    """打包前静态检查：结构、JSON、图标、脚本。"""
    for p in ("cmd", "config", "app", "app/ui", "wizard"):
        if not (FPK / p).is_dir():
            die("缺少目录 fpk/%s" % p)
    for name in ("privilege", "resource"):
        f = FPK / "config" / name
        if not f.exists():
            die("缺少 fpk/config/%s" % name)
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            die("config/%s 不是合法 JSON：%s" % (name, e))
    for f in (FPK / "app" / "ui" / "config",):
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            die("app/ui/config 不是合法 JSON：%s" % e)

    for name, size in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
        f = FPK / name
        if not f.exists():
            die("缺少 %s，请先运行 python tools/make_icons.py" % name)
        with f.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            die("%s 不是 PNG" % name)
        w, h = struct.unpack(">II", head[16:24])
        if (w, h) != (size, size):
            die("%s 尺寸应为 %dx%d，实际 %dx%d" % (name, size, size, w, h))

    for f in (FPK / "cmd").iterdir():
        if f.is_file():
            norm_lf(f)
            if f.read_bytes().startswith(b"\xef\xbb\xbf"):
                die("cmd/%s 含 BOM，会导致 fnOS 执行失败" % f.name)
    if not (FPK / "app" / "bin" / "server.py").exists():
        die("缺少 fpk/app/bin/server.py，请先运行 --sync-only")

    # service_port 声明必须配套 port-config + .sc，否则 fnOS 防火墙不会放行端口。
    resource = json.loads((FPK / "config" / "resource").read_text(encoding="utf-8"))
    pf = (resource.get("port-config") or {}).get("protocol-file")
    if pf:
        sc = FPK / pf
        if not sc.is_file():
            die("config/resource 声明了 %s，但包根缺少该文件" % pf)
        body = sc.read_text(encoding="utf-8").strip()
        if not re.search(r'port_forward="yes"', body) or not re.search(r'src\.ports="\d+/tcp"', body):
            die("%s 缺少 port_forward=\"yes\" 或 src.ports 声明" % pf)
        log("端口放行声明：%s（%s）" % (pf, meta.get("service_port", "8765")))
    log("静态检查通过（manifest / JSON / 图标 / 生命周期脚本 / 端口声明）")


def add_file(tar, src, arc, mode=None):
    """以可控权限加入单个文件（保证 cmd/* 在 NAS 上可执行）。"""
    info = tar.gettarinfo(str(src), arcname=arc)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    if mode is not None:
        info.mode = mode
    elif arc.startswith("cmd/"):
        info.mode = 0o755
    else:
        info.mode = 0o644
    with open(src, "rb") as fh:
        tar.addfile(info, fh)


def add_bytes(tar, arc, data, mode=0o644):
    import io
    info = tarfile.TarInfo(arc)
    info.size = len(data)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def add_dir(tar, arc):
    info = tarfile.TarInfo(arc.rstrip("/"))
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    tar.addfile(info)


def make_app_tgz(dest):
    """app.tgz = fpk/app/ 的内容（解压到 target/ 后为 bin/、web/、ui/）。"""
    app = FPK / "app"
    with tarfile.open(dest, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(app.rglob("*")):
            rel = path.relative_to(app).as_posix()
            if path.is_dir():
                add_dir(tar, rel)
            elif path.is_file():
                add_file(tar, path, rel, 0o755 if rel.startswith("bin/") else 0o644)
    digest = hashlib.md5(dest.read_bytes()).hexdigest()
    log("app.tgz 生成完毕（%d 字节，MD5 %s）" % (dest.stat().st_size, digest))
    return digest


def assemble(meta, checksum, platform, out, port_config=True):
    """组装 FPK 根包：manifest、app.tgz、cmd、config、wizard、ui、图标、防火墙规则。

    port_config=False 用于产出「无端口声明」对照包，便于在真实 NAS 上
    排除安装期 port-config 解析失败的可能（resource 写成 {} 且不含 .sc）。
    """
    with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        add_bytes(tar, "manifest", rewrite_manifest(meta, checksum, platform))
        add_file(tar, out.parent / "app.tgz", "app.tgz")

        for sub in ("cmd", "config", "wizard"):
            add_dir(tar, sub)
            d = FPK / sub
            for path in sorted(d.rglob("*")):
                rel = path.relative_to(d).as_posix()
                if path.is_dir():
                    add_dir(tar, "%s/%s" % (sub, rel))
                elif not port_config and sub == "config" and rel == "resource":
                    add_bytes(tar, "config/resource", b"{}\n")
                elif path.is_file():
                    add_file(tar, path, "%s/%s" % (sub, rel))

        # 桌面入口：包根 ui/（兼容 fnos-apps 风格）与 app.tgz 内 ui/ 并存
        add_dir(tar, "ui")
        uidir = FPK / "app" / "ui"
        for path in sorted(uidir.rglob("*")):
            rel = path.relative_to(uidir).as_posix()
            if path.is_dir():
                add_dir(tar, "ui/" + rel)
            elif path.is_file():
                add_file(tar, path, "ui/" + rel)

        # LICENSE 放在仓库根目录，不进 FPK（保持与已验证可装的诊断包一致）。
        for name in ("ICON.PNG", "ICON_256.PNG"):
            add_file(tar, FPK / name, name)
        # .sc 端口协议文件：与 config/resource 的 protocol-file 一一对应。
        # 不声明它，fnOS 防火墙不会开放 service_port，局域网插件会连不上。
        if port_config:
            for sc in sorted(FPK.glob("*.sc")):
                add_file(tar, sc, sc.name)


def verify_fpk(path, expected_md5):
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
        for required in ("manifest", "app.tgz", "cmd/main", "config/privilege",
                         "config/resource", "ICON.PNG", "ICON_256.PNG"):
            if required not in names:
                die("包内缺少 %s" % required)
        manifest = tar.extractfile("manifest").read().decode("utf-8")
        m = re.search(r"^checksum=(\S+)\s*$", manifest, re.M)
        if not m or m.group(1) != expected_md5:
            die("manifest 中 checksum 与 app.tgz 不一致")
        app_md5 = hashlib.md5(tar.extractfile("app.tgz").read()).hexdigest()
        if app_md5 != expected_md5:
            die("app.tgz 校验和不匹配")
        main_info = tar.getmember("cmd/main")
        if not (main_info.mode & 0o111):
            die("cmd/main 缺少执行权限")
        res = json.loads(tar.extractfile("config/resource").read().decode("utf-8"))
        pf = (res.get("port-config") or {}).get("protocol-file")
        if pf and pf not in names:
            die("包内缺少 .sc 协议文件 %s" % pf)
    log("包校验通过：结构、checksum、执行权限、端口声明")


def zip_extension(meta, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(EXT.rglob("*")):
            if path.is_dir():
                continue
            if any(part in IGNORE for part in path.parts) or path.suffix == ".pyc":
                continue
            z.write(path, path.relative_to(EXT).as_posix())
    log("插件压缩包：%s" % out.name)


def main():
    ap = argparse.ArgumentParser(description="飞牛 OS FPK 构建器")
    ap.add_argument("--sync-only", action="store_true", help="仅同步源码到 fpk/app，随后可用 fnpack build")
    ap.add_argument("--platform", help="覆盖 manifest 中的 platform（x86 / arm / all）")
    ap.add_argument("--no-zip", action="store_true", help="不生成插件 zip")
    ap.add_argument("--no-port-config", action="store_true",
                    help="生成对照包：resource={} 且不含 .sc，用于排除安装期端口声明的影响")
    args = ap.parse_args()

    sync_sources()
    if args.sync_only:
        log("完成。现在可在 fpk/ 目录执行 fnpack build")
        return

    meta = read_manifest()
    validate(meta)
    DIST.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="eb-fpk-"))
    try:
        app_tgz = work / "app.tgz"
        md5 = make_app_tgz(app_tgz)
        shutil.move(str(app_tgz), str(DIST / "app.tgz"))
        app_tgz = DIST / "app.tgz"

        platform = args.platform or meta["platform"]
        suffix = "_noport" if args.no_port_config else ""
        out = DIST / ("%s_%s_%s%s.fpk" % (PACKAGE_NAME, meta["version"], platform, suffix))
        assemble(meta, md5, platform, out, port_config=not args.no_port_config)
        verify_fpk(out, md5)
        app_tgz.unlink(missing_ok=True)
        log("FPK 产物：%s（%d 字节）" % (out.name, out.stat().st_size))

        if not args.no_zip:
            zip_extension(meta, DIST / ("%s-extension_%s.zip" % (PACKAGE_NAME, meta["version"])))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
