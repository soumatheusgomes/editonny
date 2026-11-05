# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
"""EDL -> cut.mp4 + cut_timeline.json.

Usage:
    uv run python helpers/render_cut.py <edit>/edl.json [-o cut.mp4] [--jobs N] [--no-jcut] [--preview]

Pipeline
  1. Plan: the audio of a range starts exactly at the EDL start. The picture starts on the
     nearest source frame when one is within half an output frame, else at the exact time
     (sparse VFR sources such as screen recordings: the frame on screen at that time is used).
     Lengths are whole output frames. J-cut per junction: the incoming picture skips
     `lead_frames` while its audio keeps them (so it starts earlier, over the outgoing
     picture); the outgoing tail loses up to `tail_trim_frames` only where there is no
     measured speech (edit/regions/<stem>.json, or an inline silencedetect when missing).
     A range with "dry": false keeps its written edges: no lead and no tail trim on it (a video playing
     on screen starts on its first frame).
     A source whose shape differs from the output is cropped around `focus_x`/`focus_y` of
     the range, or around the face found by YuNet when the crop is large.
     "picture_source": "CAM2" on a range: the audio still comes from the range's source and the picture
     from CAM2 at the same instant, through synchronization.json (sync_cameras.py; the EDL key
     "synchronization" points to another file). A source used only as audio (a lapela .wav) needs no video.
     Two ranges in a row from the same audio source that touch (start of the second within half a
     frame of the end of the first) are one continuous voice: no J-cut and no 30 ms fades there, and
     the second one's audio starts on the exact sample where the first one's ends (a camera switch in
     the middle of a sentence does not dip or skip). Their gain_db must match, or the fades stay.
  2. Extract, in parallel, one picture clip (H.264, final quality, upright, scaled+cropped,
     approved FPS, source color/precision, and only a confirmed grade/conversion) and one float WAV (48k stereo,
     gain+limiter, 30 ms fades, sample-exact length) per range into edit/clips/ (cached).
  3. Count the encoded frames of each clip -> out_start/out_end (frame-accurate).
  4. Picture: concat demuxer, stream copy. Audio: adelay in samples + amix normalize=0,
     loudnorm two-pass (I=-14, TP=-1, LRA=11), AAC 192k, length pinned with -t. The true peak
     of the encoded file is measured; above -1.05 dBTP (a margin for verify_cut's ebur128 reading)
     the audio is lowered and encoded again.
  5. Write <output stem>_timeline.json (cut_timeline.json by default). A full render inside
     the edit dir also merges state.json (awaiting stays null: verify_cut asks for approval).
     `--preview` writes cut_preview.mp4 and leaves cut.mp4 and state.json alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from functools import lru_cache

from common import (ASSETS_DIR, OUTPUT_FORMATS, file_signature, fmt_time, load_json, nominal_fps, now_iso,
                    output_fps_for, probe_video, require_binary, save_json)
from common import has_ffmpeg_filter as _has_ffmpeg_filter
from grade import check_filter, graded_chain, resolve_grade

SR = 48000
FADE_SAMPLES = 1440            # 30 ms
SPEECH_GUARD_S = 0.10          # the tail trim never gets closer than this to measured speech (a weak
                               # final consonant sits under the speech regions; 0.04 bit it)
MIN_JOIN_GAP_S = 0.10          # silence kept between outgoing and incoming speech after a J-cut
SILENCE_NOISE_DB = -35
MIN_PIC_FRAMES = 6
DEFAULT_JCUT = {"lead_frames": 4, "tail_trim_frames": 2}
LOUDNORM = {"I": -14.0, "TP": -1.0, "LRA": 11.0}
TRUE_PEAK_LIMIT = -1.0         # delivered cut.mp4 (after AAC) must not go above this
TRUE_PEAK_MARGIN = 0.05        # loudnorm (here) and ebur128 (verify_cut) read the true peak a few hundredths apart
CACHE_VERSION = "render_cut/3-preserve"
SYNC_FILE = "synchronization.json"
BT709_TAGS = "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
ASPECT_WARN = 1.15             # source/output shape ratio beyond this: warn and frame on the face
CROP_MIN = 0.02                # crops smaller than this are just "fill", not recorded
REPEAT_TOL_S = 0.02
FACE_MODEL = ASSETS_DIR / "models" / "face_detection_yunet_2023mar.onnx"
FACE_SAMPLES = 5


@lru_cache(maxsize=None)
def has_ffmpeg_filter(name: str) -> bool:
    return _has_ffmpeg_filter(name)


class CutError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f'notice: {msg}', file=sys.stderr, flush=True)


def ffmpeg(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        raise CutError(f"FFmpeg failed to {what}:\n{(proc.stderr or '').strip()[-1500:]}")
    return proc


def edl_signature(edl: dict) -> str:
    return hashlib.sha1(json.dumps(edl, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ EDL loading


def resolve_source(value: str, edit_dir: Path) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    for base in (edit_dir, edit_dir.parent):
        cand = Path(os.path.abspath(base / p))
        if cand.exists():
            return cand
    return Path(os.path.abspath(edit_dir / p))


def load_edl(path: Path) -> dict:
    if not path.exists():
        raise CutError(f'EDL not found: {path}', 2)
    try:
        edl = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CutError(f'EDL is not valid JSON ({path.name}, line {exc.lineno}): {exc.msg}', 2)
    if not isinstance(edl, dict):
        raise CutError('EDL must be a JSON object', 2)
    ranges = edl.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise CutError('EDL has no intervals in "ranges"', 2)
    sources = edl.get("sources") or {}
    if not isinstance(sources, dict) or not sources:
        raise CutError('EDL has no "sources" mapping names to video paths', 2)
    seen: set[str] = set()
    for i, r in enumerate(ranges):
        rid = str(r.get("id") or f'r{i + 1}')
        r["id"] = rid
        if rid in seen:
            raise CutError(f'id repetido no EDL: {rid}', 2)
        seen.add(rid)
        if r.get("source") not in sources:
            raise CutError(f'''interval {rid}: source {r.get('source')!r} is absent from "sources"''', 2)
        try:
            r["start"], r["end"] = float(r["start"]), float(r["end"])
        except (KeyError, TypeError, ValueError):
            raise CutError(f'interval {rid}: start/end must be numeric seconds', 2)
        if r["start"] < 0 or r["end"] <= r["start"]:
            raise CutError(f"interval {rid}: end ({r['end']}) must exceed start ({r['start']})", 2)
        try:
            r["gain_db"] = float(r.get("gain_db") or 0.0)
        except (TypeError, ValueError):
            raise CutError(f'interval {rid}: invalid gain_db', 2)
        for k in ("focus_x", "focus_y"):
            if r.get(k) is None:
                continue
            try:
                r[k] = float(r[k])
            except (TypeError, ValueError):
                raise CutError(f'interval {rid}: {k} must be a number from 0 to 1', 2)
            if not 0.0 <= r[k] <= 1.0:
                raise CutError(f'interval {rid}: {k} must be between 0 and 1 (0 = left/top, 1 = right/bottom)', 2)
        pic = r.get("picture_source")
        if pic in (None, "", False) or pic == r["source"]:
            r["picture_source"] = None
        elif not isinstance(pic, str) or pic not in sources:
            raise CutError(f'interval {rid}: picture_source {pic!r} is absent from "sources"; add the camera and its path', 2)
    return edl


def picture_key(r: dict) -> str:
    """The source the picture of a range comes from."""
    return r.get("picture_source") or r["source"]


def load_sync(edl: dict, edit_dir: Path, probes: dict) -> tuple[dict[str, tuple[float, float]], Path | None]:
    """{source: (offset, scale)} from synchronization.json for every source a range with picture_source touches.

    Rule of the file: time in the source = offset + adjustment_s + scale x time in the main one."""
    needed = sorted({k for r in edl["ranges"] if r.get("picture_source") for k in (r["source"], r["picture_source"])})
    if not needed:
        return {}, None
    raw = edl.get("synchronization") or SYNC_FILE
    path = resolve_source(str(raw), edit_dir)
    if not path.exists():
        raise CutError(f'alternate-camera intervals require synchronization, but {path} does not exist. Run helpers/sync_cameras.py --edl <EDIT>/edl_manual.json or include synchronization in the EDL', 2)
    doc = load_json(path)
    fonts_value = doc.get("sources") if isinstance(doc, dict) else None
    if not isinstance(fonts_value, dict):
        raise CutError(f'{path.name} is not a synchronization file; source mapping is missing', 2)
    out: dict[str, tuple[float, float]] = {}
    for key in needed:
        f = fonts_value.get(key)
        if not isinstance(f, dict):
            raise CutError(f'source {key} is absent from {path.name}: include this source when running sync_cameras.py', 2)
        try:
            off = float(f.get("offset") or 0.0) + float(f.get("adjustment_s") or 0.0)
            scale = float(f.get("scale") or 1.0)
        except (TypeError, ValueError):
            raise CutError(f'{path.name}: offset, adjustment_s, or scale of {key} is not numeric', 2)
        if not 0.9 < scale < 1.1:
            raise CutError(f'{path.name}: scale {scale} of {key} is outside the reasonable range', 2)
        src = probes[key]["path"]
        measured = Path(str(f.get("file") or ""))
        same_path = measured.exists() and os.path.samefile(measured, src)
        same_sig = f.get("signature") == file_signature(src)
        if not same_path and not same_sig:
            raise CutError(f'{path.name} measured {key} in another file ({measured}), rather than {src}: run sync_cameras.py again', 2)
        if same_path and not same_sig and f.get("signature"):
            warn(f'{src.name} changed after synchronization ({path.name}): if re-exported, rerun sync_cameras.py')
        if f.get("confidence") == "weak":
            warn(f'synchronization for {key} was weak in {path.name}: inspect visually (sync_cameras.py --proof)')
        out[key] = (off, scale)
    return out, path


def jcut_settings(edl: dict, in_jcut: bool) -> dict:
    raw = edl.get("jcut", None)
    if in_jcut or raw is False:
        return {"enabled": False, "lead_frames": 0, "tail_trim_frames": 0}
    cfg = dict(DEFAULT_JCUT)
    if isinstance(raw, dict):
        for k in cfg:
            if raw.get(k) is not None:
                cfg[k] = max(0, int(raw[k]))
    return {"enabled": cfg["lead_frames"] > 0 or cfg["tail_trim_frames"] > 0, **cfg}


# ------------------------------------------------------------------ source analysis


def source_frame_times(src: Path, t: float, before: float = 3.0, after: float = 3.0) -> list[float]:
    """Presentation times of the source frames around t (packets, no decoding).

    The window is wide and the list is sorted: with B-frames the packets come in decode order,
    and ffprobe stops reading at the first packet past the window end, so a narrow window
    loses frames that sit just before t.
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals",
         f'{max(0.0, t - before):.3f}%{t + after:.3f}', "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(src)], capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = set()
    for line in proc.stdout.splitlines():
        try:
            out.add(float(line.strip().strip(",")))
        except ValueError:
            continue
    return sorted(out)


def picture_start(src: Path, t: float, fps: int) -> tuple[float, bool]:
    """(source time of the first picture frame, snapped?).

    Snaps to the nearest real frame only when it is within half an output frame. A sparse VFR
    source (static screen recording) has no frame near t: the exact time is kept and the
    extraction shows the frame that is on screen at t.
    """
    times = source_frame_times(src, t)
    if times:
        near = min(times, key=lambda x: abs(x - t))
        if abs(near - t) <= 0.5 / fps + 1e-6:
            return near, True
    return t, False


def nominal_rate(src: Path, info: dict) -> float | None:
    """Nominal fps of a source for choosing the output fps; None for sparse or variable sources
    (screen recordings write frames only when the screen changes) that must not drag it down."""
    avg = float(info.get("fps") or 0.0)
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=r_frame_rate", "-of", "csv=p=0", str(src)], capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        r = float(Fraction(proc.stdout.strip().split(",")[0]))
    except (ValueError, ZeroDivisionError):
        r = 0.0
    if avg <= 0 or (r > 0 and avg < 0.8 * r):
        return None
    n = nominal_fps(avg)
    return n if n >= 23.0 else None


def choose_output_fps(probes: dict) -> int:
    rates = [nominal_rate(pr["path"], pr["info"]) for pr in probes.values()]
    rates = [r for r in rates if r]
    return output_fps_for(max(rates)) if rates else 30


def crop_geometry(info: dict, W: int, H: int) -> tuple[str | None, float, float]:
    """(axis cropped "x"/"y" or None, kept fraction of that axis, source/output shape ratio)."""
    dw, dh = info.get("display_width") or W, info.get("display_height") or H
    ratio = (dw / dh) / (W / H)
    if ratio > 1.0 + CROP_MIN:
        return "x", 1.0 / ratio, ratio
    if ratio < 1.0 - CROP_MIN:
        return "y", ratio, ratio
    return None, 1.0, ratio


def face_focus(src: Path, t0: float, t1: float) -> tuple[float, float, int] | None:
    """Median center (0..1, upright frame) of the biggest face on FACE_SAMPLES frames of [t0, t1].

    Returns (x, y, frames with a face), or None when OpenCV or the model is missing."""
    if not FACE_MODEL.exists():
        return None
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    xs, ys = [], []
    for i in range(FACE_SAMPLES):
        t = t0 + (i + 0.5) * (t1 - t0) / FACE_SAMPLES
        proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f'{t:.3f}', "-i", str(src), "-frames:v", "1",
                               "-an", "-vf", "scale=640:-2", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            continue
        img = cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        h, w = img.shape[:2]
        try:
            det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), 0.6, 0.3, 50)
            _, faces = det.detect(img)
        except cv2.error:
            return None
        if faces is None or not len(faces):
            continue
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
        xs.append(min(1.0, max(0.0, (x + fw / 2) / w)))
        ys.append(min(1.0, max(0.0, (y + fh / 2) / h)))
    if not xs:
        return 0.5, 0.5, 0
    return float(np.median(xs)), float(np.median(ys)), len(xs)


def parse_silences(stderr: str, t0: float, t1: float) -> list[list[float]]:
    sil: list[list[float]] = []
    cur = None
    for line in stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end: (-?[\d.]+)", line)
        if m and cur is not None:
            sil.append([t0 + cur, t0 + float(m.group(1))])
            cur = None
    if cur is not None:
        sil.append([t0 + cur, t1])
    return sil


def _intersect(a: list, b: list) -> list:
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def speech_in_window(edit_dir: Path, stem: str, src: Path, info: dict, t0: float, t1: float) -> tuple[list, str]:
    """Measured speech intervals inside [t0, t1] of a source.

    edit/regions/<stem>.json is the authority when present and made from this very file (same
    source_sig); its coarse min_silence (0.25 s)
    hides the short pauses editors cut at, so the edge window is refined with a fine
    silencedetect (same noise floor, 0.08 s) and the two are intersected. Without a regions
    file only the fine pass is used (noise -35 dB).
    """
    t0 = max(0.0, t0)
    if not info.get("has_audio") or t1 - t0 <= 0.01:
        return [], 'no audio'
    reg = load_json(edit_dir / "regions" / f'{stem}.json')
    reg_spans = None
    noise = SILENCE_NOISE_DB
    if isinstance(reg, dict) and isinstance(reg.get("speech"), list) and "start" not in reg:
        # regions of another file with the same name (video.mov + video.mp4) or of an older copy do not count
        same_file = reg.get("source_sig") in (None, file_signature(src))
        if same_file and abs(float(reg.get("duration") or info["duration"]) - info["duration"]) < 0.2:
            reg_spans = [[max(t0, float(a)), min(t1, float(b))] for a, b in reg["speech"]
                         if float(b) > t0 and float(a) < t1]
            try:
                noise = float(reg.get("noise_db", noise))
            except (TypeError, ValueError):
                pass
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-ss", f'{t0:.3f}', "-t", f'{t1 - t0:.3f}',
         "-i", str(src), "-vn", "-af", f'silencedetect=n={noise:g}dB:d=0.08', "-f", "null", "-"],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    sil = parse_silences(proc.stderr, t0, t1)
    fine, cur = [], t0
    for a, b in sil:
        if a > cur + 1e-3:
            fine.append([cur, a])
        cur = max(cur, b)
    if cur < t1 - 1e-3:
        fine.append([cur, t1])
    if reg_spans is None:
        return fine, "silencedetect"
    return _intersect(sorted(reg_spans), fine), "regions+silencedetect"


def channel_mode(src: Path, info: dict) -> str:
    """stereo | mono | left | right | multi. Phase-cancelled stereo -> use the stronger channel."""
    ch = info.get("audio_channels") or 0
    if ch == 1:
        return "mono"
    if ch != 2:
        return "multi"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-t", "120", "-i", str(src), "-vn", "-af",
         "pan=3c|c0=c0|c1=c1|c2=0.5*c0+0.5*c1,astats=metadata=0:measure_perchannel=RMS_level:measure_overall=none",
         "-f", "null", "-"], capture_output=True, text=True, encoding='utf-8', errors='replace')
    levels = [float(x) if x not in ("-inf", "inf", "nan") else -120.0
              for x in re.findall(r"RMS level dB: (-?[\w.]+)", proc.stderr)]
    if len(levels) < 3:
        return "stereo"
    left, right, both = levels[:3]
    best = max(left, right)
    if best > -90 and both < best - 6:
        warn(f'{src.name}: phase-inverted stereo (L+R {both:.1f} dB contra {best:.1f} dB). Using one channel.')
        return "left" if left >= right else "right"
    return "stereo"


_HWACCEL_CACHE: dict[str, bool] = {}


def hwaccel_for(src: Path, info: dict) -> bool:
    if os.environ.get("EDITONNY_HWACCEL", "1") == "0" or sys.platform != "darwin":
        return False
    codec = info.get("codec", "")
    if codec not in ("hevc", "h264", "prores"):
        return False
    if codec not in _HWACCEL_CACHE:
        hw = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
        ok = "videotoolbox" in hw
        if ok:
            ok = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-hwaccel", "videotoolbox", "-i", str(src),
                                 "-frames:v", "2", "-an", "-f", "null", "-"], capture_output=True).returncode == 0
        _HWACCEL_CACHE[codec] = ok
    return _HWACCEL_CACHE[codec]


# ------------------------------------------------------------------ planning


@dataclass
class Plan:
    idx: int
    rid: str
    source: str
    src: Path
    info: dict
    edl_start: float
    edl_end: float
    gain_db: float
    beat: str = ""
    quote: str = ""
    pic_source: str = ""       # source key of the picture ("picture_source", else the audio source)
    pic_src: Path = field(default_factory=Path)
    pic_info: dict = field(default_factory=dict)
    pic_a: float = 0.0         # picture time = pic_a + pic_b x audio time (identity without picture_source)
    pic_b: float = 1.0
    audio_start_s: float | None = None   # set when the audio continues the previous range sample for sample
    joined: bool = False       # the audio continues from the previous range (no J-cut, no fades between them)
    fade_in: bool = True
    fade_out: bool = True
    lead: int = 0
    tail: int = 0
    pic_start: float = 0.0     # source time of the first picture frame
    pic_snapped: bool = True   # pic_start is a real source frame (False: sparse VFR, exact time)
    frames: int = 0            # picture frames
    crop: dict | None = None   # {"axis", "kept", "focus_x", "focus_y", "focus_from"} when the shape differs
    speech_src: str = ""
    gap_in: float = 0.0
    gap_out: float = 0.0
    lead_note: str = ""
    key: str = ""
    video_path: Path = field(default_factory=Path)
    audio_path: Path = field(default_factory=Path)
    meta_path: Path = field(default_factory=Path)
    cached: bool = False
    out_start_frame: int = 0
    seconds: float = 0.0

    @property
    def audio_frames(self) -> int:
        return self.lead + self.frames

    @property
    def audio_start(self) -> float:
        """The audio of a range starts where the EDL says (lead frames included), or where the previous
        range's audio ends when the two are one continuous voice."""
        return self.edl_start if self.audio_start_s is None else self.audio_start_s

    @property
    def other_picture(self) -> bool:
        return self.pic_source != self.source

    def to_pic(self, t: float) -> float:
        """Audio-source time -> picture-source time."""
        return self.pic_a + self.pic_b * t


def plan_crops(plans: list[Plan], ranges: list[dict], W: int, H: int, edit_dir: Path, jobs: int) -> None:
    """Crop position of every range whose source shape differs from the output.

    focus_x/focus_y from the EDL win; a large crop (shape ratio beyond ASPECT_WARN) is framed on
    the median face center of the range; otherwise the crop stays centered.
    """
    cache_path = edit_dir / "clips" / "focus_cache.json"
    cache = load_json(cache_path, default={}) or {}
    warned: set[str] = set()
    all_value = []
    for p, r in zip(plans, ranges):
        axis, kept, ratio = crop_geometry(p.pic_info, W, H)
        if axis is None:
            continue
        fx, fy = r.get("focus_x"), r.get("focus_y")
        big = ratio > ASPECT_WARN or ratio < 1.0 / ASPECT_WARN
        if big and p.pic_source not in warned:
            warned.add(p.pic_source)
            dw, dh = p.pic_info.get("display_width"), p.pic_info.get("display_height")
            part = "width" if axis == "x" else "height"
            warn(f'{p.pic_src.name} is {dw}x{dh} and output is {W}x{H}: the cut retains only {kept:.0%} of the {part}. Framing follows the face or explicit focus_x/focus_y. Inspect the contact sheet')
        given = fx if axis == "x" else fy
        p.crop = {"axis": axis, "kept": round(kept, 3), "focus_x": 0.5 if fx is None else fx,
                  "focus_y": 0.5 if fy is None else fy, "focus_from": "edl" if given is not None else "center"}
        if given is None and big:
            key = f'{file_signature(p.pic_src)}|{p.to_pic(p.edl_start):.3f}|{p.to_pic(p.edl_end):.3f}'
            all_value.append((p, key, fx, fy))

    def detect(job):
        p, key, fx, fy = job
        hit = cache.get(key)
        if not (isinstance(hit, list) and len(hit) == 3):
            found = face_focus(p.pic_src, p.to_pic(p.edl_start), p.to_pic(p.edl_end))
            hit = list(found) if found else None
        return job, hit

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(detect, all_value))
    for (p, key, fx, fy), hit in results:
        if hit is None:
            warn(f'interval {p.rid}: face detection unavailable (OpenCV missing); center crop')
            continue
        cache[key] = hit
        if hit[2] == 0:
            warn(f'interval {p.rid}: no face detected at {p.pic_src.name}; center crop. Adjust focus_x/focus_y if needed.')
            continue
        if p.crop["axis"] == "x":
            p.crop["focus_x"] = round(hit[0], 3)
        else:
            p.crop["focus_y"] = round(hit[1], 3)
        p.crop["focus_from"] = "face"
    if results:
        save_json(cache_path, cache)


def plan_cut(edl: dict, edit_dir: Path, fps: int, jcut: dict, probes: dict, jobs: int,
             W: int = 1080, H: int = 1920, sync: dict | None = None) -> list[Plan]:
    ranges = edl["ranges"]
    n = len(ranges)
    sync = sync or {}
    plans: list[Plan] = []
    for i, r in enumerate(ranges):
        src = probes[r["source"]]["path"]
        info = probes[r["source"]]["info"]
        dur = info["duration"]
        if r["start"] >= dur - 1.0 / fps:
            raise CutError(f"interval {r['id']}: start {r['start']:.2f}s exceeds the ending of {src.name} ({dur:.2f}s)", 2)
        end = r["end"]
        if end > dur:
            warn(f"interval {r['id']}: end {end:.2f}s exceeds the ending of {src.name}; using {dur:.2f}s")
            end = dur
        p = Plan(i, r["id"], r["source"], src, info, r["start"], end, r["gain_db"],
                 str(r.get("beat") or ""), str(r.get("quote") or ""))
        p.pic_source = picture_key(r)
        p.pic_src, p.pic_info = probes[p.pic_source]["path"], probes[p.pic_source]["info"]
        if p.other_picture:
            (oa, sa), (ob, sb) = sync[p.source], sync[p.pic_source]
            p.pic_b = sb / sa
            p.pic_a = ob - p.pic_b * oa
            t0, t1 = p.to_pic(p.edl_start), p.to_pic(end)
            pdur = p.pic_info["duration"]
            if t0 < -0.5 / fps or t1 > pdur + 0.5 / fps:
                raise CutError(f'interval {p.rid}: {p.pic_source} has no picture for {p.edl_start:.2f} to {end:.2f}s of {p.source} (synchronization maps this to {t0:.2f} to {t1:.2f} seconds of a total {pdur:.2f}s). Use another camera or shorten the interval', 2)
        plans.append(p)
    for a, b in zip(plans, plans[1:]):
        if a.source == b.source and b.edl_start < a.edl_end - REPEAT_TOL_S and b.edl_end > a.edl_start:
            warn(f'interval {b.rid} repeats {a.edl_end - b.edl_start:.2f}s of interval {a.rid} (same source): start {b.edl_start:.2f}s precedes end {a.edl_end:.2f}s of the preceding interval')
    # One continuous voice: the next range picks up the same audio source where this one stops.
    for a, b in zip(plans, plans[1:]):
        if a.source != b.source or abs(b.edl_start - a.edl_end) > 0.5 / fps:
            continue
        if abs(a.gain_db - b.gain_db) > 0.05:
            warn(f'intervals {a.rid} and {b.rid} continue the same speech with different gain_db values ({a.gain_db:+.1f} and {b.gain_db:+.1f}): speech has a 30 ms join here. Use identical gain_db values to preserve continuous speech')
            continue
        b.joined = True
        a.fade_out = b.fade_in = False
    plan_crops(plans, ranges, W, H, edit_dir, jobs)

    lead_req = jcut["lead_frames"] if jcut["enabled"] else 0
    tail_req = jcut["tail_trim_frames"] if jcut["enabled"] else 0

    # Measured silence at both edges of every range (only needed around junctions).
    def edge_gaps(p: Plan) -> tuple[float, float, str]:
        if not jcut["enabled"] or n == 1:
            return 0.0, 0.0, ""
        stem = Path(p.src).stem
        gap_in = gap_out = 0.0
        how = ""
        if p.idx > 0:
            w1 = min(p.edl_end, p.edl_start + 1.0)
            spans, how = speech_in_window(edit_dir, stem, p.src, p.info, p.edl_start, w1)
            first = min((max(a, p.edl_start) for a, b in spans if b > p.edl_start), default=None)
            gap_in = (w1 - p.edl_start) if first is None else max(0.0, first - p.edl_start)
        if p.idx < n - 1:
            w0 = max(p.edl_start, p.edl_end - 1.0)
            spans, how = speech_in_window(edit_dir, stem, p.src, p.info, w0, p.edl_end)
            last = max((min(b, p.edl_end) for a, b in spans if a < p.edl_end), default=None)
            gap_out = (p.edl_end - w0) if last is None else max(0.0, p.edl_end - last)
        return gap_in, gap_out, how

    with ThreadPoolExecutor(max_workers=max(2, jobs * 2)) as pool:
        gaps = list(pool.map(edge_gaps, plans))

    prev: Plan | None = None
    for p, (gap_in, gap_out, how) in zip(plans, gaps):
        p.gap_in, p.gap_out, p.speech_src = gap_in, gap_out, how
        dur = p.pic_info["duration"]
        # "dry": false (a video on screen, a held ending): the edges are the ones written, no J-cut on it.
        whole = not ranges[p.idx].get("dry", True)
        nxt_joined = p.idx < n - 1 and plans[p.idx + 1].joined
        # Outgoing tail: trim only silence, keeping SPEECH_GUARD_S after the last word.
        p.tail = 0
        if tail_req and p.idx < n - 1 and not whole and not nxt_joined:
            p.tail = max(0, min(tail_req, math.floor((gap_out - SPEECH_GUARD_S) * fps + 1e-6)))
        # Incoming lead: the early audio overlaps the outgoing tail, but the two voices must not
        # meet: keep MIN_JOIN_GAP_S of measured silence between them. It also may not reach back
        # before the outgoing picture starts, nor before the source start.
        p.lead = 0
        if lead_req and prev is not None and not whole and not p.joined:
            room = prev.gap_out - prev.tail / fps + gap_in - MIN_JOIN_GAP_S
            p.lead = max(0, min(lead_req, math.floor(room * fps + 1e-6), prev.frames))
            if p.lead < lead_req:
                p.lead_note = 'insufficient silence at the join' if room * fps < lead_req else "short preceding interval"
        if p.joined and prev is not None:
            # the audio goes on from the exact sample where the previous range's audio stops
            p.audio_start_s = prev.audio_start + (prev.lead + prev.frames) / fps
        p.pic_start, p.pic_snapped = picture_start(p.pic_src, p.to_pic(p.audio_start + p.lead / fps), fps)
        frames = 0
        for _ in range(4):
            total = min(round((p.to_pic(p.edl_end) - p.pic_start) * fps),
                        math.floor((dur - p.pic_start) * fps + 1e-6))
            frames = total - p.tail
            if frames >= MIN_PIC_FRAMES or (p.tail == 0 and p.lead == 0):
                break
            if p.tail:
                p.tail = max(0, p.tail - (MIN_PIC_FRAMES - frames))
            elif p.lead:
                p.lead = 0
                p.pic_start, p.pic_snapped = picture_start(p.pic_src, p.to_pic(p.audio_start), fps)
        if frames < 1:
            raise CutError(f'interval {p.rid} is too short ({p.edl_end - p.edl_start:.3f}s)', 2)
        p.frames = frames
        prev = p
    return plans


# ------------------------------------------------------------------ extraction


def crop_filter(W: int, H: int, crop: dict | None) -> str:
    """crop to WxH, centered, or around the focus point (0..1 of the scaled frame), clamped inside."""
    if not crop or (abs(crop["focus_x"] - 0.5) < 1e-6 and abs(crop["focus_y"] - 0.5) < 1e-6):
        return f'crop={W}:{H}'
    fx, fy = crop["focus_x"], crop["focus_y"]
    return (f'crop={W}:{H}:x=max(0\\,min(iw-ow\\,iw*{fx:.4f}-ow/2)):y=max(0\\,min(ih-oh\\,ih*{fy:.4f}-oh/2))')


def build_vf(info: dict, W: int, H: int, fps: int, grade_filter: str, crop: dict | None = None, fit: str = 'crop') -> str:
    from media_policy import frame_tags, preserve_scale
    # start_time=0: with -noaccurate_seek the frames before the seek point fill the first slot,
    # so a sparse VFR source shows the frame that is on screen at the start time.
    if fit == 'preserve' and info.get('display_width') and info['display_width'] * H != info['display_height'] * W:
        raise CutError('Preserve framing cannot crop a different aspect ratio; confirm contain or crop', 2)
    geometry = [f'scale={W}:{H}:force_original_aspect_ratio={"decrease" if fit == "contain" else "increase"}:flags=lanczos{preserve_scale(info)}']
    geometry.append(f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black' if fit == 'contain' else crop_filter(W, H, crop))
    parts = [f'fps={fps}:start_time=0', *geometry, 'setsar=1']
    # Normalize the deprecated format name inside the range-aware scaler, not a later auto-scale.
    if info.get('pix_fmt') == 'yuvj420p':
        parts.append('format=yuv420p')
    if grade_filter:
        if info.get('is_hdr') or info.get('bit_depth', 8) > 8:
            raise CutError('Creative grading cannot silently reduce HDR or source precision; confirm a tested conversion', 2)
        parts.append(graded_chain(grade_filter))
    if frame_tags(info):
        parts.append(frame_tags(info))
    return ",".join(parts)


AUDIO_RESAMPLE = f'aresample={SR}:async=1:first_pts=0'


def channel_filter(mode: str) -> str:
    """How a source's channels become stereo (mode from channel_mode)."""
    return {"mono": "pan=stereo|c0=c0|c1=c0", "left": "pan=stereo|c0=c0|c1=c0",
            "right": "pan=stereo|c0=c1|c1=c1"}.get(mode, "aformat=channel_layouts=stereo")


def af_tail(gain_db: float, samples: int, fade_in: bool = True, fade_out: bool = True) -> list[str]:
    """Gain + limiter, exact length and 30 ms fades of a range (after resampling and channel choice).
    No fade on a side where the voice goes on into the next range (or comes from the previous one).
    compose_final's background-noise removal runs its denoiser before this same tail."""
    parts = []
    if gain_db:
        parts.append(f'volume={gain_db:.2f}dB')
        if gain_db > 0:
            parts.append("alimiter=limit=0.891:attack=5:release=60:level=false:latency=true")
    fade = max(1, min(FADE_SAMPLES, samples // 4))
    parts += [f'apad=whole_len={samples}', f'atrim=end_sample={samples}']
    if fade_in:
        parts.append(f'afade=t=in:start_sample=0:nb_samples={fade}')
    if fade_out:
        parts.append(f'afade=t=out:start_sample={samples - fade}:nb_samples={fade}')
    parts.append("aformat=sample_fmts=flt:channel_layouts=stereo")
    return parts


def build_af(p: Plan, mode: str, samples: int) -> str:
    return ",".join([AUDIO_RESAMPLE, channel_filter(mode), *af_tail(p.gain_db, samples, p.fade_in, p.fade_out)])


def count_frames(path: Path) -> int:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        return int(proc.stdout.strip().split(",")[0])
    except ValueError:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        return int(proc.stdout.strip().split(",")[0] or 0)


def wav_samples(path: Path) -> int:
    """Sample frames in a WAV by walking its chunks (the stdlib `wave` rejects float WAVs)."""
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"RIFF":
                return -1
            fh.read(4)
            if fh.read(4) != b"WAVE":
                return -1
            block_align = 0
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    return -1
                cid, size = head[:4], int.from_bytes(head[4:], "little")
                if cid == b"fmt ":
                    fmt = fh.read(size)
                    block_align = int.from_bytes(fmt[12:14], "little")
                    continue
                if cid == b"data":
                    if size in (0, 0xFFFFFFFF):   # streamed header: use the file size
                        size = os.path.getsize(path) - fh.tell()
                    return size // block_align if block_align else -1
                fh.seek(size + (size & 1), 1)
    except OSError:
        return -1


def extract(p: Plan, cfg: dict) -> Plan:
    t0 = time.time()
    fps, W, H = cfg["fps"], cfg["width"], cfg["height"]
    spf = SR / fps
    src_fps = p.pic_info.get("fps") or fps
    picture_info = {**p.pic_info, **cfg['media_plan'].get('inputOverride', {})}
    vf = build_vf(picture_info, W, H, cfg.get('fps_str', fps), cfg["grade_filter"], p.crop, cfg.get('fit', 'crop'))
    samples = round((p.out_start_frame + p.frames) * spf) - round((p.out_start_frame - p.lead) * spf)
    audio_start = p.audio_start
    mode = cfg["channel_modes"].get(p.source, "stereo") if p.info.get("has_audio") else "silent"
    af = build_af(p, mode, samples)
    from media_policy import encoder_args, frame_tags
    media = cfg['media_plan']
    if media['filters']:
        vf += ',' + ','.join(media['filters'])
        if frame_tags(media):
            vf += ',' + frame_tags(media)
    # One-second GOP: the preview and HyperFrames seek the cut, and sparse keyframes freeze or fail those seeks.
    enc = [*encoder_args(media, quality=cfg['crf']), '-preset', cfg['x264_preset'], '-g', str(max(1, round(fps))),
           '-video_track_timescale', str(Fraction(cfg.get('fps_str', str(fps))).numerator), '-movflags', '+faststart']
    key_src = {"v": CACHE_VERSION, "src": str(p.src), "sig": cfg['source_hashes'][str(p.src)], "pic": round(p.pic_start, 6),
               "snap": p.pic_snapped, "frames": p.frames, "astart": round(audio_start, 6), "samples": samples, "vf": vf, "af": af,
               "enc": enc, "mode": mode}
    if p.other_picture:
        key_src.update(pic_src=str(p.pic_src), pic_sig=cfg['source_hashes'][str(p.pic_src)])
    p.key = hashlib.sha1(json.dumps(key_src, sort_keys=True).encode()).hexdigest()[:12]
    stem = f"{cfg['mode']}_{p.idx:03d}_{re.sub('[^A-Za-z0-9_-]', '_', p.rid)}_{p.key}"
    clips = cfg["clips_dir"]
    p.video_path, p.audio_path, p.meta_path = clips / f'{stem}.mp4', clips / f'{stem}.wav', clips / f'{stem}.json'
    meta = load_json(p.meta_path)
    if (isinstance(meta, dict) and meta.get("key") == p.key and p.video_path.exists() and p.audio_path.exists()):
        p.frames = int(meta["frames"])
        p.cached = True
        return p

    # A snapped start seeks a quarter frame early so that frame lands on the first output slot.
    pic_seek = max(0.0, p.pic_start - (0.25 / max(src_fps, fps) if p.pic_snapped else 0.0))
    tmp_v, tmp_a = p.video_path.with_suffix(".part.mp4"), p.audio_path.with_suffix(".part.wav")

    def run_extract(hw: bool) -> subprocess.CompletedProcess:
        cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
        if hw:
            cmd += ["-hwaccel", "videotoolbox"]
        cmd += ["-noaccurate_seek", "-ss", f'{pic_seek:.6f}', "-i", str(p.pic_src)]
        if p.info.get("has_audio"):
            cmd += ["-ss", f'{max(0.0, audio_start):.6f}', "-i", str(p.src)]
            amap = "1:a:0"
        else:
            cmd += ["-f", "lavfi", "-i", f'anullsrc=r={SR}:cl=stereo']
            amap = "1:a:0"
        cmd += ["-map", "0:v:0", "-vf", vf, "-frames:v", str(p.frames), "-an", *enc, "-f", "mp4", str(tmp_v)]
        cmd += ["-map", amap, "-af", af, "-vn", "-c:a", "pcm_f32le", "-ar", str(SR), "-ac", "2",
                "-f", "wav", str(tmp_a)]
        return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')

    use_hw = hwaccel_for(p.pic_src, p.pic_info)
    proc = run_extract(use_hw)
    if proc.returncode != 0 and use_hw:
        warn(f'interval {p.rid}: hardware decoding failed; retrying in software')
        proc = run_extract(False)
    if proc.returncode != 0:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
        raise CutError(f'FFmpeg failed to extract interval {p.rid}:\n{proc.stderr.strip()[-1500:]}')

    got = count_frames(tmp_v)
    if got <= 0:
        raise CutError(f'interval {p.rid}: no frame extracted from {p.pic_src.name}')
    if got != p.frames:
        warn(f'interval {p.rid}: expected {p.frames} frames; received {got}; adjusting audio')
        p.frames = got
        samples = round((p.out_start_frame + p.frames) * spf) - round((p.out_start_frame - p.lead) * spf)
        af = build_af(p, mode, samples)
        cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
        if p.info.get("has_audio"):
            cmd += ["-ss", f'{max(0.0, audio_start):.6f}', "-i", str(p.src), "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", f'anullsrc=r={SR}:cl=stereo', "-map", "0:a:0"]
        cmd += ["-af", af, "-vn", "-c:a", "pcm_f32le", "-ar", str(SR), "-ac", "2", "-f", "wav", str(tmp_a)]
        ffmpeg(cmd, f'rebuild audio for interval {p.rid}')
    got_samples = wav_samples(tmp_a)
    if got_samples >= 0 and got_samples != samples:
        raise CutError(f'interval {p.rid}: audio output has {got_samples} samples; expected {samples}')
    os.replace(tmp_v, p.video_path)
    os.replace(tmp_a, p.audio_path)
    save_json(p.meta_path, {"key": p.key, "frames": p.frames, "samples": samples, "id": p.rid,
                            "source": str(p.src), "picture": str(p.pic_src), "pic_start": p.pic_start,
                            "pic_snapped": p.pic_snapped, "audio_start": audio_start})
    p.seconds = time.time() - t0
    return p


# ------------------------------------------------------------------ assembly


def concat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def mix_audio(plans: list[Plan], fps: int, total_samples: int, out_wav: Path) -> None:
    spf = SR / fps
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    for p in plans:
        cmd += ["-i", str(p.audio_path)]
    chains, labels = [], []
    pos = 0
    for i, p in enumerate(plans):
        delay = int(round((pos - p.lead) * spf))
        if delay < 0:
            raise CutError(f'interval {p.rid}: negative audio delay ({delay}); inconsistent J-cut')
        chains.append(f'[{i}:a]adelay=delays={delay}S:all=1[a{i}]')
        labels.append(f'[a{i}]')
        pos += p.frames
    if len(plans) == 1:
        graph = f'{chains[0]};[a0]apad=whole_len={total_samples},atrim=end_sample={total_samples}[out]'
    else:
        graph = (";".join(chains) + ";" + "".join(labels)
                 + f'amix=inputs={len(plans)}:normalize=0:duration=longest:dropout_transition=0,'
                 + f'apad=whole_len={total_samples},atrim=end_sample={total_samples}[out]')
    cmd += ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_f32le", "-ar", str(SR), "-f", "wav", str(out_wav)]
    ffmpeg(cmd, 'mix audio')


def loudnorm_measure(wav_path: Path, targets: dict | None = None) -> dict | None:
    targets = targets or LOUDNORM
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(wav_path), "-af",
         f"loudnorm=I={targets['I']}:TP={targets['TP']}:LRA={targets['LRA']}:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True, encoding='utf-8', errors='replace')
    text = proc.stderr
    if proc.returncode:
        raise CutError('Loudness measurement failed: ' + text[-300:], 2)
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    try:
        if not math.isfinite(float(data["input_i"])) or float(data["input_i"]) < -70:
            return None
    except (KeyError, ValueError):
        return None
    return data


def true_peak_db(path: Path) -> float | None:
    """True peak (dBTP) of the first audio track with 2 decimals (loudnorm's measurement; ebur128,
    used by verify_cut, prints 1 decimal and hides a -0.96 as -1.0). -120 for silence."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-vn", "-af",
                           f"loudnorm=I={LOUDNORM['I']}:TP={LOUDNORM['TP']}:LRA={LOUDNORM['LRA']}:print_format=json",
                           "-f", "null", "-"], capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r'"input_tp"\s*:\s*"(-?inf|-?[\d.]+)"', proc.stderr or "")
    if not m:
        return None
    return -120.0 if "inf" in m.group(1) else float(m.group(1))


# ------------------------------------------------------------------ main


def acquire_lock(clips_dir: Path):
    """One render per edit dir at a time (clips cache and pruning are shared)."""
    fh = open(clips_dir / ".render.lock", "a+", encoding='utf-8')
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:          # Windows: no advisory lock, run anyway
        pass
    except OSError:
        fh.close()
        raise CutError('render_cut is already running in this edit folder. Wait for it to finish.', 2)
    return fh


def render(args: argparse.Namespace) -> int:
    from workspace import managed_operation
    with managed_operation(Path(args.edl).expanduser().resolve().parent):
        return _render(args)


def _render(args: argparse.Namespace) -> int:
    t_all = time.time()
    require_binary("ffmpeg")
    require_binary("ffprobe")
    edl_path = Path(os.path.abspath(args.edl.expanduser()))
    edl = load_edl(edl_path)
    edl_sig = edl_signature(json.loads(edl_path.read_text(encoding="utf-8")))  # raw file, before normalizing
    edit_dir = edl_path.parent
    from project_contract import managed_config, settings_hash
    from media_policy import plan_media
    contract = managed_config(edit_dir, 'cut')
    if contract and edl.get('contractHash') != settings_hash(contract):
        raise CutError('EDL does not match the confirmed project contract; regenerate its settings snapshot', 2)
    if contract:
        from project_contract import validate_edl_scope, digest
        manifest = load_json(edit_dir / 'source_manifest.json')
        state = load_json(edit_dir / 'state.json') or {}
        parent = state.get('parentEdl')
        if parent is not None and digest(parent) != state.get('parentEdlHash'):
            raise CutError('Parent editing baseline changed; restore its original evidence', 2)
        if contract['workflow']['editing']['scope'] == 'intervals' and parent is None:
            if state.get('parentRevision'):
                raise CutError('Interval revision is missing its parent editing baseline', 2)
            parent = {'sources': {s['id']: s['path'] for s in manifest['sources']}, 'ranges': [
                {'source': s['id'], 'start': 0, 'end': s['facts']['duration']} for s in manifest['sources']]}
        validate_edl_scope(contract, manifest, edl, parent_edl=parent)
    jcut = jcut_settings(edl, args.in_jcut)

    probes: dict[str, dict] = {}
    audio_keys = {r["source"] for r in edl["ranges"]}
    picture_keys = {picture_key(r) for r in edl["ranges"]}
    for key, value in edl["sources"].items():
        if key not in audio_keys and key not in picture_keys:
            continue
        src = resolve_source(str(value), edit_dir)
        if not src.exists():
            raise CutError(f'source video {key!r} not found: {src}', 2)
        try:
            info = probe_video(src)
        except Exception as exc:  # noqa: BLE001
            raise CutError(f'could not read {src.name}: {exc}', 2)
        if key in picture_keys and not info.get("has_video"):
            if key in audio_keys:
                raise CutError(f'{src.name} has no video stream; provide an alternate picture source for its intervals', 2)
            raise CutError(f'{src.name} has no video stream', 2)
        from common import content_hash
        probes[key] = {"path": src, "info": info, 'sha256': content_hash(src)}
    sync, sync_path = load_sync(edl, edit_dir, probes)

    out_cfg = dict(edl.get("output") or {})
    if contract:
        video = contract['workflow']['video']
        if any(key in out_cfg and str(out_cfg[key]) != str(video[target]) for key, target in (('width', 'width'), ('height', 'height'))):
            raise CutError('EDL dimensions conflict with the confirmed output snapshot', 2)
        if out_cfg.get('fps') and Fraction(str(out_cfg['fps'])) != Fraction(video['frameRate']):
            raise CutError('EDL frame rate conflicts with the confirmed output snapshot', 2)
        out_cfg.update(width=video['width'], height=video['height'], fps=video['frameRate'])
    first_info = next(pr['info'] for key, pr in probes.items() if key in picture_keys)
    fmt = out_cfg.get("format")
    if fmt and fmt not in OUTPUT_FORMATS and not (out_cfg.get("width") and out_cfg.get("height")):
        raise CutError(f"output.format {fmt!r} unknown (use {', '.join(OUTPUT_FORMATS)})", 2)
    default_size = OUTPUT_FORMATS[fmt] if fmt else (first_info['display_width'], first_info['display_height'])
    W = int(out_cfg.get("width") or default_size[0])
    H = int(out_cfg.get("height") or default_size[1])
    fps_str = str(Fraction(str(out_cfg.get('fps') or first_info['fps_str'])))
    fps = float(Fraction(fps_str))
    if not 0 < fps <= 240:
        raise CutError('Output frame rate must be explicitly known and within the supported range', 2)
    variable_rate = contract['workflow']['video']['timing'] == 'vfr' if contract else any(pr['info'].get('is_vfr') for key, pr in probes.items() if key in picture_keys) and not out_cfg.get('fps')
    if args.preview:
        W, H = (W // 2) - (W // 2) % 2, (H // 2) - (H // 2) % 2
    if W % 2 or H % 2:
        raise CutError('Odd output dimensions require an explicit adjustment; no automatic rounding', 2)

    try:
        grade_filter = resolve_grade(edl.get("grade"))
    except ValueError as exc:
        raise CutError(str(exc), 2)
    err = check_filter(grade_filter)
    if err:
        raise CutError(f'the EDL grade is unsupported by FFmpeg: {err}', 2)
    if contract and grade_filter and edl.get('grade') != contract['workflow']['color'].get('grade'):
        raise CutError('EDL grade was not confirmed in project settings', 2)
    effective = contract or {'workflow': {'color': {'mode': 'convert' if grade_filter else 'preserve', 'target': 'source', 'grade': edl.get('grade')}}}
    media_plan = plan_media([pr['info'] for key, pr in probes.items() if key in picture_keys], effective, None, 'master')
    grade_filter = ''  # The shared media plan owns each requested transform exactly once.

    jobs = args.jobs if args.jobs and args.jobs > 0 else min(4, max(1, (os.cpu_count() or 4) // 3))
    out_path = (Path(os.path.abspath(args.output.expanduser())) if args.output
                else edit_dir / ("cut_preview.mp4" if args.preview else "cut.mp4"))
    from common import ensure_distinct_output
    protected = [pr['path'] for pr in probes.values()]
    if contract:
        protected += [source['path'] for source in manifest['sources']]
    ensure_distinct_output(out_path, protected)
    if contract and (out_path != edit_dir / ('cut_preview.mp4' if args.preview else 'cut.mp4') or out_path.is_symlink()):
        raise CutError('Managed cut output belongs to this revision as edit/cut.mp4 (or cut_preview.mp4); use workspace export for delivery', 2)
    if variable_rate:
        if args.preview:
            raise CutError('VFR cut-only preserves source geometry; inspect the full-resolution candidate instead of a resized frame-grid preview', 2)
        from render_vfr import render_vfr
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return render_vfr(edl, edit_dir, probes, contract, media_plan, out_path, parent_edl=parent if contract else None)
    timeline_path = out_path.with_name(f'{out_path.stem}_timeline.json')
    clips_dir = edit_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(clips_dir)

    mode = "prev" if args.preview else "full"
    log(f"Cut: {len(edl['ranges'])} intervals from {len(probes)} source(s) -> {W}x{H} {fps} fps{(' (preview)' if args.preview else '')}")
    if jcut["enabled"]:
        log(f"J-cut: audio enters {jcut['lead_frames']} frames early; tail trimmed to {jcut['tail_trim_frames']} frames outside speech")
    else:
        log('Hard joins without J-cuts')
    if sync:
        log(f"Alternate-camera picture at {sum((1 for r in edl['ranges'] if r.get('picture_source')))} intervals, synchronization from {sync_path.name}")

    t_plan = time.time()
    plans = plan_cut(edl, edit_dir, fps, jcut, probes, jobs, W, H, sync)

    def validate_effective_scope():
        if not contract or (contract['workflow']['editing']['scope'] != 'intervals' and
                            not contract['workflow']['editing'].get('protectedIntervals')):
            return
        for picture in (False, True):
            ranges = []
            for plan, original in zip(plans, edl['ranges']):
                audio = not picture and plan.info.get('has_audio')
                start = plan.audio_start if audio else (plan.pic_start - plan.pic_a) / plan.pic_b
                end = (plan.audio_start + plan.audio_frames / fps if audio else
                       (plan.pic_start + plan.frames / fps - plan.pic_a) / plan.pic_b)
                ranges.append({**original, 'start': start, 'end': end})
            try:
                validate_edl_scope(contract, manifest, {**edl, 'ranges': ranges}, parent_edl=parent)
            except ValueError as error:
                raise CutError(f'Planned cut violates confirmed scope: {error}. Disable J-cuts or adjust ranges to preserve the scope.', 2) from None

    validate_effective_scope()
    out_position = 0
    for plan in plans:
        plan.out_start_frame = out_position
        out_position += plan.frames
    channel_modes = {}
    for key, pr in probes.items():
        if key in audio_keys:
            channel_modes[key] = channel_mode(pr["path"], pr["info"]) if pr["info"].get("has_audio") else "silent"
    t_plan = time.time() - t_plan

    cfg = {"fps": fps, 'fps_str': fps_str, 'media_plan': media_plan, 'fit': contract['workflow']['video']['fit'] if contract else 'crop', "width": W, "height": H, "grade_filter": grade_filter, "channel_modes": channel_modes,
           "x264_preset": "veryfast" if args.preview else "medium", "crf": 26 if args.preview else None,
           "clips_dir": clips_dir, "mode": mode,
           'source_hashes': {str(pr['path']): pr['sha256'] for pr in probes.values()}}
    t_ext = time.time()
    log(f'Extracting {len(plans)} intervals ({jobs} in parallel)...')
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(extract, p, cfg) for p in plans]
        plans = [f.result() for f in futures]
    validate_effective_scope()
    t_ext = time.time() - t_ext
    for a, b in zip(plans, plans[1:]):
        if b.joined and abs(a.audio_start + (a.lead + a.frames) / fps - b.audio_start) > 0.5 / SR:
            warn(f'speech continues from {a.rid} to {b.rid} became misaligned (interval {a.rid} changed frame count during extraction); speech may jump. Run again')

    # Frame-accurate positions from the encoded clips.
    pos = 0
    total_frames = sum(p.frames for p in plans)
    total_samples = int(round(total_frames * SR / fps))
    duration = total_frames / fps

    t_mix = time.time()
    mix_wav = clips_dir / f'{mode}_mix.wav'
    mix_audio(plans, fps, total_samples, mix_wav)
    audio_policy = contract['workflow']['audio'] if contract else {'mode': 'normalize', 'targetLufs': LOUDNORM['I'], 'processingPeak': LOUDNORM['TP'], 'truePeakLimit': TRUE_PEAK_LIMIT}
    has_audio = any(pr['info'].get('has_audio') for key, pr in probes.items() if key in audio_keys)
    targets = {'I': audio_policy['targetLufs'], 'TP': audio_policy['processingPeak'], 'LRA': LOUDNORM['LRA']}
    measured = loudnorm_measure(mix_wav, targets) if has_audio and audio_policy['mode'] == 'normalize' else None
    t_mix = time.time() - t_mix

    concat_list = clips_dir / f'{mode}_concat.txt'
    lines = ["ffconcat version 1.0"]
    for p in plans:
        lines.append(f"file '{concat_escape(p.video_path)}'")
        lines.append(f'duration {p.frames / fps:.6f}')
    concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

    normal_wav = clips_dir / f'{mode}_norm.wav'
    if measured:
        af = (f"loudnorm=I={targets['I']}:TP={targets['TP']}:LRA={targets['LRA']}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true,aresample={SR}")
        ffmpeg(["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(mix_wav), "-af", af, "-c:a", "pcm_f32le",
                "-ar", str(SR), "-ac", "2", "-f", "wav", str(normal_wav)], "normalize volume")
        audio_in = normal_wav
    else:
        if has_audio and audio_policy['mode'] == 'normalize':
            raise CutError('Normalization requires a finite voice measurement; choose preserve for silent audio', 2)
        audio_in = mix_wav

    t_mux = time.time()
    tmp_out = out_path.with_name(f'.{out_path.stem}.part.mp4')

    def mux(gain_db: float) -> None:
        af_mux = ["-af", f'volume={gain_db:.2f}dB'] if abs(gain_db) > 0.001 else []
        audio_args = ["-i", str(audio_in)] if has_audio else []
        audio_map = ["-map", "1:a:0", *af_mux, "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2"] if has_audio else ['-an']
        ffmpeg(["ffmpeg", "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                *audio_args, "-map", "0:v:0", "-c:v", "copy", *audio_map, "-t", f'{duration:.6f}',
                "-movflags", "+faststart", "-f", "mp4", str(tmp_out)], f'assemble {out_path.name}')

    peak_gain = 0.0
    try:
        mux(peak_gain)
        normalize = has_audio and audio_policy['mode'] == 'normalize'
        out_tp = true_peak_db(tmp_out) if normalize else None
        # The AAC encoder adds inter-sample peaks over the loudnorm target: lower just enough and encode again.
        peak_goal = audio_policy['truePeakLimit'] - TRUE_PEAK_MARGIN
        for _ in range(3):
            if out_tp is None or out_tp <= peak_goal:
                break
            peak_gain += peak_goal - out_tp - 0.15
            mux(peak_gain)
            out_tp = true_peak_db(tmp_out)
        if normalize and (out_tp is None or out_tp > audio_policy['truePeakLimit']):
            raise CutError('Encoded audio could not meet the confirmed true-peak limit', 2)
        for pr in probes.values():
            if content_hash(pr['path']) != pr['sha256']:
                raise CutError('Source changed during rendering; no candidate was published', 2)
        if contract:
            managed_config(edit_dir, 'cut')
        os.replace(tmp_out, out_path)
    finally:
        tmp_out.unlink(missing_ok=True)
        normal_wav.unlink(missing_ok=True)
    t_mux = time.time() - t_mux

    got_frames = count_frames(out_path)
    if got_frames != total_frames:
        warn(f'the final file has {got_frames} frames; the timeline expects {total_frames}')

    segments = []
    for p in plans:
        out_start_f, out_end_f = pos, pos + p.frames
        src_start = p.pic_start
        src_end = p.pic_start + p.frames / fps
        image_value = {"picture_source": None}
        if p.other_picture:
            # src_start/src_end stay on the audio source's clock (the voice tools read them); the picture has its own
            image_value = {"picture_source": p.pic_source, "picture_path": str(p.pic_src),
                      "picture_src_start": round(src_start, 6), "picture_src_end": round(src_end, 6)}
            src_start = p.audio_start + p.lead / fps
            src_end = p.audio_start + (p.lead + p.frames) / fps
        segments.append({
            "id": p.rid, "source": p.source, "source_path": str(p.src),
            "audio_mode": channel_modes.get(p.source, "stereo"),
            "src_start": round(src_start, 6), "src_end": round(src_end, 6), **image_value,
            "audio_continues": p.joined, "audio_fade_in": p.fade_in, "audio_fade_out": p.fade_out,
            "out_start": round(out_start_f / fps, 6), "out_end": round(out_end_f / fps, 6),
            "audio_out_start": round((out_start_f - p.lead) / fps, 6), "audio_out_end": round(out_end_f / fps, 6),
            "beat": p.beat, "quote": p.quote,
            "out_start_frame": out_start_f, "out_end_frame": out_end_f, "frames": p.frames,
            "lead_frames": p.lead, "tail_trim_frames": p.tail,
            "audio_src_start": round(p.audio_start, 6), "pic_snapped": p.pic_snapped,
            "edl_start": p.edl_start, "edl_end": p.edl_end, "gain_db": p.gain_db,
            "crop": p.crop,
        })
        pos = out_end_f

    timeline = {
        "edl_sig": edl_sig, "fps": fps, 'fps_str': fps_str, 'mediaPolicy': media_plan,
        'contractHash': settings_hash(contract) if contract else None, "width": W, "height": H,
        "duration": round(duration, 6), "frames": total_frames,
        "video": out_path.name, "preview": bool(args.preview),
        "jcut": jcut, "grade": edl.get("grade") or "",
        "loudnorm_input": ({k: float(measured[k]) for k in ("input_i", "input_tp", "input_lra")} if measured else None),
        "true_peak_db": out_tp, "peak_guard_db": round(peak_gain, 2),
        "hdr_sources": sorted(k for k, pr in probes.items() if pr["info"].get("is_hdr")),
        "synchronization": ({"file": str(sync_path), "sources": {k: {"offset": v[0], "scale": v[1]}
                                                              for k, v in sync.items()}} if sync else None),
        "rendered_at": now_iso(),
        "segments": segments,
    }
    save_json(timeline_path, timeline)

    # Only a full render inside the edit dir drives the preview. It does not ask for approval yet:
    # verify_cut.py does that after checking the file.
    state_note = ""
    if out_path.parent == edit_dir and not args.preview:
        state_path = edit_dir / "state.json"
        state = load_json(state_path, default={}) or {}
        state.update({
            "phase": "cut", "awaiting": None,
            "message": "Cut rendered. Verifying before presentation...",
            "cut_video": out_path.name, "cut_timeline": timeline_path.name, "updated_at": now_iso(),
        })
        save_json(state_path, state)
        state_note = ' · state.json updated; approval requires verify_cut'

    # Drop stale cached clips of this quality mode and the temp mix.
    keep = {q.video_path.name for q in plans} | {q.audio_path.name for q in plans} | {q.meta_path.name for q in plans}
    for f in clips_dir.glob(f'{mode}_*'):
        if f.name not in keep:
            f.unlink(missing_ok=True)

    log("")
    log(f"{'id':6s} {'source':10s} {'origin':>15s}  {'in cut':>17s}  {'frames':>7s}  lead tail")
    for s, p in zip(segments, plans):
        pic = f'  picture from {p.pic_source} at {p.pic_start:.2f}s' if p.other_picture else ""
        log(f"{s['id']:6s} {s['source'][:10]:10s} {s['src_start']:6.2f}-{s['src_end']:6.2f}s  {fmt_time(s['out_start'])}-{fmt_time(s['out_end'])}  {s['frames']:7d}  {p.lead:4d} {p.tail:4d}{('  (cache)' if p.cached else '')}{('  reduced lead: ' + p.lead_note if p.lead_note else '')}{('  voice continues from the previous range' if p.joined else '')}{pic}")
    for s in segments:
        if s["crop"] and s["crop"]["focus_from"] != "center":
            c = s["crop"]
            log(f"Framing {s['id']}: focus_x {c['focus_x']:.2f} focus_y {c['focus_y']:.2f} ({c['focus_from']})")
    if measured:
        log(f"Original loudness: {float(measured['input_i']):.1f} LUFS, peak {float(measured['input_tp']):.1f} dBTP -> target {LOUDNORM['I']:.0f} LUFS")
    if out_tp is not None and out_tp <= -100:
        log('File true peak: no sound')
    elif out_tp is not None:
        guard =f' (volume {peak_gain:+.1f} dB to remain below {TRUE_PEAK_LIMIT:.0f})' if peak_gain else ""
        log(f'File true peak: {out_tp:.2f} dBTP{guard}')
    log(f'Done: {out_path} · {fmt_time(duration)} ({total_frames} frames){state_note}')
    log(f'Timeline: {timeline_path}')
    log(f'Time: total {time.time() - t_all:.1f}s (plan {t_plan:.1f}s, extraction {t_ext:.1f}s, audio {t_mix:.1f}s, composition {t_mux:.1f}s)')
    mix_wav.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)
    lock.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Render cut.mp4 and cut_timeline.json from edl.json.")
    ap.add_argument('edl', type=Path, help='edl.json path; its parent is the edit folder')
    ap.add_argument("-o", "--output", type=Path, help='output video (default: cut.mp4 beside the EDL)', dest='output')
    ap.add_argument("--jobs", type=int, default=0, help='parallel interval extraction (automatic by default)', dest='jobs')
    ap.add_argument("--no-jcut", action="store_true", help='hard joins without J-cuts', dest='in_jcut')
    ap.add_argument("--preview", action="store_true",
                    help='quick preview at half resolution and CRF 26; writes cut_preview.mp4 without changing cut.mp4 or state.json', dest='preview')
    args = ap.parse_args()
    try:
        return render(args)
    except CutError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return exc.code
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.modules.setdefault('render_cut', sys.modules[__name__])
    sys.exit(main())
