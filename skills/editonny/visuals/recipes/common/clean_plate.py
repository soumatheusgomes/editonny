#!/usr/bin/env python3
"""Recover a clean background from a fixed-camera video using person masks and temporal samples. Inpaint never-visible pixels; inspect the accompanying missing-region image. Requires MediaPipe 0.10.14, numpy<2 and OpenCV."""
import argparse
import os
import subprocess
import sys
import numpy as np

def main():
    ap = argparse.ArgumentParser(description='Recover a clean background from fixed-camera footage.')
    ap.add_argument('video')
    ap.add_argument('--output', required=True, help='Output clean-plate PNG')
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--end', type=float)
    ap.add_argument('--interval', type=float, default=1.5, help='Sample one frame every this many seconds (default 1.5)')
    ap.add_argument('--dilate', type=int, default=91, help='Person-mask dilation in pixels (default 91, covering hair and shadows)')
    a = ap.parse_args()
    try:
        import cv2
        import mediapipe as mp
    except ImportError:
        sys.exit('error: MediaPipe missing. Run: uv run --no-project --python 3.11 --with "mediapipe==0.10.14" --with "numpy<2" --with opencv-python-headless python clean_plate.py ...')
    pr = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', a.video], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if pr.returncode != 0:
        sys.exit(f'error: could not read {a.video}')
    (W, H) = map(int, pr.stdout.strip().split(','))
    if W * H > 1080 * 1920:
        k = (1080 * 1920 / (W * H)) ** 0.5
        (W, H) = (int(W * k) // 2 * 2, int(H * k) // 2 * 2)
    cmd = ['ffmpeg', '-v', 'error', '-ss', str(a.start)] + (['-to', str(a.end)] if a.end else []) + ['-i', a.video, '-vf', f'fps=1/{a.interval},scale={W}:{H}:flags=lanczos', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    raw = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    seg = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=0)
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (a.dilate, a.dilate))
    (frs, mks) = ([], [])
    while True:
        b = raw.stdout.read(W * H * 3)
        if len(b) < W * H * 3:
            break
        f = np.frombuffer(b, np.uint8).reshape(H, W, 3)
        m = seg.process(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)).segmentation_mask
        frs.append(f.copy())
        mks.append(cv2.dilate((m > 0.2).astype(np.uint8), ker))
    if len(frs) < 5:
        sys.exit(f'error: only {len(frs)} frames; use a longer segment or shorter --interval')
    print(f'{len(frs)} frames')
    F = np.stack(frs).astype(np.float32)
    M = (1 - np.stack(mks)).astype(np.float32)[..., None]
    cnt = M.sum(0)
    m1 = (F * M).sum(0) / np.maximum(cnt, 1)
    ok = (np.abs(F - m1[None]).max(-1, keepdims=True) < 26).astype(np.float32) * M
    cnt2 = ok.sum(0)
    m2 = (F * ok).sum(0) / np.maximum(cnt2, 1)
    missing = (cnt2[..., 0] < 3).astype(np.uint8)
    print(f'Pixels never visible without the person: {missing.mean() * 100:.2f}% (inpainted; inspect _missing.png)')
    out = np.clip(m2, 0, 255).astype(np.uint8)
    if missing.any():
        out = cv2.inpaint(out, cv2.dilate(missing, np.ones((5, 5), np.uint8)), 7, cv2.INPAINT_TELEA)
    base = os.path.splitext(a.output)[0]
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    for (path, image) in ((a.output, out), (base + '_coverage.png', np.clip(cnt2[..., 0] * 4, 0, 255).astype(np.uint8)), (base + '_missing.png', missing * 255)):
        if not cv2.imwrite(path, image):
            sys.exit(f'error: could not write {path}')
    print(f'ok {a.output} ({W}x{H})')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
