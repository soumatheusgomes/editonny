"""The face inside each take, 10 times a second, smoothed: the path the framing follows (`followFace` blocks).

face_layout.json keeps one face per take, so the framing stands still inside a take. When the person leans,
walks or drifts inside a take and the block is zoomed (frame, a punch-in, fullscreen at 1.2), the face slides
to the border or the forehead goes. This helper measures the face with the same YuNet detector as
face_layout.py, 10 times a second on the cut, and writes the smoothed path that project-template/build.py follows:

  1. raw measure every 0.1 s (the largest confident face, as face_layout.py);
  2. median of 5 measures in a row (a single wrong detection does not move anything);
  3. lost face (hand in front, turned aside): the last good point is held. After a loss of 0.3 s or more, a new
     detection is only trusted when 2 of the next 3 samples also find the face;
  4. dead zone: the target moves only when the center walks more than 3% of the video width (or the face size
     changes more than 12%); micro movements and detector jitter never move the frame;
  5. every change of target becomes a sine.inOut move of 0.5 s centred on the change (the path is continuous,
     no jump);
  6. everything restarts at each take of cut_timeline.json (one take's path never leaks into the next).

Output <edit>/face_track.json (fractions of the cut.mp4 frame; times in seconds of the cut):
    {"version": 1, "video": "cut.mp4", "video_sig": "size:mtime_ns", "width", "height", "fps", "step_s": 0.1,
     "parameters": {...},
     "shots": [{"id", "start", "end", "samples", "measured", "w_med", "h_med",
                  "points": [[t, cx, cy, w, h, measured], ...]}]}      # measured: 1 = face found near this sample
A take with no face at all has "points": [] (the build keeps the take's framing and says so).

The build runs this by itself when a block asks for `"followFace": true` and the file is missing or older than the
cut. By hand:
    uv run --directory '<SKILL>' python helpers/track_faces.py '<EDIT>' [--force]
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import file_signature, load_json, now_iso, probe_video, require_binary, save_json  # noqa: E402

VERSION_VALUE = 1
STEP_S = 0.1              # 10 measures a second
WINDOW = 5                 # median of 5 measures in a row
DEAD_ZONE = 0.03          # the target moves when the center walks more than 3% of the width
SIZE_DEAD_ZONE = 0.12        # ... or the face width changes more than 12%
TRANSITION_S = 0.5          # each move of the target: sine.inOut of 0.5 s, centred on the change
LONG_LOSS = 3            # samples without a face that count as "lost" (0.3 s)
CONFIRM = 2               # after a long loss: 2 of the next 3 samples must find the face
SIDE_DETECTION = 540        # frames are scaled to this short side before detection (same as face_layout.py)


class FaceTrackingError(Exception):
    pass


# ---------------------------------------------------------------- smoothing (pure: tested without a video)

def _median(vals: list[float]) -> float:
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _smooth_value(u: float) -> float:
    """sine.inOut from 0 to 1"""
    if u <= 0:
        return 0.0
    if u >= 1:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * u)


KEYS = ("cx", "cy", "w", "h")


def smooth_value(samples_value: list, width_value: int, height_value: int, step_value: float = STEP_S) -> list | None:
    """The path of one take. `samples`: [(t, {"cx", "cy", "w", "h"} or None)] in time order, fractions of the
    frame. Returns [[t, cx, cy, w, h, measured]] (same times) or None when the take has no face at all."""
    n = len(samples_value)
    if not n:
        return None
    raw_value = [a[1] for a in samples_value]
    # 3a. after a long loss, a lone detection is not trusted yet
    reliable = list(raw_value)
    lost = LONG_LOSS          # the start of a take counts as a loss (nothing to hold yet)
    for k in range(n):
        if raw_value[k] is None:
            lost += 1
            continue
        if lost >= LONG_LOSS:
            findings = sum(1 for j in range(k, min(n, k + 3)) if raw_value[j] is not None)
            if findings < min(CONFIRM, n - k):
                reliable[k] = None
                continue
        lost = 0
    # 2. median of the trusted measures around each sample
    med = []
    half_value = WINDOW // 2
    for k in range(n):
        if reliable[k] is None:
            med.append(None)
            continue
        neighbors = [reliable[j] for j in range(max(0, k - half_value), min(n, k + half_value + 1)) if reliable[j] is not None]
        med.append({c: _median([v[c] for v in neighbors]) for c in KEYS})
    good_value = [m for m in med if m is not None]
    if not good_value:
        return None
    # 3b. hold the last good point (and the first good one before the first detection)
    safe_value, last_index = [], good_value[0]
    for m in med:
        if m is not None:
            last_index = m
        safe_value.append(last_index)
    # 4. dead zone: the target stays until the face really moved
    scale_y = height_value / float(width_value)          # a vertical fraction in widths
    target_value, anc = [], safe_value[0]
    for v in safe_value:
        if (abs(v["cx"] - anc["cx"]) > DEAD_ZONE or abs(v["cy"] - anc["cy"]) * scale_y > DEAD_ZONE
                or abs(v["w"] / max(anc["w"], 1e-6) - 1) > SIZE_DEAD_ZONE):
            anc = v
        target_value.append(anc)
    # 5. each change of target is an eased move of TRANSITION_S centred on it
    ts = [a[0] for a in samples_value]
    changes = [(ts[k], {c: target_value[k][c] - target_value[k - 1][c] for c in KEYS}) for k in range(1, n) if target_value[k] is not target_value[k - 1]]
    output_value = []
    for k in range(n):
        v = dict(target_value[0])
        for tm, d in changes:
            e = _smooth_value((ts[k] - (tm - TRANSITION_S / 2)) / TRANSITION_S)
            if e <= 0:
                break
            for c in KEYS:
                v[c] += d[c] * e
        measured_value = any(raw_value[j] is not None for j in range(max(0, k - 1), min(n, k + 2)))
        output_value.append([round(ts[k], 3), round(v["cx"], 4), round(v["cy"], 4), round(v["w"], 4), round(v["h"], 4),
                      1 if measured_value else 0])
    return output_value


def shots_of_cut(timeline: dict | None, duration_value: float) -> list[dict]:
    segs = []
    for i, s in enumerate((timeline or {}).get("segments", []) or []):
        try:
            a, b = float(s["out_start"]), float(s.get("out_end", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        segs.append({"id": str(s.get("id") or f't{i + 1}'), "start": a, "end": b})
    segs.sort(key=lambda s: s["start"])
    for k, s in enumerate(segs):            # a timeline without out_end: the next take (or the end) closes it
        if s["end"] <= s["start"]:
            s["end"] = segs[k + 1]["start"] if k + 1 < len(segs) else duration_value
    if not segs:
        segs = [{"id": "t1", "start": 0.0, "end": duration_value}]
    return segs


def assemble_shots(shots: list[dict], measurements: dict[float, dict | None], width_value: int, height_value: int) -> list[dict]:
    """measurements: {t: face or None} for every sample of the cut. One path per take."""
    ts = sorted(measurements)
    output_value = []
    for tm in shots:
        # 2 ms of slack towards the new take, as the build does (a cut time rounded to 3 digits)
        am = [(t, measurements[t]) for t in ts if tm["start"] - 2e-3 <= t < tm["end"] - 2e-3]
        points_value = smooth_value(am, width_value, height_value) or []
        findings = [m for _t, m in am if m is not None]
        output_value.append({"id": tm["id"], "start": round(tm["start"], 4), "end": round(tm["end"], 4), 'samples': len(am),
                      'measured': len(findings),
                      "w_med": round(_median([m["w"] for m in findings]), 4) if findings else None,
                      "h_med": round(_median([m["h"] for m in findings]), 4) if findings else None,
                      "points": points_value})
    return output_value


# ---------------------------------------------------------------- measuring

def measure_value(video: Path, fps: float, width_value: int, height_value: int, progress: bool = True) -> dict[float, dict | None]:
    """One decoding pass (every `step`-th frame, scaled to 540 on the short side), one detection per sample."""
    import numpy as np  # noqa: PLC0415

    import face_layout  # noqa: PLC0415

    step_value = max(1, int(round(fps * STEP_S)))
    scale_x = min(1.0, SIDE_DETECTION / max(1, min(width_value, height_value)))
    dw, dh = max(2, int(round(width_value * scale_x / 2)) * 2), max(2, int(round(height_value * scale_x / 2)) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(video),
           "-vf", f"select='not(mod(n\\,{step_value}))',scale={dw}:{dh}:flags=area", "-fps_mode", "passthrough",
           "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size_value = dw * dh * 3
    out: dict[float, dict | None] = {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    k, start_value = 0, time.time()
    try:
        while True:
            buf = proc.stdout.read(size_value) if proc.stdout else b""
            if len(buf) < size_value:
                break
            img = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
            f = face_layout.detect_array(img)
            t = round(k * step_value / fps, 4)
            out[t] = None if not f else {"cx": (f["x0"] + f["x1"]) / 2, "cy": (f["y0"] + f["y1"]) / 2,
                                         "w": f["x1"] - f["x0"], "h": f["y1"] - f["y0"]}
            k += 1
            if progress and k % 100 == 0:
                print(f'  {t:6.1f} s measured ({time.time() - start_value:.0f} s)', flush=True)
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
    if not out:
        raise FaceTrackingError(f"could not read frames from {video.name}: {' '.join(err.strip().splitlines()[-3:])}")
    return out


def build_value(edit: Path, force: bool = False) -> dict:
    edit = edit.resolve()
    video = edit / "cut.mp4"
    if not video.exists():
        raise FaceTrackingError(f'could not find {video}: finish the cut first (render_cut.py)')
    output_value = edit / "face_track.json"
    sig = file_signature(video)
    old_value = load_json(output_value) if output_value.exists() and not force else None
    if isinstance(old_value, dict) and old_value.get("version") == VERSION_VALUE and old_value.get("video_sig") == sig:
        old_value['_rerun'] = False
        return old_value
    require_binary("ffmpeg")
    info = probe_video(video)
    width_value = int(info.get("display_width") or info.get("width") or 0)
    height_value = int(info.get("display_height") or info.get("height") or 0)
    fps = float(info.get("fps") or 30.0)
    duration_value = float(info.get("duration") or 0.0)
    if width_value <= 0 or height_value <= 0:
        raise FaceTrackingError(f'could not read dimensions of {video.name}')
    tl_p = edit / "cut_timeline.json"
    timeline = load_json(tl_p) if tl_p.exists() else None
    shots = shots_of_cut(timeline, duration_value)
    start_value = time.time()
    measurements = measure_value(video, fps, width_value, height_value)
    face_track = {
        "version": VERSION_VALUE, "video": "cut.mp4", "video_sig": sig, "width": width_value, "height": height_value,
        "fps": round(fps, 3), "duration": round(duration_value, 3), "step_s": STEP_S,
        "parameters": {"median": WINDOW, "dead_zone": DEAD_ZONE, 'zone_size': SIZE_DEAD_ZONE,
                       "transition_s": TRANSITION_S, "long_loss_s": round(LONG_LOSS * STEP_S, 2),
                       "confirmation": f'{CONFIRM} of 3'},
        "shots": assemble_shots(shots, measurements, width_value, height_value),
        "seconds": round(time.time() - start_value, 1),
        "created_at": now_iso(),
    }
    save_json(output_value, face_track)
    face_track['_rerun'] = True
    return face_track


def summary_value(face_track: dict) -> list[str]:
    lines_value = []
    W = face_track["width"]
    for tm in face_track["shots"]:
        pts = tm["points"]
        if not pts:
            lines_value.append(f"  {tm['id']:>5} {tm['start']:6.2f}-{tm['end']:6.2f} seconds without a face ({tm['measured']}/{tm['samples']}): retain the take's framing")
            continue
        xs, ys = [p[1] for p in pts], [p[2] for p in pts]
        walks = max(max(xs) - min(xs), (max(ys) - min(ys)) * face_track["height"] / W)
        lines_value.append(f"  {tm['id']:>5} {tm['start']:6.2f}-{tm['end']:6.2f}s  face {tm['measured']}/{tm['samples']}  moves up to {walks * W:.0f} px ({walks:.0%} of width)")
    return lines_value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Measure the face ten times per second in each cut shot and smooth the path used by face-following blocks.')
    ap.add_argument('edit', type=Path, help='cut edit folder containing cut.mp4 and cut_timeline.json')
    ap.add_argument("--force", action="store_true", help='recompute even when face_track.json is current', dest='force')
    a = ap.parse_args(argv)
    edit = a.edit.expanduser()
    if not edit.is_dir():
        print(f'error: edit folder not found: {edit}', file=sys.stderr)
        return 2
    try:
        print('measuring faces ten times per second; this may take a few seconds per minute of video...', flush=True)
        face_track = build_value(edit, a.force)
    except FaceTrackingError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001  (OpenCV or the model missing: say it plainly)
        print(f'error: {e}', file=sys.stderr)
        return 2
    for line_value in summary_value(face_track):
        print(line_value)
    status = 'measured now' if face_track.get('_rerun') else 'already current'
    print(f"ok: {edit.resolve() / 'face_track.json'} ({status})")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
