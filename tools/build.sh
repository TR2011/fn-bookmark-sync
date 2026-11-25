#!/bin/bash
# 在飞牛 NAS / Linux / WSL 上构建。优先使用官方 fnpack，缺失时退回内置打包器。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/3 同步服务端源码到 fpk/app"
mkdir -p fpk/wizard
python3 tools/build_fpk.py --sync-only

if command -v fnpack >/dev/null 2>&1; then
    echo "==> 2/3 使用官方 fnpack 打包"
    (cd fpk && fnpack build)
    mkdir -p dist
    find fpk -maxdepth 1 -name '*.fpk' -exec cp -f {} dist/ \;
    echo "==> 3/3 补充生成插件压缩包"
    python3 - <<'PY'
import zipfile
from pathlib import Path
root = Path('.')
ext = root / 'extension'
dist = root / 'dist'
dist.mkdir(exist_ok=True)
meta = {}
for line in (root / 'fpk' / 'manifest').read_text(encoding='utf-8').splitlines():
    if '=' in line:
        k, v = line.strip().split('=', 1)
        meta[k] = v
out = dist / ('fn-bookmark-sync-extension_%s.zip' % meta['version'])
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(ext.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts:
            z.write(p, p.relative_to(ext).as_posix())
print('插件压缩包：', out)
PY
else
    echo "==> 2/3 未检测到 fnpack，使用内置打包器（结构与官方一致）"
    python3 tools/build_fpk.py
    echo "==> 3/3 完成"
fi

echo ""
echo "产物位于 dist/ ："
ls -lh dist/
