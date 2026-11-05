#!/usr/bin/env python3
"""Draw a white text sticker on a 1080x1920 green plate as an image-generation reference."""
import argparse
import os
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FONT = os.path.join(HERE, '..', '..', 'assets', 'visuals', 'fonts', 'Sora.ttf')
GREEN = (0, 177, 64)

def main():
    ap = argparse.ArgumentParser(description='Draw a white text sticker on portrait green for an image reference.')
    ap.add_argument('--lines', required=True, help='Lines separated by a vertical bar, e.g. Your editor|now uses AI')
    ap.add_argument('--output', required=True, help='1080x1920 PNG to submit as start_image')
    ap.add_argument('--x', type=int, default=540, help='Sticker center in pixels (default 540)')
    ap.add_argument('--y', type=int, default=268, help='Sticker top in pixels (default 268)')
    ap.add_argument('--size', type=int, default=56, help='Font size in pixels (default 56)')
    ap.add_argument('--weight', type=int, default=800, help='Variable font weight (default 800)')
    ap.add_argument('--font', default=DEFAULT_FONT, help='TTF file (default: bundled Sora)')
    ap.add_argument('--width', type=int, default=1080)
    ap.add_argument('--height', type=int, default=1920)
    a = ap.parse_args()
    if not os.path.exists(a.font):
        sys.exit(f'error: font not found: {a.font}; supply --font /path/to/font.ttf')
    f = ImageFont.truetype(a.font, a.size)
    try:
        f.set_variation_by_axes([a.weight])
    except Exception:
        pass
    rows = [l.strip() for l in a.lines.split('|') if l.strip()]
    d0 = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    ws = [d0.textlength(l, font=f) for l in rows]
    (pad_x, pad_y, spacing) = (int(a.size * 0.5), int(a.size * 0.32), int(a.size * 1.4))
    W = int(max(ws) + 2 * pad_x)
    H = int(len(rows) * spacing + 2 * pad_y - (spacing - a.size * 1.15))
    if W > a.width - 80:
        print(f'warning: sticker is {W} pixels wide; reduce --size or wrap lines (safe zone: x 140 to 940)')
    radius = int(a.size * 0.45)
    st = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(st)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=(255, 255, 255, 255), outline=(222, 222, 222, 255), width=max(2, a.size // 30))
    y = pad_y
    for (i, l) in enumerate(rows):
        d.text(((W - ws[i]) / 2, y), l, font=f, fill=(12, 12, 12, 255))
        y += spacing
    st.save(os.path.splitext(a.output)[0] + '_sticker.png')
    cv = Image.new('RGBA', (a.width, a.height), GREEN + (255,))
    sh = Image.new('RGBA', (W + 80, H + 80), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([40, 52, W + 40, H + 52], radius=radius, fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    X = a.x - W // 2
    Y = a.y
    cv.alpha_composite(sh, (X - 40, Y - 40))
    cv.alpha_composite(st, (X, Y))
    cv.convert('RGB').save(a.output)
    print(f'ok {a.output}: sticker {W}x{H} at x {X}..{X + W}, y {Y}..{Y + H}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
