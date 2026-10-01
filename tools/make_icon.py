"""生成应用图标（assets/app.ico）。

纯绘图实现，不依赖外部图片文件：深色圆角底 + 蓝色键盘格 + 白色指针。
运行：python tools/make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "app.ico")
S = 256
BG = (24, 27, 33, 255)
ACCENT = (47, 111, 237, 255)
ACCENT_D = (37, 89, 196, 255)
KEY = (232, 234, 237, 255)
MUTED = (154, 163, 175, 255)


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / 256.0

    def s(v):
        return v * k

    # 底板
    rounded(d, [s(8), s(8), s(248), s(248)], s(52), BG)

    # 键盘主体
    rounded(d, [s(38), s(96), s(218), s(196)], s(14), ACCENT)
    rounded(d, [s(38), s(96), s(218), s(140)], s(14), (60, 126, 245, 255))

    # 键位点阵 4 列 x 2 行
    kw, kh = s(28), s(22)
    x0, y0 = s(54), s(114)
    gapx, gapy = s(38), s(30)
    for r in range(2):
        for c in range(4):
            x = x0 + c * gapx
            y = y0 + r * gapy
            if r == 1 and c == 3:
                continue
            rounded(d, [x, y, x + kw, y + kh], s(5), KEY)

    # 长空格键
    rounded(d, [s(54), s(174), s(202), s(174 + 14)], s(6), KEY)

    # 鼠标指针
    pts = [
        (s(150), s(150)),
        (s(150), s(216)),
        (s(168), s(200)),
        (s(180), s(228)),
        (s(194), s(222)),
        (s(181), s(195)),
        (s(203), s(193)),
    ]
    d.polygon(pts, fill=(255, 255, 255, 255), outline=(20, 22, 26, 255))

    return img


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    base = render(256)
    imgs = [base.resize((n, n), Image.LANCZOS) for n in sizes]
    # 同时留一份 png 便于预览
    base.save(OUT.replace(".ico", ".png"))
    imgs[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
    print("已生成", OUT)
    print("已生成", OUT.replace(".ico", ".png"))


if __name__ == "__main__":
    main()
