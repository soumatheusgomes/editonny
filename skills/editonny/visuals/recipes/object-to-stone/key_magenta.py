#!/usr/bin/env python3
"""Remove a pure magenta background and pink spill, preserving gray stone and pale objects. Save transparent PNG; --trim keeps an eight-pixel margin and --width resizes proportionally."""
import argparse
import sys
import numpy as np
from PIL import Image

def main():
    ap = argparse.ArgumentParser(description='Remove magenta background and save transparent PNG.')
    ap.add_argument('input_file')
    ap.add_argument('output')
    ap.add_argument('--trim', action='store_true', help='Trim transparent margins')
    ap.add_argument('--width', type=int, help='Final width in pixels; preserves aspect ratio')
    a = ap.parse_args()
    try:
        im = np.asarray(Image.open(a.input_file).convert('RGB')).astype(np.float32)
    except OSError:
        sys.exit(f'error: could not read {a.input_file}')
    (r, g, b) = (im[..., 0], im[..., 1], im[..., 2])
    mag = np.clip((np.minimum(r, b) - g) / 110.0, 0, 1)
    corner = np.concatenate([mag[:20, :20].ravel(), mag[:20, -20:].ravel(), mag[-20:, :20].ravel(), mag[-20:, -20:].ravel()])
    if np.median(corner) < 0.5:
        print('warning: corners are not magenta; verify a solid #FF00FF background')
    alpha = 1 - np.clip((mag - 0.25) / 0.5, 0, 1)
    k = mag > 0.05
    r2 = np.where(k, np.minimum(r, g + (r - g) * (1 - mag)), r)
    b2 = np.where(k, np.minimum(b, g + (b - g) * (1 - mag)), b)
    rgba = np.dstack([r2, g, b2, alpha * 255]).clip(0, 255).astype(np.uint8)
    out = Image.fromarray(rgba)
    if a.trim:
        (ys, xs) = np.nonzero(alpha > 0.05)
        if len(xs):
            m = 8
            out = out.crop((max(0, xs.min() - m), max(0, ys.min() - m), min(out.width, xs.max() + m + 1), min(out.height, ys.max() + m + 1)))
    if a.width:
        out = out.resize((a.width, max(1, round(out.height * a.width / out.width))), Image.LANCZOS)
    out.save(a.output)
    print(f'ok {a.output}: {out.width}x{out.height}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
