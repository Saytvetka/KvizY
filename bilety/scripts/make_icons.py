"""Рисует иконки BiletY и экраны запуска iOS (static/icons): пиксельный билет с буквами «BY».
Запуск: python scripts/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"

# 16×16: # — белый пиксель билета, . — чёрный (фон, вырезы, перфорация и буквы «BY»)
ART = [
    "................",
    "................",
    "................",
    ".##############.",
    ".##########.###.",
    ".##..##.#.#####.",
    "..#.#.#.#.#.##..",
    "..#..###.#####..",
    "..#.#.##.##.##..",
    "..#..###.#####..",
    ".##########.###.",
    ".##############.",
    ".##########.###.",
    "................",
    "................",
    "................",
]


def icon(size: int, pad: float = 0.0) -> Image.Image:
    img = Image.new("RGB", (size, size), "#111111")
    d = ImageDraw.Draw(img)
    inner = size * (1 - 2 * pad)
    cell = inner / 16
    off = (size - inner) / 2
    for y, row in enumerate(ART):
        for x, ch in enumerate(row):
            if ch == "#":
                x0, y0 = off + x * cell, off + y * cell
                d.rectangle([x0, y0, x0 + cell - 0.01, y0 + cell - 0.01], fill="#ffffff")
    return img


def splash(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), "#ffffff")
    s = int(min(w, h) * 0.3)
    ic = icon(s).convert("RGB")
    img.paste(ic, ((w - s) // 2, (h - s) // 2))
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    icon(180).save(OUT / "apple-touch-icon.png", optimize=True)
    icon(192).save(OUT / "icon-192.png", optimize=True)
    icon(512).save(OUT / "icon-512.png", optimize=True)
    icon(512, pad=0.1).save(OUT / "icon-maskable-512.png", optimize=True)
    for w, h in [(1290, 2796), (1179, 2556), (1170, 2532), (750, 1334), (1640, 2360), (1668, 2388), (2048, 2732)]:
        splash(w, h).save(OUT / f"splash-{w}x{h}.png", optimize=True)
    print("ok")
