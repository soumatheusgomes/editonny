#!/usr/bin/env python3
"""Track hands with MediaPipe and export assets/hands.json.

Run: uv run --python 3.12 --with mediapipe==0.10.21 --with numpy python
     tools/track_hands.py --edit EDIT [--start SECONDS --end SECONDS]

Python 3.12 and MediaPipe 0.10.21 are intentionally pinned. First use downloads
the local runtime and hand model. Tracks reset at each cut; imageLeft/imageRight
name the starting image side, independently of mirrored handedness. Centered
three-frame smoothing never crosses a cut. Output includes palm, nodes, and pts."""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
MODEL_URL = 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task'
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
PALM = (0, 5, 9, 13, 17)
COMMAND = "uv run --python 3.12 --with mediapipe==0.10.21 --with numpy python tools/track_hands.py --edit '<EDIT>' --start 8 --end 13"
MEDIAPIPE = '0.10.21'

def project_engine():
    """Project engine."""
    try:
        with open(os.path.join(PROJECT, 'project.json'), encoding='utf-8') as f:
            s = str(json.load(f).get('skill') or '').strip()
        if s:
            return (os.path.abspath(os.path.expanduser(s)), 'project.json ("skill")')
    except (OSError, ValueError, AttributeError):
        pass
    parent = os.path.dirname(PROJECT)
    if os.path.basename(PROJECT) == 'project-template' and os.path.exists(os.path.join(parent, 'helpers', 'matte.py')):
        return (parent, 'the engine containing this template')
    return (None, '')

def default_model(skill=None):
    """Default model."""
    if skill:
        in_engine = os.path.join(skill, 'assets', 'models', 'hand_landmarker.task')
        if os.path.exists(in_engine):
            return in_engine
    return os.path.join(os.path.expanduser('~'), '.editonny', 'models', 'hand_landmarker.task')
KNUCKLES = (5, 9, 13, 17)

def media(pts, idx):
    return [sum((pts[i][0] for i in idx)) / len(idx), sum((pts[i][1] for i in idx)) / len(idx)]

def main():
    ap = argparse.ArgumentParser(description='Track hands for an attached object')
    ap.add_argument('--edit', required=True)
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--end', type=float, default=None)
    ap.add_argument('--model', default=None, help='hand_landmarker.task (default: engine assets/models, then ~/.editonny/models)')
    ap.add_argument('--output', default=os.path.join(PROJECT, 'assets', 'hands.json'))
    a = ap.parse_args()
    (skill, source) = project_engine()
    print(f'skill: {skill} ({source})' if skill else 'No engine recorded in project.json; hand model comes from ~/.editonny/models', flush=True)
    a.model = a.model or default_model(skill)
    try:
        import numpy as np
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
    except ImportError:
        print(f'error: MediaPipe is missing. Run (first use downloads packages):\n  {COMMAND}', file=sys.stderr)
        return 2
    version = getattr(mp, '__version__', '?')
    if version != MEDIAPIPE or sys.version_info[:2] != (3, 12):
        print(f'warning: this tool was verified with MediaPipe {MEDIAPIPE} on Python 3.12 (current: MediaPipe {version}, Python {sys.version_info[0]}.{sys.version_info[1]}). O mediapipe 1.x aborta count "Service is unavailable". If it fails, run:\n  {COMMAND}', file=sys.stderr, flush=True)
    edit = os.path.abspath(os.path.expanduser(a.edit))
    with open(os.path.join(edit, 'cut_timeline.json'), encoding='utf-8') as f:
        tl = json.load(f)
    (fps, W, H, dur) = (tl['fps'], tl['width'], tl['height'], tl['duration'])
    end = min(a.end if a.end is not None else dur, dur)
    (f0, f1) = (int(round(a.start * fps)), int(round(end * fps)))
    cuts = sorted((int(s['out_start_frame']) if 'out_start_frame' in s else int(round(s['out_start'] * fps)) for s in tl['segments'] if s['out_start'] > 0.01))
    if not os.path.exists(a.model):
        os.makedirs(os.path.dirname(a.model), exist_ok=True)
        print('Downloading the MediaPipe hand model (about 8 MB)...', flush=True)
        urllib.request.urlretrieve(MODEL_URL, a.model)

    def create_tracker():
        op = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=a.model), num_hands=2, running_mode=vision.RunningMode.VIDEO, min_hand_detection_confidence=0.5, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
        return vision.HandLandmarker.create_from_options(op)
    scale = 540 / min(W, H)
    (w2, h2) = (int(round(W * scale / 2)) * 2, int(round(H * scale / 2)) * 2)
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{f0 / fps:.4f}', '-i', os.path.join(edit, 'cut.mp4'), '-frames:v', str(f1 - f0), '-vf', f'scale={w2}:{h2}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    (lm, raw) = (create_tracker(), {})
    n = f0
    while n < f1:
        buf = proc.stdout.read(w2 * h2 * 3)
        if len(buf) < w2 * h2 * 3:
            break
        if n in cuts and n != f0:
            lm.close()
            lm = create_tracker()
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.frombuffer(buf, np.uint8).reshape(h2, w2, 3))
        res = lm.detect_for_video(img, int(round(n * 1000 / fps)))
        raw[n] = [[[p.x, p.y] for p in hand] for hand in res.hand_landmarks]
        n += 1
    proc.stdout.close()
    proc.wait()
    lm.close()
    end_frame = n
    (music, active, next_id) = ({}, [], 0)
    for n in range(f0, end_frame):
        if n in cuts:
            active = []
        new = []
        available = list(active)
        for pts in raw.get(n, []):
            pm = media(pts, PALM)
            best = min(available, key=lambda tr: (tr['last'][0] - pm[0]) ** 2 + (tr['last'][1] - pm[1]) ** 2, default=None)
            if best and ((best['last'][0] - pm[0]) ** 2 + (best['last'][1] - pm[1]) ** 2) ** 0.5 < 0.15 and (n - best['n'] <= 3):
                available.remove(best)
                tr = best
            else:
                tr = dict(id=next_id, side='imageLeft' if pm[0] < 0.5 else 'imageRight', q={})
                next_id += 1
            (tr['last'], tr['n']) = (pm, n)
            tr['q'][n] = pts
            music[tr['id']] = tr
            new.append(tr)
        active = new + [tr for tr in available if n - tr['n'] <= 3]
        sides = new
        if len(sides) == 2 and sides[0]['side'] == sides[1]['side']:
            (e, d) = sorted(sides, key=lambda tr: tr['last'][0])
            (e['side'], d['side']) = ('imageLeft', 'imageRight')
    frames = {k: [] for k in range(f0, end_frame)}
    for tr in music.values():
        ks = sorted(tr['q'])
        for k in ks:
            neighbors = [tr['q'][j] for j in (k - 1, k, k + 1) if j in tr['q'] and (not (j != k and max(j, k) in cuts))]
            pts = [[sum((v[i][0] for v in neighbors)) / len(neighbors), sum((v[i][1] for v in neighbors)) / len(neighbors)] for i in range(21)]
            frames[k].append(dict(side=tr['side'], palm=[round(x, 4) for x in media(pts, PALM)], nodes=[round(x, 4) for x in media(pts, KNUCKLES)], pts=[[round(x, 4), round(y, 4)] for (x, y) in pts]))
    exit = dict(fps=fps, width=W, height=H, cuts=[round(c / fps, 4) for c in cuts], frames=[dict(t=round(k / fps, 4), hands=frames[k]) for k in sorted(frames)])
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, 'w', encoding='utf-8') as f:
        json.dump(exit, f, separators=(',', ':'))
    count = sum((1 for k in frames if frames[k]))
    print(f'ok: {a.output} · {len(frames)} frames from {f0 / fps:.2f} to {end_frame / fps:.2f} s · hands in {count} · {len(music)} track(s)')
    return 0
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
