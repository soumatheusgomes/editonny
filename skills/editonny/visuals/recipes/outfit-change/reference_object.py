#!/usr/bin/env python3
"""Track a person with MediaPipe Pose and draw a bright green placeholder in the selected hand. Sizes follow shoulder width. Smooth each shot independently when request metadata is supplied. Shapes: bar, horizontal, hex or ball. Requires mediapipe==0.10.14, numpy<2 and OpenCV."""
import argparse
import json
import subprocess
import sys
import numpy as np

def main():
    ap = argparse.ArgumentParser(description='Draw a green guide object in the selected hand (MediaPipe Pose).')
    ap.add_argument('input_file')
    ap.add_argument('output')
    ap.add_argument('--hand', choices=['right', 'left'], default='right', help="The person's hand (their right appears on screen left when facing the camera)")
    ap.add_argument('--shape', choices=['bar', 'horizontal', 'hex', 'ball'], default='bar')
    ap.add_argument('--scale', type=float, default=1.0, help='Guide scale (1 = handheld dumbbell)')
    ap.add_argument('--request', help='clip_for_ai.py JSON for a clip assembled from several pieces')
    a = ap.parse_args()
    try:
        import cv2
        import mediapipe as mp
    except ImportError:
        sys.exit('error: MediaPipe missing. Run: uv run --no-project --python 3.11 --with "mediapipe==0.10.14" --with "numpy<2" --with opencv-python-headless python reference_object.py ...')
    L = dict(right=(14, 16, (18, 20, 22)), left=(13, 15, (17, 19, 21)))[a.hand]
    cap = cv2.VideoCapture(a.input_file)
    (W, H, fps) = (int(cap.get(3)), int(cap.get(4)), cap.get(5) or 30.0)
    pose = mp.solutions.pose.Pose(static_image_mode=False, model_complexity=2, min_detection_confidence=0.4, min_tracking_confidence=0.4)
    (frs, pts) = ([], [])
    while True:
        (ok, fr) = cap.read()
        if not ok:
            break
        r = pose.process(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        frs.append(fr)
        if r.pose_landmarks:
            lm = r.pose_landmarks.landmark
            p = lambda k: np.array([lm[k].x * W, lm[k].y * H])
            hand = (p(L[1]) + sum((p(k) for k in L[2]))) / 4.0
            pts.append(np.concatenate([hand, p(L[0]), p(L[1]), [np.linalg.norm(p(11) - p(12))]]))
        else:
            pts.append(None)
    if not frs:
        sys.exit('error: no video frames could be read')
    idx = [i for (i, p) in enumerate(pts) if p is not None]
    if not idx:
        sys.exit('error: no person detected in any frame')
    missing_count = len(pts) - len(idx)
    if missing_count:
        print(f'warning: {missing_count} frame(s) without pose; used the nearest detected frame')
    arr = np.array([pts[min(idx, key=lambda j: abs(j - i))] for i in range(len(pts))])
    cuts = [0]
    if a.request:
        request_data = json.load(open(a.request, encoding='utf-8'))
        ac = 0.0
        for p in request_data['pieces'][:-1]:
            ac += p['frames'] / request_data['sourceFps']
            cuts.append(int(round(ac * fps)))
    cuts.append(len(arr))
    k = np.array([1, 2, 3, 2, 1], float)
    k /= k.sum()
    sm = arr.copy()
    for (i0, i1) in zip(cuts, cuts[1:]):
        if i1 - i0 >= 5:
            for c in range(arr.shape[1]):
                sm[i0:i1, c] = np.convolve(np.pad(arr[i0:i1, c], 2, mode='edge'), k, 'valid')
    shoulders = float(np.median(arr[:, 6])) * a.scale
    GREEN = (40, 255, 60)
    ff = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', str(fps), '-i', '-', '-i', a.input_file, '-map', '0:v', '-map', '1:a?', '-c:a', 'copy', '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', a.output], stdin=subprocess.PIPE)
    for (fr, v) in zip(frs, sm):
        (hand, E, Wr) = (v[0:2], v[2:4], v[4:6])
        d = Wr - E
        d /= np.linalg.norm(d) + 1e-06
        n = np.array([-d[1], d[0]])
        o = fr.copy()
        P = lambda q: tuple((int(round(x)) for x in q))
        if a.shape == 'bar':
            c = hand + d * 0.02 * shoulders
            half_span = 0.36 * shoulders
            (q0, q1) = (c - n * half_span, c + n * half_span)
            cv2.line(o, P(q0), P(q1), GREEN, max(2, int(0.105 * shoulders)), cv2.LINE_AA)
            for q in (q0, q1):
                cv2.circle(o, P(q), int(0.135 * shoulders), GREEN, -1, cv2.LINE_AA)
        elif a.shape == 'horizontal':
            if n[0] < 0:
                n = -n
            ang = np.clip(np.degrees(np.arctan2(n[1], n[0])), -14, 14)
            u = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))])
            c = hand + d * 0.02 * shoulders
            half_span = 0.27 * shoulders
            (q0, q1) = (c - u * half_span, c + u * half_span)
            cv2.line(o, P(q0), P(q1), GREEN, max(2, int(0.09 * shoulders)), cv2.LINE_AA)
            for q in (q0, q1):
                cv2.circle(o, P(q), int(0.125 * shoulders), GREEN, -1, cv2.LINE_AA)
        elif a.shape == 'hex':
            R = 0.26 * shoulders
            ang0 = np.arctan2(d[1], d[0])
            c = hand + d * 0.02 * shoulders
            poly = np.array([[c[0] + R * np.cos(ang0 + j * np.pi / 3), c[1] + R * np.sin(ang0 + j * np.pi / 3)] for j in range(6)], np.int32)
            cv2.fillPoly(o, [poly], GREEN, cv2.LINE_AA)
        else:
            cv2.circle(o, P(hand + d * 0.05 * shoulders), int(0.22 * shoulders), GREEN, -1, cv2.LINE_AA)
        ff.stdin.write(o.tobytes())
    ff.stdin.close()
    ff.wait()
    print(f'ok {a.output}: {len(frs)} frames at {fps:.2f} fps · shoulders {shoulders / a.scale:.0f} px · hand {a.hand} · shape {a.shape}' + (f' · cuts at frames {cuts[1:-1]}' if len(cuts) > 2 else ''))
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
