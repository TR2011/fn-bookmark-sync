#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成插件与 FPK 应用图标（仅依赖 Pillow，产物为 PNG）。

v1.3.0 新 LOGO：圆角青绿底色 + 白色书签 + 向下的同步箭头。
寓意「把收藏夹收进书签、同步到 NAS」，16px 下仍能看出书签与箭头。
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SIZE = 512

BRAND_TOP = (18, 160, 139)
BRAND_BOTTOM = (10, 106, 92)
ARROW = (14, 138, 120)


def make_master(small=False):
    """small=True 时去掉箭头、加深缺口：16px 下箭头的像素不足以辨识。"""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 背景：圆角方块 + 纵向渐变
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(SIZE):
        t = y / SIZE
        color = (int(BRAND_TOP[0] + (BRAND_BOTTOM[0] - BRAND_TOP[0]) * t),
                 int(BRAND_TOP[1] + (BRAND_BOTTOM[1] - BRAND_TOP[1]) * t),
                 int(BRAND_TOP[2] + (BRAND_BOTTOM[2] - BRAND_TOP[2]) * t), 255)
        bd.line([(0, y), (SIZE, y)], fill=color)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=112, fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)

    if small:
        # 小尺寸：书签占满、缺口加深，不加箭头
        d.rounded_rectangle((128, 78, 384, 180), radius=30, fill=(255, 255, 255, 255))
        d.polygon([(128, 140), (128, 432), (256, 304), (384, 432), (384, 140)],
                  fill=(255, 255, 255, 255))
        return img

    # 白色书签：上沿平直，下沿挖出 V 形缺口
    d.rounded_rectangle((144, 96, 368, 190), radius=26, fill=(255, 255, 255, 255))
    d.polygon([(144, 150), (144, 416), (256, 344), (368, 416), (368, 150)],
              fill=(255, 255, 255, 255))

    # 书签中间的向下箭头（挖空成底色），表达「同步收纳」
    d.rounded_rectangle((236, 142, 276, 232), radius=9, fill=ARROW + (255,))
    d.polygon([(256, 300), (206, 224), (306, 224)], fill=ARROW + (255,))
    return img


def main():
    master = make_master()
    small = make_master(small=True)
    outputs = []
    ext = ROOT / "extension" / "icons"
    ext.mkdir(parents=True, exist_ok=True)
    for n in (16, 32, 48, 128):
        outputs.append(((small if n <= 16 else master), ext / ("%d.png" % n), n))
    fpk = ROOT / "fpk"
    (fpk / "app" / "ui" / "images").mkdir(parents=True, exist_ok=True)
    outputs += [
        (master, fpk / "ICON.PNG", 64),
        (master, fpk / "ICON_256.PNG", 256),
        (master, fpk / "app" / "ui" / "images" / "icon_64.png", 64),
        (master, fpk / "app" / "ui" / "images" / "icon_256.png", 256),
    ]
    for src, path, size in outputs:
        src.resize((size, size), Image.LANCZOS).save(path, "PNG")
        print("生成", path.relative_to(ROOT), "%dx%d" % (size, size))


if __name__ == "__main__":
    main()
