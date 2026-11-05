#!/usr/bin/env python3
"""Extract rotating objects from black into transparent VP9 WebM or a test PNG. Preserve dark interior surfaces by filling enclosed holes; support soft glow and a piecewise output-to-source time map."""
import argparse
import subprocess
import sys
import numpy as np
try:
    import cv2
except ImportError:
    sys.exit('error: OpenCV missing. Use engine Python or install opencv-python-headless.')
CROSS = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))

def fill_holes(m):
    """Fill background regions that do not touch the frame boundary."""
    (h, w) = m.shape
    inv = np.pad((~m).astype(np.uint8), 1, constant_values=1)
    mask = np.zeros((h + 4, w + 4), np.uint8)
    cv2.floodFill(inv, mask, (0, 0), 2, flags=4)
    return m | (inv[1:-1, 1:-1] == 1)

def largest_components(m, frac=0.03):
    (n, lab, st, _) = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=4)
    if n <= 1:
        return m
    areas = st[1:, cv2.CC_STAT_AREA]
    keep = np.where(areas >= frac * areas.max())[0] + 1
    return np.isin(lab, keep)

def key_frame(rgb, a):
    L = a.side
    f = cv2.resize(rgb, (L, L), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    lum = f.max(axis=2)
    m = lum > a.lim
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, CROSS, iterations=1 if a.holes == 'open' else 3) > 0
    m = largest_components(m)
    if a.holes == 'filled':
        m = fill_holes(m)
    elif a.holes == 'auto':
        filled = fill_holes(m)
        (n, lab) = cv2.connectedComponents((filled & ~m).astype(np.uint8), connectivity=4)
        solid = m.copy()
        for i in range(1, n):
            r = lab == i
            if r.sum() < 0.00012 * L * L or np.median(lum[r]) >= a.background:
                solid |= r
        m = solid
    core = cv2.GaussianBlur(cv2.erode(m.astype(np.uint8), CROSS, iterations=1).astype(np.float32), (0, 0), 1.2)
    soft = np.clip((lum - a.smooth) / a.band, 0, 1)
    al = np.maximum(core, soft)
    far = cv2.dilate(m.astype(np.uint8), CROSS, iterations=max(1, int(L * a.distant))) > 0
    al = np.where(far, al, 0)
    e = np.minimum.outer(np.minimum(np.arange(L), np.arange(L)[::-1]), np.minimum(np.arange(L), np.arange(L)[::-1])).astype(np.float32)
    al = al * np.clip(e / max(1.0, L * a.border), 0, 1) ** 1.5
    rgb_o = np.where(al[..., None] > 0.001, np.clip(f / np.maximum(al[..., None], 0.001), 0, 1), 0)
    rgb_o = np.where(core[..., None] > 0.99, f, rgb_o)
    return np.dstack([rgb_o, al])

def read_frame(path):
    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
        im = cv2.imread(path, cv2.IMREAD_COLOR)
        if im is None:
            sys.exit(f'error: could not read {path}')
        return ([im[:, :, ::-1]], 1.0)
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    fr = []
    while True:
        (ok, im) = cap.read()
        if not ok:
            break
        fr.append(im[:, :, ::-1])
    if not fr:
        sys.exit(f'error: no frames read from {path}')
    return (fr, fps)

def main():
    ap = argparse.ArgumentParser(description='Remove black background from a 3D object and save transparent VP9 WebM.')
    ap.add_argument('input_file')
    ap.add_argument('output', help='WebM video or a PNG test frame')
    ap.add_argument('--side', type=int, default=720, help='Square output size (default 720, matching bundled spins)')
    ap.add_argument('--mapping', default='0:0', help='Output:source time pairs in seconds')
    ap.add_argument('--dur', type=float, help='Output duration (default: source duration)')
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--lim', type=float, default=0.07, help='Solid silhouette luminance (sun: 0.5)')
    ap.add_argument('--smooth', type=float, default=0.025, help='Soft-glow start threshold')
    ap.add_argument('--band', type=float, default=0.1, help='Soft-glow width')
    ap.add_argument('--distant', type=float, default=0.05, help='Glow reach as a fraction of output side')
    ap.add_argument('--border', type=float, default=0.09, help='Edge-fade width as a fraction of output side')
    ap.add_argument('--holes', choices=['filled', 'open', 'auto'], default='filled')
    ap.add_argument('--background', type=float, default=0.022, help='Auto-mode holes below this luminance become background')
    ap.add_argument('--png-at', type=float, default=0.0, help='Source time for a PNG test frame')
    ap.add_argument('--crf', type=int, default=24)
    a = ap.parse_args()
    (fr, sfps) = read_frame(a.input_file)
    if a.output.lower().endswith('.png'):
        i = min(len(fr) - 1, int(round(a.png_at * sfps)))
        o = key_frame(fr[i], a)
        cv2.imwrite(a.output, (np.dstack([o[..., 2::-1], o[..., 3:]]) * 255).astype(np.uint8))
        print(f'ok {a.output} (frame {i})')
        return
    pairs = [tuple(map(float, p.split(':'))) for p in a.mapping.split(',')]
    (xs, ys) = ([p[0] for p in pairs], [p[1] for p in pairs])
    dur = a.dur or (len(fr) / sfps if len(pairs) == 1 else xs[-1] + (len(fr) / sfps - ys[-1]))
    n = int(round(dur * a.fps))
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{a.side}x{a.side}', '-r', str(a.fps), '-i', '-', '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-b:v', '0', '-crf', str(a.crf), '-row-mt', '1', '-deadline', 'good', '-cpu-used', '3', '-auto-alt-ref', '0', '-metadata:s:v:0', 'alpha_mode=1', a.output], stdin=subprocess.PIPE)
    for k in range(n):
        t = k / a.fps
        if len(pairs) == 1:
            ts = t + ys[0] - xs[0]
        else:
            ts = float(np.interp(t, xs, ys)) if t <= xs[-1] else ys[-1] + (t - xs[-1])
        i = min(len(fr) - 1, max(0, int(round(ts * sfps))))
        p.stdin.write((key_frame(fr[i], a) * 255).astype(np.uint8).tobytes())
    p.stdin.close()
    p.wait()
    print(f'ok {a.output}: {n} frames ({dur:.2f} s), {a.side}x{a.side}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
