#!/usr/bin/env python3
"""Align a generated photo chain to a real clean plate and isolate each changed region. Estimate affine alignment outside supplied boxes, match colors, and derive connected masks."""
import argparse
import os
import sys
import numpy as np
try:
    import cv2
except ImportError:
    sys.exit('error: OpenCV missing. Use the engine Python: uv run --directory <SKILL> python align_photo.py ...')

def read_image(p, W, H):
    im = cv2.imread(p, cv2.IMREAD_COLOR)
    if im is None:
        sys.exit(f'error: could not read {p}')
    if im.shape[:2] != (H, W):
        im = cv2.resize(im, (W, H), interpolation=cv2.INTER_AREA)
    return im.astype(np.float32)

def gray(a):
    return cv2.cvtColor(np.clip(a, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)

def align(img, ref, region=None, scale_filter=0.5):
    """Estimate the affine transform from generated img to real ref in unchanged regions (mask = 1)."""
    (H, W) = ref.shape[:2]

    def prep(a):
        g = cv2.resize(gray(a), None, fx=scale_filter, fy=scale_filter, interpolation=cv2.INTER_AREA).astype(np.float32)
        g = cv2.GaussianBlur(g, (0, 0), 1.2)
        (gx, gy) = (cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
        return cv2.GaussianBlur(np.sqrt(gx * gx + gy * gy), (0, 0), 1.0)
    (a, b) = (prep(img), prep(ref))
    m = None if region is None else (cv2.resize(region.astype(np.uint8), (a.shape[1], a.shape[0]), interpolation=cv2.INTER_NEAREST) > 0).astype(np.uint8)
    Wm = np.eye(2, 3, dtype=np.float32)
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-06)
    try:
        (cc, Wm) = cv2.findTransformECC(b, a, Wm, cv2.MOTION_AFFINE, crit, m, 5)
    except cv2.error as e:
        print('  warning: alignment failed; retaining the original photo:', str(e)[:80])
        return (img.copy(), 0.0, np.eye(3, dtype=np.float32))
    S = np.diag([scale_filter, scale_filter, 1]).astype(np.float32)
    Si = np.diag([1 / scale_filter, 1 / scale_filter, 1]).astype(np.float32)
    Mf = Si @ np.vstack([Wm, [0, 0, 1]]).astype(np.float32) @ S
    out = cv2.warpPerspective(img, Mf, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    return (out, float(cc), Mf)

def match_color(src, ref, region, steps=3, lim=28):
    (H, W) = ref.shape[:2]
    (ys, xs) = np.nonzero(region)
    if len(ys) < 1000:
        return (src, None)
    if len(ys) > 400000:
        k = np.random.RandomState(1).choice(len(ys), 400000, replace=False)
        (ys, xs) = (ys[k], xs[k])
    A = np.concatenate([src[ys, xs], np.ones((len(ys), 1), np.float32)], 1)
    B = ref[ys, xs]
    ok = np.ones(len(ys), bool)
    M = None
    for _ in range(steps):
        (M, *_) = np.linalg.lstsq(A[ok], B[ok], rcond=None)
        ok = np.abs(A @ M - B).max(1) < lim
    out = np.concatenate([src.reshape(-1, 3), np.ones((W * H, 1), np.float32)], 1) @ M
    return (out.reshape(H, W, 3).astype(np.float32), M)

def main():
    ap = argparse.ArgumentParser(description="Align the photo chain to a clean plate and extract each step's mask.")
    ap.add_argument('--plate', required=True, help='Clean plate of the real environment without the person')
    ap.add_argument('--photos', nargs='+', required=True, help='Generated photos in generation order')
    ap.add_argument('--boxes', nargs='+', required=True, help='One box per photo: x0,y0,x1,y1 as image percentages')
    ap.add_argument('--folder', required=True)
    ap.add_argument('--lim', type=float, default=38, help='Difference threshold for a definite change (default 38)')
    ap.add_argument('--close', type=int, default=33, help='Mask closing radius in pixels, filling holes in similarly colored surfaces')
    a = ap.parse_args()
    if len(a.boxes) != len(a.photos):
        sys.exit('error: supply one box per photo')
    os.makedirs(a.folder, exist_ok=True)
    P = cv2.imread(a.plate, cv2.IMREAD_COLOR)
    if P is None:
        sys.exit(f'error: could not read {a.plate}')
    (H, W) = P.shape[:2]
    P = P.astype(np.float32)
    previous = P
    for (i, (fp, cx)) in enumerate(zip(a.photos, a.boxes), 1):
        F = read_image(fp, W, H)
        (x0, y0, x1, y1) = [float(v) for v in cx.split(',')]
        box = np.zeros((H, W), np.uint8)
        box[int(y0 / 100 * H):int(y1 / 100 * H), int(x0 / 100 * W):int(x1 / 100 * W)] = 1
        outside = (1 - cv2.dilate(box, np.ones((41, 41), np.uint8))).astype(np.uint8)
        (Fa, cc, _) = align(F, previous, outside)
        (Fc, M) = match_color(Fa, previous, outside)
        residual = np.abs(Fc - previous).max(2)[outside > 0].mean()
        d = cv2.GaussianBlur(np.abs(Fc - previous).max(2), (0, 0), 2.0)
        threshold = (d > a.lim).astype(np.uint8) * box
        threshold = cv2.morphologyEx(threshold, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        threshold = cv2.morphologyEx(threshold, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (a.close, a.close)))
        (n, lab, st, _) = cv2.connectedComponentsWithStats(threshold, 8)
        if n > 1:
            largest = st[1:, cv2.CC_STAT_AREA].max()
            threshold = np.isin(lab, np.where(st[:, cv2.CC_STAT_AREA] >= 0.05 * largest)[0][1:]).astype(np.uint8)
        inv = np.pad(1 - threshold, 1, constant_values=1)
        mk = np.zeros((H + 4, W + 4), np.uint8)
        cv2.floodFill(inv, mk, (0, 0), 2, flags=4)
        threshold = threshold | (inv[1:-1, 1:-1] == 1).astype(np.uint8)
        smooth = np.clip((d - a.lim * 0.35) / (a.lim * 0.65), 0, 1) * cv2.dilate(threshold, np.ones((25, 25), np.uint8))
        mask = np.maximum(threshold.astype(np.float32), smooth * 0.85)
        mask = cv2.GaussianBlur(mask, (0, 0), 2.5) * box
        base = os.path.join(a.folder, f'{i}')
        cv2.imwrite(base + '_photo.png', np.clip(Fc, 0, 255).astype(np.uint8))
        cv2.imwrite(base + '_mask.png', np.clip(mask * 255, 0, 255).astype(np.uint8))
        preview = np.clip(Fc * (0.35 + 0.65 * mask[..., None]), 0, 255).astype(np.uint8)
        conf = np.concatenate([np.clip(Fc, 0, 255).astype(np.uint8), preview], 1)
        cv2.imwrite(base + '_preview.jpg', cv2.resize(conf, (conf.shape[1] // 3, conf.shape[0] // 3)), [cv2.IMWRITE_JPEG_QUALITY, 85])
        print(f'{i}: {os.path.basename(fp)} · alignment {cc:.3f} · residual color outside box {residual:.1f} · mask {mask.mean() * 100:.1f}% of image')
        previous = Fc
    print(f'ok {a.folder}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
