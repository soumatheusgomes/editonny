"""Where the speaker's face is in the cut, and where text fits without covering it.

Runs OpenCV's YuNet face detector (assets/models/face_detection_yunet_2023mar.onnx, MIT) on
several frames of every cut_timeline.json segment (one decoding pass) and writes
edit/face_layout.json. A segment keeps its face when at least one sampled frame finds it; the
protected zone is the union of the boxes found in that segment, so a face that moves inside a
take stays covered.

face_layout.json (coordinates: fractions of the video size; centers: pixels on the cut canvas):
    {"version": 1, "video": "cut.mp4", "video_sig": "...", "segments_key": "...", "width": 1080,
     "height": 1920, "fps": 30, "duration": 25.77, "model": "face_detection_yunet_2023mar.onnx",
     "assumed_zoom": 1.15,
     "segments": [{"id": "r1", "start": 0.0, "end": 2.8, "samples": 7, "detected": 7,
                   "face": {"x0", "y0", "x1", "y1", "eyes_y", "mouth_y", "score"} | null,
                   "zone": {"x0", "y0", "x1", "y1"} | null,     # never put text here
                   "core": {"x0", "y0", "x1", "y1"} | null,     # eyes to lower lip
                   "focus_y": 0.61,
                   "recommended": {"hook_center_y", "captions_center_y", "cta_center_y",
                                   "disclaimer_center_y", "split_top_speaker_y",
                                   "split_bottom_speaker_y"}}],
     "recommended": {"camera": {"focus_y"}, "hook": {"center_y"}, "captions": {"center_y"},
                     "cta": {"center_y"}, "disclaimer": {"center_y"}},
     "summary": "..."}

The recommended centers use the default styles (headline hook, impact captions, CTA with a sub
line, default disclaimer) and a zoom up to `assumed_zoom`. compose_final.py and style_samples.py
recompute them with the real sizes whenever a center_y (or camera.focus_y) is omitted or "auto".
The split speaker offsets are the source row (pixels of the camera frame) shown at the top of the
speaker band for the default split 0.46, with the face slid away from the seam so a caption there
misses the eyes and the mouth (compose_final.py does the same per segment with the real style).

Usage:
    uv run python helpers/face_layout.py --edit-dir DIR [--video cut.mp4] [--force] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ASSETS_DIR, file_signature, load_json, now_iso, probe_video, require_binary, save_json  # noqa: E402

LAYOUT_VERSION = 1
RULES = 3                  # bump when the recommendations change, so cached files are redone
MODEL_PATH = ASSETS_DIR / "models" / "face_detection_yunet_2023mar.onnx"
SCORE_MIN = 0.7            # YuNet confidence for the speaker's face
MIN_FACE_H = 0.06          # smaller boxes are background faces or false hits (fraction of the frame height)
DETECT_SHORT_SIDE = 540    # frames are scaled to this short side before detection
PAD_TOP = 0.08             # zone margin above the box (hair line), fraction of the box height
PAD_BOTTOM = 0.07          # zone margin under the box (chin, beard)
ASSUMED_ZOOM = 1.15        # recommendations stay valid with zoom_levels up to this
MAX_SAMPLES = 40
SAMPLE_EVERY = 0.25        # seconds between sampled frames inside a segment (heads move: sample often)
DECODE_EVERY = 0.125       # seconds between decoded frames (the samples are picked from these)
GAP_FRACTION = 0.012           # breathing room between text and a face or another text (fraction of the height)
DISCLAIMER_MIN_CENTER_FRACTION = 300.0 / 1920.0
DEFAULT_FOCUS_Y = 0.40
FACE_MOVES = 0.15          # eye lines further apart than this (fraction of the height): one number does not fit every take


class FaceLayoutError(Exception):
    pass


# ---------------------------------------------------------------- detection

_DETECTORS: dict[tuple[int, int], object] = {}


def _cv2():
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # no engine notes on the user's terminal
    try:
        import cv2  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - depends on the install
        raise FaceLayoutError(f'OpenCV is not installed ({e}). Run uv sync in the engine folder.')
    if not hasattr(cv2, "FaceDetectorYN"):
        raise FaceLayoutError('This OpenCV build lacks YuNet. Run uv sync in the engine folder.')
    if not MODEL_PATH.exists():
        raise FaceLayoutError(f'face model not found: {MODEL_PATH}')
    return cv2


def _detector(w: int, h: int):
    cv2 = _cv2()
    det = _DETECTORS.get((w, h))
    if det is None:
        det = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (w, h), SCORE_MIN, 0.3, 50)
        _DETECTORS[(w, h)] = det
    return det


def detect_array(bgr) -> dict | None:
    """The speaker's face in one BGR frame (numpy HxWx3): the largest confident box.

    -> {"x0", "y0", "x1", "y1", "eyes_y", "mouth_y", "score"} as fractions of the frame, or None.
    """
    import numpy as np  # noqa: PLC0415

    h, w = bgr.shape[:2]
    scale = DETECT_SHORT_SIDE / min(w, h) if min(w, h) > DETECT_SHORT_SIDE else 1.0
    if scale != 1.0:
        cv2 = _cv2()
        dw, dh = max(2, int(round(w * scale))), max(2, int(round(h * scale)))
        img = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
    else:
        dw, dh, img = w, h, np.ascontiguousarray(bgr)
    det = _detector(dw, dh)
    _, faces = det.detect(img)
    if faces is None:
        return None
    best = None
    for f in faces:
        x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])
        if score < SCORE_MIN or fh < MIN_FACE_H * dh:
            continue
        if best is None or fw * fh > best[2] * best[3]:
            best = (x, y, fw, fh, score, (f[5] + f[7]) / 2.0, (f[11] + f[13]) / 2.0)
    if best is None:
        return None
    x, y, fw, fh, score, eyes, mouth = best
    return {"x0": max(0.0, x / dw), "y0": max(0.0, y / dh), "x1": min(1.0, (x + fw) / dw),
            "y1": min(1.0, (y + fh) / dh), "eyes_y": float(eyes) / dh, "mouth_y": float(mouth) / dh, "score": score}


def detect_image(img) -> dict | None:
    """detect_array for a PIL image."""
    import numpy as np  # noqa: PLC0415

    rgb = np.asarray(img.convert("RGB"))
    return detect_array(np.ascontiguousarray(rgb[:, :, ::-1]))


def _read_frames(video: Path, frames: list[int], width: int, height: int, step: int) -> dict[int, object]:
    """Decode every `step`-th frame in one streamed pass (scaled for detection) and keep the listed ones.

    A short `not(mod(n,step))` select keeps the ffmpeg expression tiny (a long list of frame
    numbers overflows its parser) and streaming keeps memory flat on long videos."""
    import numpy as np  # noqa: PLC0415

    wanted = set(f for f in frames if f >= 0)
    if not wanted:
        return {}
    scale = min(1.0, DETECT_SHORT_SIDE / max(1, min(width, height)))
    dw, dh = max(2, int(round(width * scale / 2)) * 2), max(2, int(round(height * scale / 2)) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(video),
           "-vf", f"select='not(mod(n\\,{step}))',scale={dw}:{dh}:flags=area", "-fps_mode", "passthrough",
           "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = dw * dh * 3
    out = {}
    last = max(wanted)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        k = 0
        while True:
            buf = proc.stdout.read(size) if proc.stdout else b""
            if len(buf) < size:
                break
            n = k * step
            if n in wanted:
                out[n] = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
            k += 1
            if n >= last:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
    if not out and err.strip():
        raise FaceLayoutError(f"could not read frames from {video.name}: {' '.join(err.strip().splitlines()[-3:])}")
    return out


def _summarize(dets: list[dict]) -> tuple[dict | None, dict | None, dict | None]:
    """-> (typical face, protected zone, core zone) for the faces found in one segment."""
    if not dets:
        return None, None, None
    tallest = max(d["y1"] - d["y0"] for d in dets)
    dets = [d for d in dets if d["y1"] - d["y0"] >= 0.4 * tallest]

    def med(key):
        vals = sorted(d[key] for d in dets)
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    face = {k: round(med(k), 4) for k in ("x0", "y0", "x1", "y1", "eyes_y", "mouth_y", "score")}
    zone = {"x0": round(min(d["x0"] for d in dets), 4),
            "y0": round(max(0.0, min(d["y0"] - PAD_TOP * (d["y1"] - d["y0"]) for d in dets)), 4),
            "x1": round(max(d["x1"] for d in dets), 4),
            "y1": round(min(1.0, max(d["y1"] + PAD_BOTTOM * (d["y1"] - d["y0"]) for d in dets)), 4)}
    core = {"x0": zone["x0"], "x1": zone["x1"],
            "y0": round(max(0.0, min(d["eyes_y"] - 0.4 * max(0.02, d["mouth_y"] - d["eyes_y"]) for d in dets)), 4),
            "y1": round(min(1.0, max(d["mouth_y"] + 0.3 * max(0.02, d["mouth_y"] - d["eyes_y"]) for d in dets)), 4)}
    return face, zone, core


def _segments_from_timeline(timeline: dict | None, duration: float, fps: float) -> list[dict]:
    segs = []
    for s in (timeline or {}).get("segments", []) or []:
        try:
            a, b = float(s["out_start"]), float(s["out_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            segs.append({"id": str(s.get("id") or f't{len(segs) + 1}'), "start": a, "end": b})
    segs.sort(key=lambda s: s["start"])
    if not segs and duration > 0:
        # no timeline: pieces of about 3 s, so a face that moves along the video is followed
        n = max(1, int(round(duration / 3.0)))
        segs = [{"id": f'p{i + 1}', "start": round(duration * i / n, 3), "end": round(duration * (i + 1) / n, 3)}
                for i in range(n)]
    return segs


def _segments_key(segs: list[dict]) -> str:
    """Changes with the segments and with the detection settings, so an old face_layout.json is redone."""
    params = (SCORE_MIN, MIN_FACE_H, DETECT_SHORT_SIDE, PAD_TOP, PAD_BOTTOM, ASSUMED_ZOOM, MAX_SAMPLES, SAMPLE_EVERY,
              DECODE_EVERY, GAP_FRACTION, RULES)
    rows = [(round(s["start"], 3), round(s["end"], 3)) for s in segs]
    return hashlib.sha1(repr((params, rows)).encode()).hexdigest()[:16]


def detect_segments(video: Path, segs: list[dict], fps: float, width: int, height: int) -> list[dict]:
    step = max(1, int(round(fps * DECODE_EVERY)))
    plan = []
    for s in segs:
        f0 = int(round(s["start"] * fps))
        f1 = max(f0 + 1, int(round(s["end"] * fps)))
        grid = [f for f in range(((f0 + step - 1) // step) * step, f1, step)]
        if not grid:   # segment shorter than the decoding step: the nearest decoded frame
            grid = [int(round((f0 + f1) / 2.0 / step)) * step]
        n = max(3, min(MAX_SAMPLES, int(math.ceil((s["end"] - s["start"]) / SAMPLE_EVERY))))
        if len(grid) > n:
            grid = [grid[int(len(grid) * (k + 0.5) / n)] for k in range(n)]
        plan.append(sorted(set(grid)))
    decoded = _read_frames(video, [f for fr in plan for f in fr], width, height, step)
    out = []
    for s, frames in zip(segs, plan):
        dets = [d for d in (detect_array(decoded[f]) for f in frames if f in decoded) if d]
        face, zone, core = _summarize(dets)
        out.append(dict(s, samples=len([f for f in frames if f in decoded]), detected=len(dets),
                        face=face, zone=zone, core=core,
                        focus_y=round(face["eyes_y"], 4) if face else None))
    return out


# ---------------------------------------------------------------- placement primitives

def free_center(pref: float, above: float, below: float, blocked: list[tuple[float, float]],
                lo: float, hi: float, gap: float = 0.0) -> float | None:
    """Center closest to `pref` for a layer reaching `above` px up and `below` px down, with its
    edges inside [lo, hi] and at least `gap` px away from every blocked (y0, y1) band. None when
    nothing fits."""
    cmin, cmax = lo + above, hi - below
    if cmax < cmin:
        return None
    bad = sorted((y0 - below - gap, y1 + above + gap) for y0, y1 in blocked if y1 > y0)
    merged: list[list[float]] = []
    for a, b in bad:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    free, cur = [], cmin
    for a, b in merged:
        if b <= cur:
            continue
        if a > cmax:
            break
        if a > cur:
            free.append((cur, min(a, cmax)))
        cur = max(cur, b)
    if cur <= cmax:
        free.append((cur, cmax))
    if not free:
        return None
    best = min(free, key=lambda iv: 0.0 if iv[0] <= pref <= iv[1] else min(abs(pref - iv[0]), abs(pref - iv[1])))
    return min(max(pref, best[0]), best[1])


def place(prefs: list[float], above: float, below: float, blocked: list[tuple[float, float]],
          lo: float, hi: float, gap: float) -> float | None:
    """First preferred center that is free as is; otherwise the free center closest to prefs[0]."""
    for p in prefs:
        if free_center(p, above, below, blocked, lo, hi, gap) == p:
            return p
    return free_center(prefs[0], above, below, blocked, lo, hi, gap)


def zoom_rect(rect: dict, zoom: float, anchor_y: float) -> dict:
    """A normalized rect after the camera zoom (crop centered on x, anchored at anchor_y)."""
    return {"x0": max(0.0, 0.5 + (rect["x0"] - 0.5) * zoom), "x1": min(1.0, 0.5 + (rect["x1"] - 0.5) * zoom),
            "y0": max(0.0, anchor_y + (rect["y0"] - anchor_y) * zoom), "y1": min(1.0, anchor_y + (rect["y1"] - anchor_y) * zoom)}


def speaker_src_y(typ: str, split: float, eyes_px: float, W: int, H: int, safe_top: float, safe_bottom: float,
                  core: tuple[float, float] | None = None, clear: float = 0.0) -> int:
    """Row of the (camera) frame shown at the top of the speaker band of a split, putting the eyes
    at 45% of the visible band (split_top) or in its middle (split_bottom).

    With `core` (top and bottom of eyes-to-mouth in px of the camera frame) and `clear` (how far the
    caption on the seam reaches into the speaker band), the face slides away from the seam until
    that caption misses it, as long as the eyes stay well inside the visible band."""
    hb = max(2, int(round(split * H / 2.0)) * 2)
    hs = H - hb
    if typ == "split_top":
        band_y, vis0, vis1 = hb, hb, min(H, safe_bottom)
        target = vis0 + 0.45 * max(0.0, vis1 - vis0)
    else:
        band_y, vis0, vis1 = 0, safe_top, hs
        target = vis0 + 0.5 * max(0.0, vis1 - vis0)
    src = eyes_px - (target - band_y)
    if core is not None and clear > 0:
        span = max(0.0, vis1 - vis0)
        if typ == "split_top":      # the core top must sit `clear` under the seam; eyes not in the last 15% of the band
            eyes_limit = eyes_px + band_y - (vis1 - 0.15 * span)
            src = max(eyes_limit, min(src, core[0] - clear))
        else:                       # the core bottom must sit `clear` above the seam; eyes not in the first 15%
            eyes_limit = eyes_px - band_y - (vis0 + 0.15 * span)
            src = min(eyes_limit, max(src, core[1] - hs + clear))
    return int(round(min(max(src, 0), H - hs)))


# ---------------------------------------------------------------- recommendations

def _extents(canvas: tuple[int, int]) -> dict[str, tuple[float, float]]:
    """(above, below) px of the default text layers, measured on rendered art."""
    import text_render as tr  # noqa: PLC0415

    W, H = canvas
    lay = tr.layout_defaults(canvas)

    def ext(img, cy, grow=1.0):
        bb = img.getchannel("A").getbbox()
        if not bb:
            return (0.0, 0.0)
        return ((cy - bb[1]) * grow, (bb[3] - cy) * grow)

    return {
        "hook": ext(tr.render_hook('Make every word count', "headline", tr.DEFAULT_ACCENT, canvas,
                                   center_y=lay["hook"], max_width=int(900 * W / 1080)), lay["hook"], 1.05),
        "captions": tr.caption_extent([], "impact", canvas),
        "cta": ext(tr.render_cta("Tap Learn More", "Instant access", tr.DEFAULT_ACCENT, canvas,
                                 center_y=lay["cta"]), lay["cta"], 1.03),
        "disclaimer": ext(tr.render_disclaimer('Individual results depend on execution. No earnings are guaranteed.',
                                               canvas, center_y=lay["disclaimer"]), lay["disclaimer"]),
    }


def _bands(segs: list[dict], t0: float, t1: float, H: int, zoom: float, core: bool = False) -> list[tuple[float, float]]:
    out = []
    for s in segs:
        z = s.get("core" if core else "zone")
        if not z or s["end"] <= t0 or s["start"] >= t1:
            continue
        r = zoom_rect(z, zoom, s["focus_y"] if s.get("focus_y") is not None else DEFAULT_FOCUS_Y)
        out.append((r["y0"] * H, r["y1"] * H))
    return out


def recommend(segs: list[dict], canvas: tuple[int, int], duration: float, zoom: float = ASSUMED_ZOOM) -> dict:
    import text_render as tr  # noqa: PLC0415

    W, H = canvas
    lay = tr.layout_defaults(canvas)
    ext = _extents(canvas)
    gap = GAP_FRACTION * H
    lo, hi = lay["top"], lay["bottom"]
    disc_hi = min(H - 8.0 * H / 1920.0, lay["bottom"] + 0.035 * H)
    # The disclaimer center never goes above 300 px on a 1920 canvas (Reels top bar), same rule as
    # references/phase2.md gives for manual numbers.
    disc_lo = max(lo, DISCLAIMER_MIN_CENTER_FRACTION * H - ext["disclaimer"][0])

    def centers(t0: float, t1: float, captions_band: tuple[float, float] | None = None) -> dict:
        faces = _bands(segs, t0, t1, H, zoom)
        colors_value = _bands(segs, t0, t1, H, zoom, core=True)
        r = {}
        c = place([lay["cta"]], *ext["cta"], faces, lo, hi, gap)
        r["cta_center_y"] = c
        r["hook_center_y"] = place([lay["hook"]], *ext["hook"], faces, lo, hi, gap)
        cap = place([lay["caption"]], *ext["captions"], faces, lo, hi, gap)
        r["captions_center_y"] = cap
        cap_band = captions_band or ((cap - ext["captions"][0], cap + ext["captions"][1]) if cap is not None else None)
        top_anchor = disc_lo + ext["disclaimer"][0] + gap
        blocked = faces + ([cap_band] if cap_band else [])
        d = place([lay["disclaimer"], top_anchor], *ext["disclaimer"], blocked, disc_lo, disc_hi, gap)
        if d is None:   # small print may sit on the chin or the forehead, never on the eyes or the mouth
            d = place([lay["disclaimer"], top_anchor], *ext["disclaimer"], colors_value + ([cap_band] if cap_band else []),
                      disc_lo, disc_hi, 3 * gap)
        r["disclaimer_center_y"] = d
        return {k: (round(v) if v is not None else None) for k, v in r.items()}

    safe_top, safe_bottom = lay["top"], lay["bottom"]
    for s in segs:
        rec = centers(s["start"], s["end"])
        if s.get("face"):
            eyes = s["face"]["eyes_y"] * H
            core = (s["core"]["y0"] * H, s["core"]["y1"] * H) if s.get("core") else None
            above, below = ext["captions"]
            rec["split_top_speaker_y"] = speaker_src_y("split_top", 0.46, eyes, W, H, safe_top, safe_bottom,
                                                       core=core, clear=below + gap)
            rec["split_bottom_speaker_y"] = speaker_src_y("split_bottom", 0.46, eyes, W, H, safe_top, safe_bottom,
                                                          core=core, clear=above + gap)
        else:
            rec["split_top_speaker_y"] = rec["split_bottom_speaker_y"] = None
        s["recommended"] = rec

    with_face = [s for s in segs if s.get("face")]
    if with_face:
        weights = sorted((s["face"]["eyes_y"], s["end"] - s["start"]) for s in with_face)
        half, acc, focus = sum(w for _, w in weights) / 2.0, 0.0, weights[-1][0]
        for v, w in weights:
            acc += w
            if acc >= half:
                focus = v
                break
    else:
        focus = DEFAULT_FOCUS_Y
    hook_end = min(duration, 3.0)
    cta_start = max(0.0, duration - 2.5)
    faces_all = _bands(segs, 0.0, duration, H, zoom)
    faces_after_hook = _bands(segs, hook_end, duration, H, zoom) or faces_all
    cap = place([lay["caption"]], *ext["captions"], faces_after_hook, lo, hi, gap)
    cap_band = (cap - ext["captions"][0], cap + ext["captions"][1]) if cap is not None else None
    top_anchor = disc_lo + ext["disclaimer"][0] + gap
    disc = place([lay["disclaimer"], top_anchor], *ext["disclaimer"], faces_all + ([cap_band] if cap_band else []),
                 disc_lo, disc_hi, gap)
    if disc is None:
        disc = place([lay["disclaimer"], top_anchor], *ext["disclaimer"],
                     _bands(segs, 0.0, duration, H, zoom, core=True) + ([cap_band] if cap_band else []), disc_lo, disc_hi, 3 * gap)
    rec = {
        "camera": {"focus_y": round(focus, 3)},
        "hook": {"center_y": place([lay["hook"]], *ext["hook"], _bands(segs, 0.0, hook_end, H, zoom), lo, hi, gap)},
        "captions": {"center_y": cap},
        "cta": {"center_y": (place([lay["cta"]], *ext["cta"],
                                   _bands(segs, cta_start, duration, H, zoom) + ([cap_band] if cap_band else []), lo, hi, gap)
                             or place([lay["cta"]], *ext["cta"], _bands(segs, cta_start, duration, H, zoom), lo, hi, gap))},
        "disclaimer": {"center_y": disc},
    }
    for sec in rec.values():
        for k, v in list(sec.items()):
            if k == "center_y" and v is not None:
                sec[k] = int(round(v))
    return rec


def _summary(layout: dict) -> str:
    segs = layout["segments"]
    found = [s for s in segs if s.get("face")]
    rec = layout["recommended"]
    if not found:
        return (f'no face detected in {len(segs)} intervals: text retains default positions')
    eyes = [s["face"]["eyes_y"] for s in found]
    moves = max(eyes) - min(eyes) > FACE_MOVES
    if moves:
        where = "face height changes between intervals"
    else:
        where = "face low in frame" if rec["camera"]["focus_y"] > 0.5 else "face high in frame" if rec["camera"]["focus_y"] < 0.3 else "face centered"
    key = {"hook": "hook_center_y", "captions": "captions_center_y", "cta": "cta_center_y", "disclaimer": "disclaimer_center_y"}

    def v(sec):
        c = rec[sec]["center_y"]
        if c is not None:
            return str(c)
        per = sorted({s["recommended"][key[sec]] for s in segs if (s.get("recommended") or {}).get(key[sec]) is not None})
        return f'varies by interval ({per[0]} to {per[-1]})' if per else 'no free space'

    tip = (". Leave camera.focus_y and center_y at \"auto\": composition adjusts each interval" if moves else "")
    return (f"face in {len(found)} of {len(segs)} intervals, eyes between {min(eyes):.2f} and {max(eyes):.2f} of frame height ({where}). Suggested camera.focus_y: {('auto' if moves else rec['camera']['focus_y'])}, hook {v('hook')}, captions {v('captions')}, call to action {v('cta')}, warning {v('disclaimer')}{tip}")


# ---------------------------------------------------------------- build / cache

def build(edit_dir: Path, video: Path | None = None, force: bool = False, timeline: dict | None = None) -> dict:
    """Load edit/face_layout.json when it matches the video and the timeline, else detect and write it."""
    edit_dir = Path(edit_dir).resolve()
    video = Path(video).resolve() if video else edit_dir / "cut.mp4"
    if not video.exists():
        raise FaceLayoutError(f'video not found: {video}')
    if timeline is None and video == edit_dir / "cut.mp4":
        timeline = load_json(edit_dir / "cut_timeline.json")   # its segments are positions in cut.mp4
    info = probe_video(video)
    width = int(info.get("display_width") or info.get("width") or 0)
    height = int(info.get("display_height") or info.get("height") or 0)
    fps = float(info.get("fps") or 30.0)
    duration = float(info.get("duration") or 0.0)
    if width <= 0 or height <= 0:
        raise FaceLayoutError(f'could not read dimensions of {video.name}')
    segs = _segments_from_timeline(timeline, duration, fps)
    key = _segments_key(segs)
    out_path = edit_dir / "face_layout.json"
    sig = file_signature(video)
    old = load_json(out_path) if out_path.exists() and not force else None
    if (isinstance(old, dict) and old.get("version") == LAYOUT_VERSION and old.get("video_sig") == sig
            and old.get("segments_key") == key and old.get("video") == _rel(video, edit_dir)):
        try:
            old["summary"] = _summary(old)   # the wording may be newer than the cached file
        except (KeyError, TypeError, ValueError):
            pass
        return old
    require_binary("ffmpeg")
    started = time.time()
    detected = detect_segments(video, segs, fps, width, height)
    layout = {
        "version": LAYOUT_VERSION,
        "video": _rel(video, edit_dir),
        "video_sig": sig,
        "segments_key": key,
        "width": width, "height": height, "fps": round(fps, 3), "duration": round(duration, 3),
        "model": MODEL_PATH.name,
        "assumed_zoom": ASSUMED_ZOOM,
        "segments": detected,
    }
    layout["recommended"] = recommend(detected, (width, height), duration)
    layout["summary"] = _summary(layout)
    layout["seconds"] = round(time.time() - started, 2)
    layout["created_at"] = now_iso()
    save_json(out_path, layout)
    return layout


def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.resolve().relative_to(base))
    except ValueError:
        return str(p.resolve())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Detect faces in each cut interval and suggest safe hook, caption, CTA, and disclaimer positions.')
    ap.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    ap.add_argument("--video", help='video to analyze (default: edit/cut.mp4)', dest='video')
    ap.add_argument("--force", action="store_true", help='recompute even when face_layout.json is current', dest='force')
    ap.add_argument("--json", action="store_true", help="print face_layout.json", dest='json')
    args = ap.parse_args(argv)
    edit = Path(args.edit_dir).expanduser().resolve()
    if not edit.is_dir():
        print(f'error: edit folder not found: {edit}', file=sys.stderr)
        return 2
    try:
        layout = build(edit, Path(args.video).expanduser() if args.video else None, force=args.force)
    except FaceLayoutError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(layout, ensure_ascii=False, indent=2))
        return 0
    for s in layout["segments"]:
        if s.get("face"):
            z = s["zone"]
            print(f"  {s['id']:>5} {s['start']:6.2f}-{s['end']:6.2f}s  face {s['detected']}/{s['samples']}  height {z['y0']:.2f}-{z['y1']:.2f}  eyes {s['face']['eyes_y']:.2f}")
        else:
            print(f"  {s['id']:>5} {s['start']:6.2f}-{s['end']:6.2f} seconds without a face (0/{s['samples']})")
    print(f"ok: {layout['summary']} -> {edit / 'face_layout.json'}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
