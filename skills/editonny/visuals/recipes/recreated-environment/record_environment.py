#!/usr/bin/env python3
"""Composite an aligned environment photo chain into fixed-camera footage. Behind-person updates use V + beta * changedMask * (newBackground - oldBackground), with a color-derived edge ceiling to avoid dark fringes. Foreground updates use ordinary alpha blending. Steps accept appear/from-bottom/from-top/from-left/from-right, optionally +front. --test saves selected preview frames only."""
import argparse
import json
import os
import subprocess
import sys
import numpy as np
try:
    import cv2
except ImportError:
    sys.exit('error: OpenCV missing. Use the engine Python: uv run --directory <SKILL> python record_environment.py ...')

def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'csv=p=0', path], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip().split(',')
    if len(out) < 3:
        sys.exit(f'error: could not read {path}')
    (n, d) = out[2].split('/')
    return (int(out[0]), int(out[1]), float(n) / float(d))

def smooth(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)

def input_file(step, t, yy, xx):
    """Return the revealed fraction of each pixel (0..1) at time t."""
    (t0, dur, entrance) = (step['t'], max(step['dur'], 0.001), step['entrance'])
    u = (t - t0) / dur
    if u <= 0:
        return None
    if u >= 1:
        return 1.0
    (x0, y0, x1, y1) = step['box']
    feather = 22.0
    if entrance == 'appear':
        return float(smooth(u))
    if entrance == 'from-bottom':
        front = y1 + feather - (y1 - y0 + 2 * feather) * smooth(u)
        return np.clip((yy - front) / feather + 0.5, 0, 1)
    if entrance == 'from-top':
        front = y0 - feather + (y1 - y0 + 2 * feather) * smooth(u)
        return np.clip((front - yy) / feather + 0.5, 0, 1)
    if entrance == 'from-left':
        front = x0 - feather + (x1 - x0 + 2 * feather) * smooth(u)
        return np.clip((front - xx) / feather + 0.5, 0, 1)
    front = x1 + feather - (x1 - x0 + 2 * feather) * smooth(u)
    return np.clip((xx - front) / feather + 0.5, 0, 1)

def main():
    ap = argparse.ArgumentParser(description='Render environment changes into a video segment.')
    ap.add_argument('--video', required=True)
    ap.add_argument('--start', type=float, required=True)
    ap.add_argument('--end', type=float, required=True)
    ap.add_argument('--plate', required=True, help='Clean plate with matching framing')
    ap.add_argument('--cutout', help='Transparent person.webm or grayscale cutout video')
    ap.add_argument('--cutout-start', type=float, help='Cut time where the cutout starts (default: start from person.json)')
    ap.add_argument('--step', nargs=5, action='append', required=True, metavar=('PHOTO', 'MASK', 'TIME', 'DURATION', 'ENTRANCE'))
    ap.add_argument('--shrink', type=int, default=1, help='Shrink cutout by this many pixels to remove old-background fringes')
    ap.add_argument('--output', required=True)
    ap.add_argument('--test', help='Comma-separated cut times: save only these frames as JPEG previews')
    a = ap.parse_args()
    (W, H, fps) = probe(a.video)
    f0 = int(round(a.start * fps))
    f1 = int(round(a.end * fps))
    N = f1 - f0
    if N <= 0:
        sys.exit('error: --end must follow --start')
    start_time = f0 / fps
    P = cv2.imread(a.plate, cv2.IMREAD_COLOR)
    if P is None:
        sys.exit(f'error: could not read {a.plate}')
    P = cv2.resize(P, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32)
    (yy, xx) = np.mgrid[0:H, 0:W].astype(np.float32)
    steps = []
    for (photo, mask, seg, dur, entrance) in a.step:
        front = entrance.endswith('+front')
        entrance = entrance.replace('+front', '')
        if entrance not in ('appear', 'from-bottom', 'from-top', 'from-left', 'from-right'):
            sys.exit(f'error: unknown entrance "{entrance}"')
        F = cv2.imread(photo, cv2.IMREAD_COLOR)
        M = cv2.imread(mask, cv2.IMREAD_GRAYSCALE)
        if F is None or M is None:
            sys.exit(f'error: could not read {photo} or {mask}')
        F = cv2.resize(F, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32)
        M = cv2.resize(M, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        (ys, xs) = np.nonzero(M > 0.05)
        box = (xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else (0, 0, W, H)
        steps.append(dict(F=F, M=M, t=float(seg), dur=float(dur), entrance=entrance, front=front or not a.cutout, box=box))
    steps.sort(key=lambda p: p['t'])

    def read_video():
        cmd = ['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, (f0 - 0.5) / fps):.6f}', '-i', a.video, '-frames:v', str(N), '-vf', f'scale={W}:{H}', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if a.test else None)
        for _ in range(N):
            b = p.stdout.read(W * H * 3)
            if len(b) < W * H * 3:
                break
            yield np.frombuffer(b, np.uint8).reshape(H, W, 3)

    def read_cutout():
        if not a.cutout:
            while True:
                yield None
        start = a.cutout_start
        if start is None:
            js = os.path.splitext(a.cutout)[0] + '.json'
            start = json.load(open(js, encoding='utf-8')).get('start', 0.0) if os.path.exists(js) else 0.0
        ss = max(0.0, start_time - start)
        webm = a.cutout.lower().endswith('.webm')
        cmd = ['ffmpeg', '-v', 'error'] + (['-c:v', 'libvpx-vp9'] if webm else []) + ['-ss', f'{ss:.6f}', '-i', a.cutout, '-frames:v', str(N), '-vf', ('alphaextract,' if webm else '') + f'format=gray,scale={W}:{H}', '-f', 'rawvideo', '-']
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if a.test else None)
        for _ in range(N):
            b = p.stdout.read(W * H)
            if len(b) < W * H:
                print('warning: cutout ended early; remaining frames have no person mask')
                break
            yield np.frombuffer(b, np.uint8).reshape(H, W)
        while True:
            yield None
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * a.shrink + 1, 2 * a.shrink + 1)) if a.shrink else None

    def person(m):
        b = (m > 127).astype(np.uint8)
        (n, lab, st, _) = cv2.connectedComponentsWithStats(b, 8)
        al = m.astype(np.float32) / 255
        if n > 2:
            al = al * (lab == 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))).astype(np.float32)
        if ker is not None:
            al = cv2.erode(al, ker)
        return cv2.GaussianBlur(al, (0, 0), 0.9)

    def composite(V, m, t):
        Vf = V.astype(np.float32)
        behind = [(p, input_file(p, t, yy, xx)) for p in steps if not p['front']]
        behind = [(p, e) for (p, e) in behind if e is not None]
        out = Vf
        if behind and m is not None:
            al = person(m)
            wb = np.clip((al - 0.02) / 0.18, 0, 1)[..., None]
            Bv = Vf * (1 - wb) + P * wb
            B = Bv.copy()
            Mc = np.zeros((H, W), np.float32)
            for (p, e) in behind:
                s = p['M'] * e
                B = B + (p['F'] - B) * s[..., None]
                Mc = np.maximum(Mc, s)
            E = 8.0
            lim = np.minimum(((Vf + E) / (Bv + E)).min(2), ((255 - Vf + E) / (255 - Bv + E)).min(2))
            inside = (al > 0.97).astype(np.float32)
            den = cv2.GaussianBlur(inside, (0, 0), 6)
            Fp = cv2.GaussianBlur(Vf * inside[..., None], (0, 0), 6) / np.maximum(den, 0.001)[..., None]
            dBF = Bv - Fp
            color_limit = np.clip(((Vf - Fp) * dBF).sum(2) / ((dBF * dBF).sum(2) + 90.0), 0, 1)
            ceiling = np.minimum(np.clip(lim * 1.1 + 0.03, 0, 1), np.clip(color_limit * 1.25 + 0.05, 0, 1))
            wq = np.clip((al - 0.1) / 0.2, 0, 1) * np.clip((den - 0.03) / 0.05, 0, 1)
            beta = (1 - al) * (1 - wq) + np.minimum(1 - al, ceiling) * wq
            out = Vf + beta[..., None] * (Mc[..., None] * (B - Bv))
        elif behind:
            for (p, e) in behind:
                s = (p['M'] * e)[..., None]
                out = out * (1 - s) + p['F'] * s
        for p in steps:
            if p['front']:
                e = input_file(p, t, yy, xx)
                if e is not None:
                    s = (p['M'] * e)[..., None]
                    out = out * (1 - s) + p['F'] * s
        return np.clip(out, 0, 255).astype(np.uint8)
    tests = [float(x) for x in a.test.split(',')] if a.test else None
    targets = {int(round(x * fps)) - f0: x for x in tests} if tests else None
    enc = None
    if not tests:
        os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
        enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', f'{fps:.6f}', '-i', '-', '-an', '-c:v', 'libx264', '-crf', '14', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-g', '30', '-keyint_min', '30', '-movflags', '+faststart', a.output], stdin=subprocess.PIPE)
    k = 0
    for (V, m) in zip(read_video(), read_cutout()):
        t = (f0 + k) / fps
        if tests:
            if k in targets:
                p = os.path.splitext(a.output)[0] + f'_test_{targets[k]:.2f}.jpg'
                cv2.imwrite(p, cv2.resize(composite(V, m, t), (W // 2, H // 2)), [cv2.IMWRITE_JPEG_QUALITY, 88])
                print('ok', p)
        else:
            enc.stdin.write(composite(V, m, t).tobytes())
        k += 1
        if tests and k > max(targets):
            break
    if enc:
        enc.stdin.close()
        enc.wait()
        print(f'ok {a.output}: {k} frames from {start_time:.3f} to {(f0 + k) / fps:.3f} s in the cut (custom scene start: {np.floor(start_time * 1000) / 1000:.3f})')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
