#!/usr/bin/env python3
"""Generuje identyfikację wizualną repo (ikona .png/.ico + banner GitHub).

Zero zależności poza Pillow:  pip install pillow
Użycie:  python tools/make_art.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

BG = (13, 17, 23, 255)
BG2 = (20, 27, 40, 255)
TORII_TOP = (255, 96, 74, 255)
TORII_BOT = (214, 48, 49, 255)
CYAN = (77, 216, 230, 255)
VIOLET = (158, 122, 255, 255)
BLUE = (88, 158, 255, 255)
TEXT_W = (230, 237, 243, 255)
MUTED = (159, 176, 200, 255)

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FONT_REG_DIRS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for p in (FONT_DIRS if bold else FONT_REG_DIRS):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _vgrad(w: int, h: int, c1, c2) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(a + (b - a) * t) for a, b in zip(c1[:3], c2[:3])) + (255,)
        for x in range(w):
            px[x, y] = col
    return img


def _hgrad(w: int, h: int, c1, c2) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for x in range(w):
        t = x / max(1, w - 1)
        col = tuple(int(a + (b - a) * t) for a, b in zip(c1[:3], c2[:3])) + (255,)
        for y in range(h):
            px[x, y] = col
    return img


def draw_torii(draw: ImageDraw.ImageDraw, x0: int, y0: int, s: float) -> None:
    """Rysuje torii + strzałkę migracji w kwadracie (x0,y0,x0+s,y0+s)."""
    def P(*pairs):
        return [(x0 + a * s, y0 + b * s) for a, b in pairs]

    # poświata pod bramą
    # (rysuje ją osobna warstwa blur w make_icon; tu tylko kształty)

    # strzałka przechodząca przez bramę (rysowana PRZED nogami = "przez środek")
    shaft = P((0.12, 0.62), (0.68, 0.62), (0.68, 0.70), (0.12, 0.70))
    head = P((0.66, 0.55), (0.88, 0.66), (0.66, 0.77))
    draw.polygon(shaft, fill=CYAN)
    draw.polygon(head, fill=VIOLET)

    # nogi (lekko pochylone)
    draw.polygon(P((0.28, 0.36), (0.36, 0.36), (0.34, 0.88), (0.26, 0.88)), fill=TORII_BOT)
    draw.polygon(P((0.64, 0.36), (0.72, 0.36), (0.74, 0.88), (0.66, 0.88)), fill=TORII_BOT)
    # nuki (pozioma belka)
    draw.polygon(P((0.20, 0.44), (0.80, 0.44), (0.80, 0.51), (0.20, 0.51)), fill=TORII_TOP)
    # gakuzuka (słupek środkowy)
    draw.polygon(P((0.47, 0.33), (0.53, 0.33), (0.53, 0.44), (0.47, 0.44)), fill=TORII_TOP)
    # kasagi (górna belka z uniesionymi końcami)
    draw.polygon(
        P((0.10, 0.30), (0.14, 0.22), (0.86, 0.22), (0.90, 0.30),
          (0.90, 0.36), (0.10, 0.36)),
        fill=TORII_TOP,
    )


def make_icon(size: int = 512) -> Image.Image:
    img = _vgrad(size, size, BG, BG2)

    # poświata
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([size * 0.12, size * 0.16, size * 0.88, size * 0.92],
               fill=(88, 158, 255, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.09))
    img.alpha_composite(glow)

    draw = ImageDraw.Draw(img)
    draw_torii(draw, 0, 0, size)

    # rama z zaokrąglonym narożnikiem
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.18), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    d2 = ImageDraw.Draw(out)
    d2.rounded_rectangle([2, 2, size - 3, size - 3], radius=int(size * 0.18),
                         outline=(88, 158, 255, 140), width=max(2, size // 128))
    return out


def make_banner(w: int = 1280, h: int = 640) -> Image.Image:
    img = _vgrad(w, h, (11, 14, 20, 255), (19, 26, 38, 255))
    draw = ImageDraw.Draw(img)

    # siatka + gwiazdy
    rnd = random.Random(7)
    for x in range(0, w, 64):
        draw.line([(x, 0), (x, h)], fill=(255, 255, 255, 7), width=1)
    for y in range(0, h, 64):
        draw.line([(0, y), (w, y)], fill=(255, 255, 255, 7), width=1)
    for _ in range(140):
        x, y = rnd.randint(0, w - 1), rnd.randint(0, h - 1)
        a = rnd.randint(18, 90)
        draw.point((x, y), fill=(200, 220, 255, a))

    # torii po lewej z poświatą
    icon = make_icon(420)
    glow = Image.new("RGBA", (520, 520), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([40, 60, 480, 500], fill=(88, 158, 255, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    img.alpha_composite(glow, (60, 60))
    img.alpha_composite(icon, (110, 110))

    # tytuł z poświatą (auto-dopasowanie, żeby nie uciekł za krawędź)
    tx, ty = 560, 210
    max_w = w - tx - 60

    size = 104
    f_title = _font(size, bold=True)
    while size > 40 and draw.textlength("ANIME EXODUS", font=f_title) > max_w:
        size -= 4
        f_title = _font(size, bold=True)

    sub = "ogladajanime.pl  →  MyAnimeList / AniList"
    ssize = 36
    f_sub = _font(ssize, bold=False)
    while ssize > 18 and draw.textlength(sub, font=f_sub) > max_w:
        ssize -= 2
        f_sub = _font(ssize, bold=False)

    tag = "CSV → AniList lookup → MAL XML   •   DearPyGui   •   buildy EXE (Nuitka)"
    tsize = 28
    f_tag = _font(tsize, bold=False)
    while tsize > 14 and draw.textlength(tag, font=f_tag) > max_w:
        tsize -= 2
        f_tag = _font(tsize, bold=False)

    glow_txt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gtd = ImageDraw.Draw(glow_txt)
    gtd.text((tx, ty), "ANIME EXODUS", font=f_title, fill=(88, 158, 255, 160))
    glow_txt = glow_txt.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(glow_txt)
    draw = ImageDraw.Draw(img)
    draw.text((tx, ty), "ANIME EXODUS", font=f_title, fill=TEXT_W)
    draw.text((tx, ty + int(size * 1.35)), sub, font=f_sub, fill=MUTED)
    draw.text((tx, ty + int(size * 1.35) + ssize + 18), tag, font=f_tag, fill=BLUE)

    # dolna linia akcentowa
    grad = _hgrad(w - 120, 6, CYAN, VIOLET)
    img.alpha_composite(grad, (60, h - 46))
    return img


def main() -> None:
    icon = make_icon(512)
    icon.save(ASSETS / "icon.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(ASSETS / "icon.ico", sizes=sizes)
    make_banner().save(ASSETS / "banner.png")
    print("OK:", ASSETS / "icon.png", ASSETS / "icon.ico", ASSETS / "banner.png")


if __name__ == "__main__":
    main()
