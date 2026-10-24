"""gen_icons.py — 生成 PWA 图标（192/512 PNG + SVG）"""
from PIL import Image, ImageDraw

ACCENT = (24, 95, 165, 255)     # #185FA5
WHITE = (255, 255, 255, 255)

def icon(size: int, path: str, padding_ratio: float = 0.0):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)                      # 圆角
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=ACCENT)
    pad = int(size * padding_ratio)
    # 折线：左低右高的上升趋势线
    pts = [
        (pad + (size - 2 * pad) * 0.16, pad + (size - 2 * pad) * 0.70),
        (pad + (size - 2 * pad) * 0.38, pad + (size - 2 * pad) * 0.42),
        (pad + (size - 2 * pad) * 0.55, pad + (size - 2 * pad) * 0.55),
        (pad + (size - 2 * pad) * 0.84, pad + (size - 2 * pad) * 0.24),
    ]
    w = max(2, int(size * 0.085))
    d.line(pts, fill=WHITE, width=w, joint="curve")
    img.save(path)
    print("saved", path, img.size)

icon(192, "web/icon-192.png")
icon(512, "web/icon-512.png")
icon(512, "web/icon-maskable.png", padding_ratio=0.12)

# SVG 版（矢量备用）
svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#185FA5"/>
<path d="M7 21.5l5-6 4 3 6-8" stroke="#fff" stroke-width="2.6" fill="none"
 stroke-linecap="round" stroke-linejoin="round"/>
</svg>'''
with open("web/icon.svg", "w", encoding="utf-8") as f:
    f.write(svg)
print("saved web/icon.svg")
