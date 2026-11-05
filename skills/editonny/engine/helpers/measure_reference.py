#!/usr/bin/env python3
"Measure a reference video's editing language without copying its content.\n\nUsage: uv run --directory ENGINE python helpers/measure_reference.py VIDEO --output FOLDER\nOptional controls include --threshold, --without-speech, --without-text, --transcript, and --engine-dir. The engine supplies transcription, screen-text analysis, face detection, and fonts. EDITONNY_ENGINE_DIR selects another prepared engine when needed.\n\nWrite a style guide distinguishing measured observations, estimates, and unknowns; a per-second interval table; editing-rule JSON; timestamped contact sheets; before/after cut images; full-size caption/text samples; and raw measurements. Keep all outputs in the requested destination. Never write into the reference video's directory or infer unseen facts from appearance alone. Use only the current English output filenames."

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print('numpy is missing. Run this helper with the locked engine environment.', file=sys.stderr)
    sys.exit(2)
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None
from PIL import Image, ImageDraw, ImageFont

VERSION_VALUE = 1
SKILL = Path(__file__).resolve().parent.parent   # this skill (helpers/..)
EV_DEFAULT = Path(os.environ.get("EDITONNY_ENGINE_DIR") or SKILL)
SR = 22050            # Sample rate of the analyzed audio.
WINDOW = 1024         # Spectrum samples per window (46 ms)
STEP = 256           # Samples between windows (11.6 ms)
BANDS = {"sub": (25, 70), "bass": (70, 200), 'body': (200, 800), "voice": (800, 3000), "treble": (3500, 10000)}
FLOOR_SCENE = 0.08      # Minimum scene score for inspecting a peak.
RATIO_PROBABLE = 8.0  # A weak peak is a likely cut when it exceeds its neighborhood by a factor of eight.
FILE_INTERVALS = "measured-intervals.csv"        # One second per row
FILE_RULES = "reference-rules.json"   # Machine-readable measurements.


class MeasurementError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- small utilities

_T0 = time.time()


def announce(msg: str) -> None:
    if re.match(r"\d/\d ", msg):
        msg = f'{msg} ({int(time.time() - _T0)} s)'
    print(msg, flush=True)


def notice(msg: str) -> None:
    print(f'notice: {msg}', flush=True)


def utf8_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def run_command(cmd: list[str], cwd: Path | None = None, check_value: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True)
    except FileNotFoundError as exc:
        raise MeasurementError(f"program not found: '{cmd[0]}'. Install FFmpeg (on macOS: brew install ffmpeg).") from exc
    if check_value and r.returncode != 0:
        end_value = r.stderr.decode("utf-8", "replace").strip()[-600:]
        raise MeasurementError(f'Command {Path(cmd[0]).name} failed: {end_value}')
    return r


def mmss(t: float) -> str:
    t = max(0.0, float(t))
    m = int(t // 60)
    return f'{m:02d}:{t - 60 * m:05.2f}'


def mmss_short(t: float) -> str:
    t = max(0.0, float(t))
    return f'{int(t // 60):02d}:{int(t % 60):02d}'


def format_number(x: float | None, decimals_value: int = 1) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "?"
    return f'{x:.{decimals_value}f}'


def pct(x: float | None, decimals_value: int = 0) -> str:
    return "?" if x is None else f'{format_number(100 * x, decimals_value)}%'


def hex_color(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(round(max(0, min(255, float(c))))) for c in rgb)


def normal_txt(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", s)).strip()


def save_json_value(path_value: Path, data_value) -> None:
    tmp = path_value.with_suffix(path_value.suffix + ".tmp")
    tmp.write_text(json.dumps(data_value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path_value)


def engine_python(engine: Path) -> list[str]:
    'Python interpreter for engine helpers; reuse this interpreter when it belongs to the same engine.'
    try:
        if engine.resolve() == SKILL:
            return [sys.executable]
    except OSError:
        pass
    for p in (engine / ".venv" / "bin" / "python", engine / ".venv" / "Scripts" / "python.exe"):
        if p.exists():
            return [str(p)]
    return ["uv", "run", "--directory", str(engine), "python"]


def load_font_value(engine: Path, size_value: int):
    for p in (engine / "assets" / "fonts" / "figtree" / "Figtree-Variable.ttf",
              Path("/System/Library/Fonts/Menlo.ttc"),
              Path("C:/Windows/Fonts/arial.ttf"),
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        try:
            f = ImageFont.truetype(str(p), size_value)
            try:
                f.set_variation_by_axes([700])
            except Exception:
                pass
            return f
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size_value)
    except TypeError:
        return ImageFont.load_default()


# ----------------------------------------------------------------------------- 1. technical metadata

def _fraction(txt: str | None) -> float:
    if not txt or txt in ("0/0", "N/A"):
        return 0.0
    if "/" in txt:
        a, b = txt.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(txt)


def _ratio(w: int, h: int) -> str:
    target_value = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9, "3:4": 3 / 4, "4:3": 4 / 3, "2:3": 2 / 3}
    r = w / h
    name_value, val = min(target_value.items(), key=lambda kv: abs(kv[1] - r))
    return name_value if abs(val - r) / val < 0.03 else f'{w}:{h}'


def technical_metadata(video: Path) -> dict:
    r = run_command(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(video)])
    d = json.loads(r.stdout.decode("utf-8", "replace"))
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video"
          and not (s.get("disposition") or {}).get("attached_pic")]
    if not vs:
        raise MeasurementError('this file has no video image.')
    v = vs[0]
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"])))
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    fps_med = _fraction(v.get("avg_frame_rate"))
    fps_nom = _fraction(v.get("r_frame_rate"))
    fps = fps_med or fps_nom or 30.0
    sha = hashlib.sha256()
    with open(video, "rb") as fh:
        for block_value in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block_value)
    return {
        "file": video.name,
        "sha256": sha.hexdigest(),
        'size_mb': round(video.stat().st_size / 1e6, 2),
        'duration_s': round(dur, 3),
        "width": w, "height": h, "rotation": rot,
        "aspect_ratio": _ratio(w, h),
        "vertical": h > w,
        "fps": round(fps, 3),
        "variable_fps": bool(fps_nom and fps_med and abs(fps_nom - fps_med) > 0.5),
        "video_codec": v.get("codec_name"),
        "has_audio": a is not None,
        "audio_codec": a.get("codec_name") if a else None,
        "audio_channels": int(a.get("channels") or 0) if a else 0,
        "audio_sample_rate_hz": int(a.get("sample_rate") or 0) if a else 0,
    }


# ----------------------------------------------------------------------------- 2. cuts

def score_scenes(video: Path, tmp: Path) -> tuple[np.ndarray, np.ndarray]:
    'FFmpeg scene-change score from 0 to 1 for each frame.'
    run_command(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-an", "-sn", "-dn", "-vf",
           "scale=320:-2,select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score:file=scene-scores.txt",
           "-f", "null", "-"], cwd=tmp)
    times_value: list[float] = []
    scores: list[float] = []
    t = None
    for line_value in (tmp / "scene-scores.txt").read_text(errors="replace", encoding='utf-8').splitlines():
        m = re.search(r"pts_time:(-?[\d.]+)", line_value)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"scene_score=([\d.]+)", line_value)
        if m and t is not None:
            times_value.append(t)
            scores.append(float(m.group(1)))
            t = None
    return np.array(times_value, dtype=np.float64), np.array(scores, dtype=np.float64)


def find_cuts(times_value: np.ndarray, scores: np.ndarray, dur: float, threshold_value: float) -> list[dict]:
    n = len(scores)
    cand: list[dict] = []
    for i in range(1, n):
        t, s = float(times_value[i]), float(scores[i])
        if s < FLOOR_SCENE or t < 0.1 or t > dur - 0.05:
            continue
        if s < scores[max(0, i - 3):i + 4].max():
            continue
        viz = np.concatenate([scores[max(0, i - 15):max(0, i - 2)], scores[i + 3:i + 16]])
        base = float(np.median(viz)) if len(viz) else 0.0
        ratio_value = s / max(base, 0.003)
        if s >= threshold_value:
            type_value = "cut"
        elif ratio_value >= RATIO_PROBABLE:
            type_value = "probable"
        else:
            continue
        cand.append({"t": round(t, 3), 'frame': i, "note": round(s, 3), "ratio": round(ratio_value, 1), "type": type_value})
    groups_value: list[list[dict]] = []
    for c in cand:
        if groups_value and c["t"] - groups_value[-1][-1]["t"] < 0.25:
            groups_value[-1].append(c)
        else:
            groups_value.append([c])
    cuts_value = []
    for g in groups_value:
        best_value = dict(max(g, key=lambda c: c["note"]))
        if any(c["type"] == "cut" for c in g):
            best_value["type"] = "cut"
        best_value['adjacent_peaks'] = len(g)
        if len(g) >= 3:
            best_value["notes"] = 'several consecutive changed frames may indicate an animated transition'
        cuts_value.append(best_value)
    return cuts_value


def measure_pacing(cuts_t: list[float], dur: float) -> dict:
    cuts_t = sorted(cuts_t)
    edges_value = [0.0] + cuts_t + [dur]
    plans_value = [b - a for a, b in zip(edges_value, edges_value[1:]) if b - a > 0.01]
    inter = [b - a for a, b in zip(cuts_t, cuts_t[1:])]
    n = len(cuts_t)
    largest_i = int(np.argmax(plans_value)) if plans_value else 0
    per10 = []
    for a in range(0, int(math.ceil(dur / 10.0))):
        per10.append(sum(1 for t in cuts_t if a * 10 <= t < (a + 1) * 10))
    return {
        "cuts": n,
        'cuts_per_minute': round(n / (dur / 60.0), 1) if dur > 0 else 0.0,
        'mean_shot_s': round(float(np.mean(plans_value)), 2) if plans_value else round(dur, 2),
        'median_shot_s': round(float(np.median(plans_value)), 2) if plans_value else round(dur, 2),
        'average_cut_interval_s': round(float(np.mean(inter)), 2) if inter else None,
        'shortest_shot_s': round(min(plans_value), 2) if plans_value else None,
        'longest_shot_s': round(max(plans_value), 2) if plans_value else None,
        'longest_uncut_interval': {"start": round(edges_value[largest_i], 2), "end": round(edges_value[largest_i + 1], 2)}
        if plans_value else None,
        'short_shots_pct': round(sum(1 for p in plans_value if p < 1.5) / len(plans_value), 3) if plans_value else 0.0,
        'cuts_in_first_3s': sum(1 for t in cuts_t if t < 3.0),
        'first_cut_s': round(cuts_t[0], 3) if cuts_t else None,
        'cuts_per_10s': per10,
    }


# ----------------------------------------------------------------------------- 3. frames

def screen_default(w: int, h: int) -> tuple[int, int]:
    'Reference canvas: 1080x1920 vertical, 1920x1080 horizontal, or 1080x1080 square.'
    r = w / h
    if r < 0.9:
        return 1080, 1920
    if r > 1.1:
        return 1920, 1080
    return 1080, 1080


def size_output(w: int, h: int, side_value: int) -> tuple[int, int]:
    scale = min(1.0, side_value / max(w, h))
    return max(2, int(round(w * scale / 2)) * 2), max(2, int(round(h * scale / 2)) * 2)


def _times_showinfo(stderr: bytes) -> list[float]:
    txt = stderr.decode("utf-8", "replace")
    return [float(m.group(1)) for m in re.finditer(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+)", txt)]


def frames_per_second(video: Path, directory: Path, w: int, h: int) -> list[tuple[float, Path]]:
    directory.mkdir(parents=True, exist_ok=True)
    vf = f"select='isnan(prev_t)+gt(floor(t)\\,floor(prev_t))',scale={w}:{h},showinfo"
    r = run_command(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-i", str(video),
               "-an", "-sn", "-vf", vf, "-fps_mode", "vfr", "-q:v", "3", str(directory / "q_%05d.jpg")])
    times_value = _times_showinfo(r.stderr)
    files_value = sorted(directory.glob("q_*.jpg"))
    if len(times_value) != len(files_value):
        times_value = [float(i) for i in range(len(files_value))]
    return list(zip(times_value, files_value))


def frames_by_index(video: Path, times_q: np.ndarray, indices: list[int], directory: Path,
                       w: int, h: int) -> dict[int, Path]:
    'Extract exact indexed frames, batching consecutive frames into one call.'
    n_total = len(times_q)
    requests_value = sorted({int(i) for i in indices if 0 <= i < n_total})
    if not requests_value:
        return {}
    directory.mkdir(parents=True, exist_ok=True)
    groups_value: list[list[int]] = []
    for i in requests_value:
        if groups_value and i - groups_value[-1][-1] == 1 and len(groups_value[-1]) < 8:
            groups_value[-1].append(i)
        else:
            groups_value.append([i])

    def remove_value(g: list[int]) -> list[tuple[int, Path]]:
        k0 = g[0]
        target_value = (float(times_q[k0 - 1]) + float(times_q[k0])) / 2 if k0 > 0 else 0.0
        pref = f'g{k0:07d}_'
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f'{target_value:.4f}', "-i", str(video), "-an", "-sn",
                        "-frames:v", str(len(g)), "-vf", f'scale={w}:{h}', "-fps_mode", "passthrough", "-q:v", "3",
                        str(directory / f'{pref}%02d.jpg')], capture_output=True)
        return list(zip(g, sorted(directory.glob(f'{pref}*.jpg'))))

    output_value: dict[int, Path] = {}
    with ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) // 2))) as ex:
        for pairs in ex.map(remove_value, groups_value):
            output_value.update(dict(pairs))
    return output_value


# ----------------------------------------------------------------------------- 4. face

class FaceDetector:
    def __init__(self, engine: Path):
        self.det = None
        model_value = engine / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
        if cv2 is not None and hasattr(cv2, "FaceDetectorYN") and model_value.exists():
            try:
                self.det = cv2.FaceDetectorYN.create(str(model_value), "", (320, 320), 0.7, 0.3, 50)
            except Exception:
                self.det = None

    @property
    def ok(self) -> bool:
        return self.det is not None

    def measure_value(self, img) -> dict | None:
        if self.det is None or img is None:
            return None
        if max(img.shape[:2]) > 640:   # Measurements use frame fractions; smaller images find the same face faster.
            scale = 640.0 / max(img.shape[:2])
            img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        try:
            _, faces = self.det.detect(img)
        except Exception:
            return None
        if faces is None or len(faces) == 0:
            return None
        f = max(faces, key=lambda r: r[2] * r[3])
        x, y, fw, fh = [float(v) for v in f[:4]]
        return {"height": round(fh / h, 4), "width": round(fw / w, 4),
                "cx": round((x + fw / 2) / w, 4), "cy": round((y + fh / 2) / h, 4),
                "eye_y": round(float(f[5] + f[7]) / 2 / h, 4), "conf": round(float(f[14]), 3),
                'faces': int(len(faces))}


def same_scene(a, b) -> bool:
    'Determine whether frames share a recording by background colors despite different zoom levels.'
    if a is None or b is None:
        return False
    ha = cv2.calcHist([cv2.cvtColor(a, cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 16], [0, 180, 0, 256])
    hb = cv2.calcHist([cv2.cvtColor(b, cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 16], [0, 180, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    return float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL)) >= 0.75


def type_start_plan(height_face: float | None) -> str:
    if height_face is None:
        return 'no face'
    if height_face >= 0.30:
        return "extreme close-up"
    if height_face >= 0.17:
        return "close-up"
    if height_face >= 0.09:
        return 'medium shot'
    return "shot aberto"


# ----------------------------------------------------------------------------- 5. cores

def _lab(px_bgr: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(px_bgr.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    lab[:, 0] *= 100.0 / 255.0
    lab[:, 1:] -= 128.0
    return lab


def _lab_hex(lab) -> str:
    l8 = np.array([[[lab[0] * 255.0 / 100.0, lab[1] + 128.0, lab[2] + 128.0]]]).clip(0, 255).astype(np.uint8)
    return hex_color(cv2.cvtColor(l8, cv2.COLOR_LAB2RGB)[0, 0])


def _kmeans(lab: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    k = max(1, min(k, len(lab)))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    cv2.setRNGSeed(7)
    _, rot, centers = cv2.kmeans(lab.astype(np.float32), k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    return rot.ravel(), centers


def _join_colors(items_value: list[dict], limit_value: float) -> list[dict]:
    'Merge weighted Lab colors whose distance is below the threshold.'
    groups_value: list[dict] = []
    for it in sorted(items_value, key=lambda x: -x["weight"]):
        for g in groups_value:
            if float(np.linalg.norm(g["lab"] - it["lab"])) < limit_value:
                tot = g["weight"] + it["weight"]
                g["lab"] = (g["lab"] * g["weight"] + it["lab"] * it["weight"]) / tot
                g["weight"] = tot
                g["n"] = g.get("n", 1) + it.get("n", 1)
                break
        else:
            groups_value.append({"lab": np.array(it["lab"], dtype=np.float32), "weight": it["weight"], "n": it.get("n", 1)})
    return sorted(groups_value, key=lambda g: -g["weight"])


def describe_color(lab) -> str:
    l, a, b = [float(v) for v in lab]
    chroma = math.hypot(a, b)
    if chroma < 10:
        if l > 85:
            return "white"
        if l < 15:
            return "black"
        return "light gray" if l > 60 else ("gray" if l > 35 else "dark gray")
    ang = (math.degrees(math.atan2(b, a)) + 360) % 360
    names_value = [(20, "pink"), (45, "red"), (70, "amber"), (100, "yellow"), (135, "yellow-green"),
             (170, "green"), (215, "blue-green"), (260, "blue"), (300, "purple"), (340, 'lilac'), (361, "pink")]
    name_value = next(n for lim, n in names_value if ang < lim)
    if name_value == "amber" and l < 45:
        name_value = "brown"
    tone_value = "light" if l > 70 else ("dark" if l < 35 else "")
    return f'{name_value} {tone_value}'.strip()


def image_palette(frames_value: list[tuple[float, Path]]) -> dict:
    if cv2 is None or not frames_value:
        return {}
    samples_value = []
    brightness = []
    satur = []
    for _, file_value in frames_value:
        img = cv2.imread(str(file_value))
        if img is None:
            continue
        small_value = cv2.resize(img, (48, int(48 * img.shape[0] / img.shape[1])), interpolation=cv2.INTER_AREA)
        samples_value.append(small_value.reshape(-1, 3))
        hsv = cv2.cvtColor(small_value, cv2.COLOR_BGR2HSV)
        brightness.append(float(hsv[..., 2].mean()) / 255.0)
        satur.append(float(hsv[..., 1].mean()) / 255.0)
    if not samples_value:
        return {}
    px = np.concatenate(samples_value)
    lab = _lab(px)
    rot, centers = _kmeans(lab, 10)
    items_value = [{"lab": centers[i], "weight": float((rot == i).mean())} for i in range(len(centers))]
    groups_value = _join_colors(items_value, 12.0)
    dominant = [{"hex": _lab_hex(g["lab"]), "fraction": round(g["weight"], 3), "name": describe_color(g["lab"]),
                   "lightness": round(float(g["lab"][0]), 1), "chroma": round(float(np.hypot(g["lab"][1], g["lab"][2])), 1)}
                  for g in groups_value[:7]]
    # Rare saturated colors are candidates for the accent color.
    rot2, scene2 = _kmeans(lab[np.hypot(lab[:, 1], lab[:, 2]) > 30], 6) if (np.hypot(lab[:, 1], lab[:, 2]) > 30).sum() > 50\
        else (np.array([]), np.zeros((0, 3)))
    accent_value = []
    if len(scene2):
        items2 = [{"lab": scene2[i], "weight": float((rot2 == i).mean())} for i in range(len(scene2))]
        tot_sat = float((np.hypot(lab[:, 1], lab[:, 2]) > 30).mean())
        for g in _join_colors(items2, 15.0)[:4]:
            accent_value.append({"hex": _lab_hex(g["lab"]), "name": describe_color(g["lab"]),
                             'image_fraction': round(g["weight"] * tot_sat, 4)})
    a_med = float(lab[:, 1].mean())
    b_med = float(lab[:, 2].mean())
    return {
        "dominant": dominant,
        "saturated": accent_value,
        "mean_brightness": round(float(np.mean(brightness)), 3),
        "mean_saturation": round(float(np.mean(satur)), 3),
        'dark_frames_pct': round(float(np.mean([b < 0.30 for b in brightness])), 3),
        'bright_frames_pct': round(float(np.mean([b > 0.75 for b in brightness])), 3),
        "temperature": "warm" if b_med > 6 or a_med > 6 else ("cool" if b_med < -4 else "neutral"),
    }


def colors_of_text(img, box_value: tuple[int, int, int, int]) -> dict | None:
    """Measure text fill, outline, and background colors in a BGR crop."""
    x0, y0, x1, y1 = box_value
    rec = img[y0:y1, x0:x1]
    if rec.size == 0 or rec.shape[0] < 8 or rec.shape[1] < 8:
        return None
    lab = _lab(rec.reshape(-1, 3)).reshape(rec.shape[0], rec.shape[1], 3)
    ring_value = np.concatenate([lab[:2].reshape(-1, 3), lab[-2:].reshape(-1, 3), lab[:, :2].reshape(-1, 3),
                           lab[:, -2:].reshape(-1, 3)])
    background_value = np.median(ring_value, axis=0)
    solid_background = float(ring_value[:, 0].std()) < 7.0
    all_value = lab.reshape(-1, 3)
    dist = np.linalg.norm(all_value - background_value, axis=1)
    ink_value = all_value[dist > 28]
    if len(ink_value) < 0.03 * len(all_value) or len(ink_value) < 30:
        return None
    rot, centers = _kmeans(ink_value, 3)
    d_ink = dist[dist > 28]
    groups_value = []
    for i in range(len(centers)):
        sel = rot == i
        fr = float(sel.mean())
        if not sel.any():
            continue
        px, dd = ink_value[sel], d_ink[sel]
        center_value = px[dd >= np.median(dd)].mean(0)   # Measure the letter interior without the outline blended into the background.
        groups_value.append({"lab": center_value, "fraction": fr, "lightness": float(center_value[0]), "chroma": float(np.hypot(center_value[1], center_value[2]))})
    dark_background = float(background_value[0]) < 30
    outline_value = None
    if not dark_background:
        dark_pixels = [g for g in groups_value if g["lightness"] < 25 and g["fraction"] >= 0.12]
        if dark_pixels:
            outline_value = max(dark_pixels, key=lambda g: g["fraction"])
    fill = [g for g in groups_value if g is not outline_value and g["fraction"] >= 0.10 and (g["lightness"] > 50 or g["chroma"] > 35)]
    if not fill:
        fill = [max(groups_value, key=lambda g: float(np.linalg.norm(g["lab"] - background_value)))]
    fill.sort(key=lambda g: -g["fraction"])
    two = len(fill) >= 2 and float(np.linalg.norm(fill[0]["lab"] - fill[1]["lab"])) > 30
    return {
        "fill": [{"lab": g["lab"], "hex": _lab_hex(g["lab"]), "fraction": round(g["fraction"], 3)} for g in fill[:2]],
        "dark_outline": outline_value is not None,
        "background_hex": _lab_hex(background_value),
        "solid_background": solid_background,
        'two_colors': two,
    }


def roles_of_colors(samples_color: list[dict]) -> dict:
    'Separate text color, current-word accent, and muted future-word color.'
    items_value = [{"lab": p["lab"], "weight": 1.0, "n": 1} for c in samples_color for p in c["fill"][:2]]
    groups_value = [g for g in _join_colors(items_value, 16.0) if g["weight"] >= max(2, 0.06 * len(samples_color))]
    if not groups_value:
        return {}

    def chroma(g):
        return float(np.hypot(g["lab"][1], g["lab"][2]))
    bright_neutral = [g for g in groups_value if chroma(g) < 22 and g["lab"][0] >= 70]
    colored = [g for g in groups_value if chroma(g) >= 25]
    erased = [g for g in groups_value if chroma(g) < 22 and 35 <= g["lab"][0] < 70]
    out: dict = {}
    if bright_neutral:
        text_value = max(bright_neutral, key=lambda g: g["weight"])
        accent_value = max(colored, key=lambda g: g["weight"]) if colored else None
    else:
        text_value = max(groups_value, key=lambda g: g["weight"])
        remainder = [g for g in colored if float(np.linalg.norm(g["lab"] - text_value["lab"])) > 30]
        accent_value = max(remainder, key=lambda g: g["weight"]) if remainder else None
    out['text_color'] = _lab_hex(text_value["lab"])
    if accent_value is not None:
        out['accent_color'] = _lab_hex(accent_value["lab"])
        out['two_colors_pct'] = round(min(1.0, accent_value["weight"] / len(samples_color)), 3)
    if erased:
        g = max(erased, key=lambda g: g["weight"])
        if g["weight"] >= 0.1 * len(samples_color):
            out['muted_color'] = _lab_hex(g["lab"])
    out['recognized_colors'] = [{"hex": _lab_hex(g["lab"]), "lines": int(g["weight"])} for g in groups_value[:5]]
    return out


# ----------------------------------------------------------------------------- 6. on-screen text

def read_text_in_screen(video: Path, engine: Path, measurements: Path, transcript_value: Path | None, fps: float) -> dict | None:
    script = engine / "helpers" / "screen_text.py"
    if not script.exists():
        notice(f'could not find {script}; continuing without on-screen text recognition')
        return None
    if transcript_value is None:
        # Supply a placeholder word when no transcript is available to satisfy the OCR helper contract.
        transcript_value = measurements / "_no-speech.json"
        save_json_value(transcript_value, {"words": [{"word": "zzzzzz", "start": -100.0, "end": -99.0}]})
    cmd = engine_python(engine) + [str(script), "--video", str(video), "--edit-dir", str(measurements), "--fps", str(fps),
                           "--force", "--transcript", str(transcript_value)]
    r = subprocess.run(cmd, capture_output=True, cwd=engine)
    file_value = measurements / "screen_text.json"
    if r.returncode != 0 or not file_value.exists():
        notice("On-screen text recognition failed: " + r.stderr.decode("utf-8", "replace").strip()[-300:])
        return None
    return json.loads(file_value.read_text(encoding="utf-8"))


def _is_word(txt: str) -> bool:
    letters = sum(ch.isalpha() for ch in txt)
    return letters >= 2 and letters >= 0.5 * max(1, len(txt.replace(" ", "")))


def analyze_texts(doc: dict, words_value: list[dict] | None, W: int, H: int) -> dict:
    samples_value = doc.get('samples', [])
    step_value = 1.0 / float(doc.get("fps") or 2.0)
    read_words = doc.get("backend") == "vision"
    # Link matching text at nearby positions across adjacent samples.
    points: list[tuple[int, int]] = [(i, j) for i, sample in enumerate(samples_value) for j in range(len(sample["items"]))]
    parent = {n: n for n in points}

    def root(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    keys_value = {(i, j): normal_txt(samples_value[i]["items"][j].get("text", "")) for i, j in points}
    for i, sample in enumerate(samples_value):
        for j, it in enumerate(sample["items"]):
            k = keys_value[(i, j)]
            if len(k) < 2:
                continue
            for di in (1, 2):
                if i + di >= len(samples_value):
                    break
                for j2, it2 in enumerate(samples_value[i + di]["items"]):
                    if keys_value[(i + di, j2)] == k and abs(it2["center_y"] - it["center_y"]) < 0.02\
                            and abs(it2["center_x"] - it["center_x"]) < 0.08:
                        parent[root((i + di, j2))] = root((i, j))
    track_comp: dict = defaultdict(list)
    for n in points:
        track_comp[root(n)].append(n[0])
    dur_in = {n: (max(track_comp[root(n)]) - min(track_comp[root(n)]) + 1) * step_value for n in points}
    spoken = []
    if words_value:
        for w in words_value:
            nt = normal_txt(str(w.get("text", w.get("word", ""))))
            if len(nt) >= 2:
                spoken.append((float(w.get("start", 0)), nt))

    def matches_with_speech(t: float, txt: str) -> bool:
        written = {p for p in normal_txt(txt).split() if len(p) >= 2}
        if not written:
            return False
        spoken_words = {p for (ts, p) in spoken if t - 1.5 <= ts <= t + 1.5}
        return bool(written & spoken_words)

    items_value: list[dict] = []
    for i, sample in enumerate(samples_value):
        for j, it in enumerate(sample["items"]):
            txt = str(it.get("text", "")).strip()
            dur_s = dur_in[(i, j)]
            e = dict(it)
            e["t"] = float(sample["t"])
            e['sample'] = i
            e["dur_s"] = round(dur_s, 2)
            e['speech'] = matches_with_speech(e["t"], txt) if spoken else None
            # Static text stays in place and does not transcribe speech; slow captions can also remain for several seconds.
            if spoken:
                fixed = (dur_s >= 3.0 and not e['speech']) or dur_s >= 10.0
            else:
                fixed = dur_s >= 6.0
            e["fixed"] = bool(read_words and len(keys_value[(i, j)]) >= 2 and fixed)
            e['word'] = _is_word(txt) if read_words else True
            items_value.append(e)

    # Caption candidates change over time, contain letters, and sit near the horizontal center.
    cand = [e for e in items_value if not e["fixed"] and e['word'] and 0.2 <= e["center_x"] <= 0.8]
    used_speech = False
    if spoken:
        cand_speech = [e for e in cand if e['speech']]
        if len(cand_speech) >= 3:
            cand = cand_speech
            used_speech = True
    if not used_speech and read_words:
        cand = [e for e in cand if e["height"] >= 0.015]   # Without speech, small HUD text and labels do not count as captions.
    res: dict = {"backend": doc.get("backend"), 'samples': len(samples_value), "reading_fps": doc.get("fps"),
                 'has_captions': False, 'matched_speech': used_speech}
    screen_w, screen_h = screen_default(W, H)
    res["standard_canvas"] = f'{screen_w}x{screen_h}'
    # Group by screen height because captions may move between sections.
    groups_y: list[dict] = []

    def clear_switch(bl: list[dict]) -> bool:
        'Without speech to compare, changing text is treated as captions; signs, brands, and products repeat.'
        if used_speech or not read_words:
            return True
        samples = sorted({e['sample'] for e in bl})
        if len(samples) < 4:
            return False
        per_value = {i: " ".join(sorted(normal_txt(e["text"]) for e in bl if e['sample'] == i)) for i in samples}
        ts = [e["t"] for e in bl]
        return len(set(per_value.values())) / len(samples) >= 0.15 and max(ts) - min(ts) >= 4.0

    if cand:
        # The main caption height has the most samples within a narrow vertical window.
        centers_c = sorted({round(e["center_y"] / 0.005) * 0.005 for e in cand})
        best_value, best_n = None, 0
        for yc in centers_c:
            bl = [e for e in cand if abs(e["center_y"] - yc) <= 0.012]
            n = len({e['sample'] for e in bl})
            if n > best_n and clear_switch(bl):
                best_value, best_n = yc, n
        if best_value is not None:
            primary_bl = [e for e in cand if abs(e["center_y"] - best_value) <= 0.015]
            h_ref = float(np.median([e["height"] for e in primary_bl]))
            # Neighboring lines of a multiline caption belong to the same main block.
            ids_p = {id(e) for e in primary_bl}
            per_am: dict[int, list[dict]] = defaultdict(list)
            for e in primary_bl:
                per_am[e['sample']].append(e)
            neighbors = [e for e in cand if id(e) not in ids_p and e['sample'] in per_am
                        and any(0.5 * h_ref < abs(e["center_y"] - q["center_y"]) <= 2.2 * h_ref
                                for q in per_am[e['sample']])]
            primary_bl = primary_bl + neighbors
            used_value = {id(e) for e in primary_bl}
            centers_value = []
            for i_am in per_am:
                ys_am = [e["center_y"] for e in primary_bl if e['sample'] == i_am]
                centers_value.append((min(ys_am) + max(ys_am)) / 2)
            groups_y.append({"members": primary_bl, 'samples': sorted({e['sample'] for e in primary_bl}),
                             "center_y": float(np.median(centers_value))})
            # Another position counts only as a continuous time block with matching letter size.
            remainder = sorted([e for e in cand if id(e) not in used_value], key=lambda e: e["center_y"])
            blocks_value: list[list[dict]] = []
            for e in remainder:
                if blocks_value and e["center_y"] - blocks_value[-1][-1]["center_y"] <= 0.015:
                    blocks_value[-1].append(e)
                else:
                    blocks_value.append([e])
            for bl in blocks_value:
                bl = sorted(bl, key=lambda e: e["t"])
                runs_value: list[list[dict]] = [[bl[0]]]
                for e in bl[1:]:
                    if e["t"] - runs_value[-1][-1]["t"] > 1.5:
                        runs_value.append([e])
                    else:
                        runs_value[-1].append(e)
                for co in runs_value:
                    sample_ids = sorted({e['sample'] for e in co})
                    dur_co = co[-1]["t"] - co[0]["t"] + step_value
                    h_co = float(np.median([e["height"] for e in co]))
                    c_co = float(np.median([e["center_y"] for e in co]))
                    if abs(c_co - groups_y[0]["center_y"]) <= 1.5 * h_ref:
                        # Text adjacent to the main caption is another line of that caption.
                        groups_y[0]["members"] = groups_y[0]["members"] + co
                        groups_y[0]['samples'] = sorted(set(groups_y[0]['samples']) | set(sample_ids))
                    elif len(sample_ids) >= 4 and dur_co >= 2.5 and 0.7 * h_ref <= h_co <= 1.4 * h_ref and clear_switch(co):
                        groups_y.append({"members": co, 'samples': sample_ids, "center_y": c_co})
    caption = [e for g in groups_y for e in g["members"]]
    samples_with = sorted({e['sample'] for e in caption})
    # Captions cover much of the speech; a brief hook title repeating speech does not qualify.
    if used_speech:
        ts_f = [ts for ts, _ in spoken]
        base_am = [i for i, sample in enumerate(samples_value) if any(sample["t"] - 0.5 <= x <= sample["t"] + 0.5 for x in ts_f)]
        coverage_value = len(set(samples_with) & set(base_am)) / max(1, len(base_am))
        minimum_ok = coverage_value >= 0.20
    else:
        minimum_ok = len(samples_with) >= 0.15 * len(samples_value)
    if groups_y and len(samples_with) >= 3 and minimum_ok:
        res['has_captions'] = True
        track_c = groups_y[0]["center_y"]
        primary = groups_y[0]["members"]
        heights = np.array([e["height"] for e in primary])
        word_count = [len(re.findall(r"\S+", e["text"])) for e in primary if e.get("text")]
        lines_value: dict[int, int] = {}
        for i_am in {e['sample'] for e in primary}:
            ys_am = sorted(e["center_y"] for e in primary if e['sample'] == i_am)
            line_count = 1
            for y0, y1 in zip(ys_am, ys_am[1:]):
                if y1 - y0 > 0.5 * float(np.median(heights)):
                    line_count += 1
            lines_value[i_am] = line_count
        letters = "".join(e.get("text", "") for e in primary)
        alpha_value = [c for c in letters if c.isalpha()]
        uppercase = sum(c.isupper() for c in alpha_value) / len(alpha_value) if alpha_value else None
        # Letter width relative to line height distinguishes narrow and wide fonts.
        prop = []
        for e in primary:
            nch = len(e.get("text", "").replace(" ", ""))
            if nch >= 3 and e["height"] > 0:
                prop.append((e["width"] * W / nch) / (e["height"] * H))
        seq = []
        for i in range(len(samples_value)):
            seq.append(" ".join(sorted(normal_txt(e["text"]) for e in caption if e['sample'] == i)))
        switches = sum(1 for a0, b0 in zip(seq, seq[1:]) if a0 and b0 and a0 != b0)
        minutes_with = len(samples_with) * step_value / 60.0
        with_speech = None
        if spoken:
            ts_speech = [ts for ts, _ in spoken]
            speech_samples = [i for i, sample in enumerate(samples_value)
                       if any(sample["t"] - 0.5 <= ts <= sample["t"] + 0.5 for ts in ts_speech)]
            if speech_samples:
                with_speech = round(sum(1 for i in speech_samples if i in set(samples_with)) / len(speech_samples), 3)
        positions = []
        for g in groups_y:
            ts = [samples_value[i]["t"] for i in g['samples']]
            positions.append({"center_y": round(g["center_y"], 4), "standard_center_y": int(round(g["center_y"] * screen_h)),
                             'samples': len(g['samples']),
                             'caption_part': round(len(g['samples']) / max(1, len(samples_with)), 3),
                             "start": round(min(ts), 2), "end": round(max(ts) + step_value, 2)})
        res.update({
            "center_y": round(track_c, 4),
            "center_y_px": int(round(track_c * H)),
            "standard_center_y": int(round(track_c * screen_h)),
            "positions": positions,
            'line_height_fraction': round(float(np.median(heights)), 4),
            'line_height_px': int(round(float(np.median(heights)) * H)),
            'standard_line_height': int(round(float(np.median(heights)) * screen_h)),
            'median_words_per_line': float(np.median(word_count)) if word_count else None,
            'max_words_per_line': int(max(word_count)) if word_count else None,
            'words_per_line_histogram': dict(sorted(Counter(word_count).items())) if word_count else {},
            'lines_per_group': float(np.median(list(lines_value.values()))) if lines_value else None,
            'lines_per_group_pct': {str(k): round(v / len(lines_value), 3) for k, v in sorted(Counter(lines_value.values()).items())}
            if lines_value else {},
            "uppercase_pct": round(uppercase, 3) if uppercase is not None else None,
            'letter_width_height_ratio': round(float(np.median(prop)), 3) if prop else None,
            'captioned_time_pct': round(len(samples_with) / max(1, len(samples_value)), 3),
            'captioned_speech_pct': with_speech,
            'sampled_changes_per_minute': round(switches / minutes_with, 1) if minutes_with > 0 else None,
            "median_center_x": round(float(np.median([e["center_x"] for e in primary])), 3),
            'matched_speech': bool(spoken) and all(e['speech'] for e in caption),
        })
    # Static titles, labels, handles, and watermarks.
    fixed_value: dict[tuple, dict] = {}
    for e in items_value:
        if not e["fixed"]:
            continue
        k = (normal_txt(e["text"]), round(e["center_y"] / 0.04))
        f = fixed_value.setdefault(k, {"text": e["text"], "start": e["t"], "end": e["t"], "center_y": e["center_y"],
                                 'height_fraction': e["height"], "center_x": e["center_x"]})
        f["end"] = max(f["end"], e["t"])
        f["start"] = min(f["start"], e["t"])
    listing_fixed = []
    for f in sorted(fixed_value.values(), key=lambda f: f["start"]):
        t = f["text"]
        mark_value = bool(re.search(r"@|www\.|\.com|\.format_number|https?:", t, re.I))
        listing_fixed.append({**f, "end": round(f["end"] + step_value, 2), 'duration_s': round(f["end"] + step_value - f["start"], 2),
                            'height_px': int(round(f['height_fraction'] * H)),
                            "words": len(t.split()), "looks_like_brand_or_handle": mark_value,
                            "zone": "top" if f["center_y"] < 0.3 else ("bottom" if f["center_y"] > 0.7 else "middle")})
    res["static_texts"] = listing_fixed
    res["with_text_pct"] = round(sum(1 for sample in samples_value if sample["items"]) / max(1, len(samples_value)), 3)
    res['bright_screen_intervals'] = doc.get("fullscreen", [])
    # Hook text in the first three seconds
    hook_value = [e for e in items_value if e["t"] < 3.0 and e['word']]
    large = [e for e in hook_value if res.get('line_height_fraction') and e["height"] > 1.3 * res['line_height_fraction']]
    res['hook'] = {
        "text_in_first_3s": bool(hook_value),
        'larger_than_captions': bool(large),
        'max_height_px': int(round(max((e["height"] for e in hook_value), default=0) * H)),
        "zone": (lambda y: "top" if y < 0.3 else ("bottom" if y > 0.7 else "middle"))(
            float(np.median([e["center_y"] for e in (large or hook_value)]))) if hook_value else None,
    }
    res["_items"] = items_value
    res['_caption'] = caption
    res["_primary"] = groups_y[0]["members"] if res.get('has_captions') else []
    return res


# ----------------------------------------------------------------------------- 7. audio

def decode_audio(video: Path) -> np.ndarray | None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-vn", "-ac", "2", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True)
    x = np.frombuffer(r.stdout, np.float32)
    if x.size < SR:
        return None
    return x[: x.size // 2 * 2].reshape(-1, 2)


def measure_loudness(video: Path) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vn", "-af",
                        "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True)
    txt = r.stderr.decode("utf-8", "replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", txt, re.S)
    if not m:
        return {}
    d = json.loads(m.group(0))

    def f(v):
        try:
            x = float(v)
            return round(x, 1) if math.isfinite(x) else None
        except (TypeError, ValueError):
            return None
    return {"integrated_lufs": f(d.get("input_i")), 'true_peak_dbtp': f(d.get("input_tp")),
            "loudness_range_lu": f(d.get("input_lra"))}


def spectrum(signal: np.ndarray, accumulate_value: np.ndarray | None = None) -> dict:
    n = 1 + max(0, (len(signal) - WINDOW) // STEP)
    win_value = np.hanning(WINDOW).astype(np.float32)
    freqs = np.fft.rfftfreq(WINDOW, 1.0 / SR)
    sel = {b: (freqs >= a) & (freqs < z) for b, (a, z) in BANDS.items()}
    E = {b: np.empty(n, np.float32) for b in BANDS}
    rms = np.empty(n, np.float32)
    flux = np.zeros(n, np.float32)
    sum_value = np.zeros(len(freqs), np.float64)
    n_sum = 0
    previous = None
    for c0 in range(0, n, 2048):
        c1 = min(n, c0 + 2048)
        idx = np.arange(c0, c1)[:, None] * STEP + np.arange(WINDOW)[None, :]
        fr = signal[idx]
        rms[c0:c1] = np.sqrt(np.mean(fr ** 2, axis=1))
        mag = np.abs(np.fft.rfft(fr * win_value, axis=1)).astype(np.float32)
        p = mag ** 2
        for b, s in sel.items():
            E[b][c0:c1] = 10 * np.log10(p[:, s].sum(1) + 1e-10)
        lm = np.log1p(mag)
        prev = np.vstack([(previous if previous is not None else lm[0])[None, :], lm[:-1]])
        flux[c0:c1] = np.maximum(0.0, lm - prev).sum(1)
        previous = lm[-1]
        if accumulate_value is not None:
            m = accumulate_value[c0:c1]
            if m.any():
                sum_value += p[m].sum(0)
                n_sum += int(m.sum())
    return {"n": n, "t": (np.arange(n) * STEP + WINDOW / 2) / SR, "db": 20 * np.log10(rms + 1e-9),
            "bands": E, "flux": flux, "freqs": freqs, "mean_spectrum": sum_value / n_sum if n_sum else None}


def median_moving(x: np.ndarray, window_value: int) -> np.ndarray:
    dec = 4
    xs = x[::dec]
    k = max(1, window_value // (2 * dec))
    xp = np.pad(xs, k, mode="edge")
    v = np.lib.stride_tricks.sliding_window_view(xp, 2 * k + 1)
    med = np.median(v, axis=1)
    return np.interp(np.arange(len(x)), np.arange(len(xs)) * dec, med).astype(np.float32)


def peaks_value(x: np.ndarray, threshold_value: float, dist: int) -> list[int]:
    if len(x) < 3:
        return []
    viz = np.maximum(np.roll(x, 1), np.roll(x, -1))
    cand = np.where((x >= threshold_value) & (x >= viz))[0]
    cand = cand[np.argsort(-x[cand])]
    free_value = np.ones(len(x), bool)
    selected = []
    for i in cand:
        if free_value[i]:
            selected.append(int(i))
            free_value[max(0, i - dist):i + dist + 1] = False
    return sorted(selected)


def _speech_mask(times_value: np.ndarray, words_value: list[dict], padding: float = 0.05) -> np.ndarray:
    m = np.zeros(len(times_value), bool)
    for w in words_value:
        a, z = float(w.get("start", 0)) - padding, float(w.get("end", w.get("start", 0))) + padding
        i0, i1 = np.searchsorted(times_value, [a, z])
        m[i0:i1] = True
    return m


def _pauses(words_value: list[dict], dur: float, minimum_value: float = 0.3) -> list[tuple[float, float]]:
    ps = sorted(words_value, key=lambda w: float(w.get("start", 0)))
    out = []
    for a, b in zip(ps, ps[1:]):
        g0, g1 = float(a.get("end", a.get("start", 0))), float(b.get("start", 0))
        if g1 - g0 >= minimum_value:
            out.append((g0, g1))
    return out


def _bpm(env: np.ndarray, fr: float) -> tuple[float | None, float]:
    if len(env) < fr * 8:
        return None, 0.0
    e = (env - env.mean()).astype(np.float64)
    nfft = 1 << int(math.ceil(math.log2(2 * len(e))))
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, nfft)) ** 2, nfft)[:len(e)]
    if ac[0] <= 0:
        return None, 0.0
    lo, hi = int(fr * 60 / 180), int(fr * 60 / 70)
    lag = lo + int(np.argmax(ac[lo:hi]))
    return round(60.0 * fr / lag, 1), round(float(ac[lag] / ac[0]), 2)


def tonal_peaks(esp: np.ndarray | None, freqs: np.ndarray) -> int:
    if esp is None:
        return 0
    sel = (freqs >= 80) & (freqs <= 2500)
    db = 10 * np.log10(esp[sel] + 1e-12)
    base = np.array([np.median(db[max(0, i - 12):i + 13]) for i in range(len(db))])
    exc = db - base
    return len(peaks_value(exc, 8.0, 3))


def rms_frames_value(signal: np.ndarray) -> np.ndarray:
    'RMS level in dBFS over sliding sample windows, without FFT.'
    n = 1 + max(0, (len(signal) - WINDOW) // STEP)
    cs = np.concatenate([[0.0], np.cumsum(signal.astype(np.float64) ** 2)])
    i0 = np.arange(n) * STEP
    energy = (cs[i0 + WINDOW] - cs[i0]) / WINDOW
    return (10 * np.log10(np.maximum(energy, 0) + 1e-18)).astype(np.float32)


def analyze_audio(video: Path, dur: float, words_value: list[dict] | None, cuts_t: list[float]) -> dict:
    x = decode_audio(video)
    if x is None:
        return {"has_audio": False}
    res: dict = {"has_audio": True}
    res["loudness"] = measure_loudness(video)
    L, R = x[:, 0].astype(np.float32), x[:, 1].astype(np.float32)
    M = (L + R) / 2
    S = (L - R) / 2
    rms_m = float(np.sqrt(np.mean(M ** 2)) + 1e-12)
    rms_s = float(np.sqrt(np.mean(S ** 2)) + 1e-12)
    lc, rc = L - L.mean(), R - R.mean()
    den = math.sqrt(float(np.dot(lc, lc)) * float(np.dot(rc, rc)))
    corr = float(np.dot(lc, rc)) / den if den > 0 else 1.0
    del lc, rc, x
    stereo = 20 * math.log10(rms_s / rms_m) > -30 and corr < 0.995
    res['left_right_correlation'] = round(corr, 4)
    res["side_minus_mid_db"] = round(20 * math.log10(rms_s / rms_m), 1)
    res["stereo"] = bool(stereo)
    fr = SR / STEP

    mdb = rms_frames_value(M)
    n_fr = len(mdb)
    t = (np.arange(n_fr) * STEP + WINDOW / 2) / SR
    speech_value = _speech_mask(t, words_value) if words_value else None
    pause_value = None
    if words_value:
        pause_value = np.zeros(n_fr, bool)
        for g0, g1 in _pauses(words_value, dur, 0.3):
            i0, i1 = np.searchsorted(t, [g0 + 0.12, g1 - 0.12])
            pause_value[i0:i1] = True
        if pause_value.sum() < 0.5 * fr:
            pause_value = None
    if speech_value is not None and speech_value.any():
        voice_db = float(np.median(mdb[speech_value]))
        method_value = 'speech inferred from timed transcript words'
    else:
        voice_db = float(np.percentile(mdb, 80))
        method_value = 'speech inferred from the loudest audio intervals'
    floor_reliable = pause_value is not None
    if pause_value is not None:
        floor_db_value = float(np.median(mdb[pause_value]))
        quiet_value = pause_value
        method_value += ", background from speech pauses"
    else:
        method_value += (', background estimated from quiet intervals because speech rarely pauses' if words_value
                   else ", background from the quietest intervals")
        active_value = mdb > voice_db - 40
        floor_db_value = float(np.percentile(mdb[active_value], 10)) if active_value.any() else float(np.percentile(mdb, 10))
        quiet_value = active_value & (mdb <= np.percentile(mdb[active_value], 15)) if active_value.any() else None
    at_value = spectrum(M, accumulate_value=quiet_value)
    dif = voice_db - floor_db_value
    tone_count = tonal_peaks(at_value["mean_spectrum"], at_value["freqs"])
    music_value: dict = {'voice_dbfs': round(voice_db, 1), "between_phrase_floor_dbfs": round(floor_db_value, 1),
                    "difference_db": round(dif, 1), 'floor_tonal_peaks': tone_count, "method": method_value}

    # The side channel (left minus right) suppresses centered speech while retaining stereo music.
    sdb = None
    speech_intervals = None
    voice_centered = False
    if stereo:
        sdb = rms_frames_value(S)
        if speech_value is not None and speech_value.any():
            voice_centered = float(np.median(mdb[speech_value]) - np.median(sdb[speech_value])) >= 15.0
        else:
            voice_centered = res["side_minus_mid_db"] <= -15.0
        music_value["median_side_dbfs"] = round(float(np.median(sdb)), 1)
        if speech_value is not None and pause_value is not None:
            s_speech = float(np.median(sdb[speech_value]))
            s_pause = float(np.median(sdb[pause_value]))
            music_value['side_during_speech_dbfs'] = round(s_speech, 1)
            music_value['side_during_pauses_dbfs'] = round(s_pause, 1)
            if voice_centered:
                music_value['ducks_during_speech'] = bool(s_pause - s_speech > 3.0)
        speech_intervals = spectrum(S)
        env = np.maximum(speech_intervals["flux"] - median_moving(speech_intervals["flux"], int(fr) | 1), 0)
        bpm, strength = _bpm(env, fr)
        music_value["bpm"] = bpm if strength >= 0.25 else None
        music_value["bpm_strength"] = strength
        seg = []
        for s in range(int(dur)):
            sel = (t >= s) & (t < s + 1)
            seg.append(float(np.median(sdb[sel])) if sel.any() else -120.0)
        seg = np.array(seg)
        if len(seg) >= 6 and voice_centered:
            ref = float(np.median(seg[:-3]))
            end_music = None
            for s in range(len(seg) - 1, -1, -1):
                if seg[s] > ref - 10:
                    end_music = s + 1
                    break
            if end_music is not None and dur - end_music >= 1.0:
                music_value['disappears_before_end_s'] = round(dur - end_music, 1)
    music_value['centered_voice'] = voice_centered
    side_continuous = bool(voice_centered and sdb is not None and
                         music_value.get('side_during_pauses_dbfs', music_value.get("median_side_dbfs", -99)) > -58)
    if dif >= 35 or floor_db_value < -60:
        music_value["verdict"] = 'no music'
        music_value["explanation"] = 'near silence between phrases'
    elif dif < 8:
        music_value["verdict"] = 'loud continuous sound'
        music_value["explanation"] = 'background is near voice level: loud music or little speech'
    elif tone_count >= 3 or side_continuous:
        music_value["verdict"] = 'probable music'
        music_value["explanation"] = f'continuous musical sound approximately {format_number(dif, 0)} dB below speech'
    elif not floor_reliable:
        music_value["verdict"] = "uncertain"
        music_value["explanation"] = ("speech has too few pauses for a reliable background measurement; listen to the reference or use the user's description")
    else:
        music_value["verdict"] = 'probable background noise'
        music_value["explanation"] = f'continuous sound approximately {format_number(dif, 0)} dB below speech, but unlikely to be music'
    music_value["reliable_floor"] = floor_reliable
    res["music"] = music_value

    # Sound attacks, including syllables.
    flx = at_value["flux"]
    base = median_moving(flx, int(fr) | 1)
    mad = median_moving(np.abs(flx - base), int(fr) | 1) + 1e-6
    at = peaks_value((flx - base) / mad, 4.0, int(0.1 * fr))
    res['attacks_per_minute'] = round(len(at) / (dur / 60.0), 1) if dur else None

    # Likely sound effects.
    # Calibration: a side-channel rise of 6 dB above the surrounding four-second median
    # identified 32 of 44 manually annotated effects in two reference clips, with few false positives.
    events_value: list[dict] = []
    if sdb is not None and voice_centered:
        exc = sdb - median_moving(sdb, int(4 * fr) | 1)
        for i in peaks_value(exc, 6.0, int(0.3 * fr)):
            events_value.append({"t": float(t[i]), "strength_db": round(float(exc[i]), 1), 'source': "side"})
    else:
        sub = at_value["bands"]["sub"]
        exs = sub - median_moving(sub, int(1.5 * fr) | 1)
        voice_b = at_value["bands"]["voice"]
        exv = voice_b - median_moving(voice_b, int(1.5 * fr) | 1)
        sc = exs - np.maximum(exv, 0)
        p95 = float(np.percentile(sub, 95))
        for i in peaks_value(sc, 18.0, int(0.3 * fr)):
            if mdb[i] > voice_db - 25 and sub[i] >= p95:
                events_value.append({"t": float(t[i]), "strength_db": round(float(sc[i]), 1), 'source': "bass"})
    if pause_value is not None:
        exc_p = mdb - (floor_db_value + 10)
        exc_p[~pause_value] = -99
        for i in peaks_value(exc_p, 0.0, int(0.25 * fr)):
            events_value.append({"t": float(t[i]), "strength_db": round(float(mdb[i] - floor_db_value), 1), 'source': "pause"})
    events_value.sort(key=lambda e: e["t"])
    together: list[dict] = []
    for e in events_value:
        if together and e["t"] - together[-1]["t"] < 0.25:
            if e["strength_db"] > together[-1]["strength_db"]:
                e['source'] = together[-1]['source'] + "+" + e['source']
                together[-1] = e
            else:
                together[-1]['source'] += "+" + e['source']
        else:
            together.append(e)
    font_band = speech_intervals["bands"] if (speech_intervals is not None and voice_centered) else at_value["bands"]
    for e in together:
        i = min(max(int(np.searchsorted(t, e["t"])), 0), len(t) - 1)
        rel = {b: float(font_band[b][i] - np.median(font_band[b][max(0, i - int(fr)):i + int(fr)]))
               for b in BANDS}
        low_value = max(rel["sub"], rel["bass"])
        if low_value >= max(rel["treble"], rel['body']) and low_value > 6:
            e["type"] = "bass: impact or beat"
        elif rel["treble"] > rel['body'] + 3:
            e["type"] = "treble: snap, sparkle, or click"
        else:
            e["type"] = 'broadband: whoosh, transition, or pop'
        e["t"] = round(e["t"], 2)
        e['near_cut'] = any(abs(e["t"] - c) <= 0.2 for c in cuts_t)
    res["effects"] = {
        "probable_count": len(together),
        "per_minute": round(len(together) / (dur / 60.0), 1) if dur else None,
        'near_cut_pct': round(sum(e['near_cut'] for e in together) / len(together), 3) if together else None,
        'cuts_with_sound_pct': round(sum(1 for c in cuts_t if any(abs(e["t"] - c) <= 0.2 for e in together))
                                    / len(cuts_t), 3) if cuts_t else None,
        "events": together,
        "confidence": 'medium' if voice_centered else "low",
        "method": ("side-channel peaks (left minus right, suppressing centered speech), "
                   "and sounds within speech pauses" if voice_centered else
                   'bass jumps unexplained by speech and sounds within pauses; less reliable without a clean side channel'),
    }
    per_seg = []
    for s in range(int(math.ceil(dur))):
        sel = (t >= s) & (t < s + 1)
        if not sel.any():
            per_seg.append(None)
            continue
        item = {"db": round(float(np.percentile(mdb[sel], 90)), 1)}
        if sdb is not None:
            item["side_db"] = round(float(np.median(sdb[sel])), 1)
        per_seg.append(item)
    res['_per_second'] = per_seg
    return res


# ----------------------------------------------------------------------------- 8. speech

def transcribe_value(video: Path, engine: Path, measurements: Path) -> Path | None:
    script = engine / "helpers" / "transcribe.py"
    if not script.exists():
        notice(f'could not find {script}; continuing without transcription.')
        return None
    cmd = engine_python(engine) + [str(script), str(video), "--edit-dir", str(measurements), "--language", "auto", "--no-prompt"]
    r = subprocess.run(cmd, capture_output=True, cwd=engine)
    file_value = measurements / "transcripts" / f'{video.stem}.json'
    if r.returncode != 0 or not file_value.exists():
        notice('transcription failed; continuing without it. ' + r.stderr.decode("utf-8", "replace").strip()[-300:])
        return None
    return file_value


def read_words(file_value: Path | None) -> list[dict] | None:
    if not file_value or not file_value.exists():
        return None
    d = json.loads(file_value.read_text(encoding="utf-8"))
    ws = d.get("words") if isinstance(d, dict) else d
    out = []
    for w in ws or []:
        txt = str(w.get("text", w.get("word", ""))).strip()
        if txt and w.get("start") is not None:
            out.append({"text": txt, "start": float(w["start"]), "end": float(w.get("end", w["start"]))})
    return sorted(out, key=lambda w: w["start"]) or None


def measure_speech(words_value: list[dict] | None, dur: float, language_value: str | None) -> dict | None:
    if not words_value:
        return None
    ini, end_value = words_value[0]["start"], words_value[-1]["end"]
    span = max(0.1, end_value - ini)
    pauses_value = _pauses(words_value, dur, 0.3)
    return {
        "language": language_value,
        "words": len(words_value),
        'words_per_minute': round(len(words_value) / (span / 60.0), 0),
        'first_word_s': round(ini, 2),
        'last_word_s': round(end_value, 2),
        'trailing_silence_s': round(max(0.0, dur - end_value), 2),
        'pauses_over_0_3s': len(pauses_value),
        'pauses_per_minute': round(len(pauses_value) / (span / 60.0), 1),
        'longest_pause_s': round(max((b - a for a, b in pauses_value), default=0.0), 2),
        'speech_share_pct': round(sum(w["end"] - w["start"] for w in words_value) / dur, 3) if dur else None,
    }


# ----------------------------------------------------------------------------- 9. verification images

def _label(d: ImageDraw.ImageDraw, xy, text_value: str, font_value, color_value=(255, 230, 0), background_value=(0, 0, 0)):
    x, y = xy
    box_value = d.textbbox((x + 4, y + 2), text_value, font=font_value)
    d.rectangle([x, y, box_value[2] + 4, box_value[3] + 3], fill=background_value)
    d.text((x + 4, y + 2), text_value, fill=color_value, font=font_value)


def assemble_sheets(frames_value: list[tuple[float, Path]], cuts_value: list[dict], sounds_value: list[dict], directory: Path,
                  W: int, H: int, font_value, font_p) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    vertical = H >= W
    tw = 160 if vertical else 256
    th = int(round(tw * H / W))
    cols, rows = (10, 6) if vertical else (8, 8)
    per_value = cols * rows
    outputs = []
    total = (len(frames_value) + per_value - 1) // per_value
    for f in range(total):
        part_value = frames_value[f * per_value:(f + 1) * per_value]
        top = 34
        sheet_value = Image.new("RGB", (cols * tw, top + th * ((len(part_value) + cols - 1) // cols)), (18, 18, 18))
        d = ImageDraw.Draw(sheet_value)
        d.text((8, 7), f'sheet {f + 1} of {total}   {mmss_short(part_value[0][0])} to {mmss_short(part_value[-1][0])}   red border = cut; amber = tentative cut; SOUND = probable effect',
               fill=(230, 230, 230), font=font_p)
        for j, (t, file_value) in enumerate(part_value):
            k = f * per_value + j
            t_ant = frames_value[k - 1][0] if k > 0 else -1.0
            im = Image.open(file_value).convert("RGB").resize((tw, th))
            dd = ImageDraw.Draw(im)
            inside_value = [c for c in cuts_value if t_ant < c["t"] <= t + 1e-6]
            if inside_value:
                strong_value = any(c["type"] == "cut" for c in inside_value)
                color_value = (235, 40, 40) if strong_value else (255, 150, 0)
                dd.rectangle([0, 0, tw - 1, th - 1], outline=color_value, width=5)
                _label(dd, (3, th - 24), "CUT " + ",".join(mmss(c["t"]) if c["t"] >= 60 else mmss(c["t"])[3:]
                                                               for c in inside_value[:2]), font_p,
                        color_value=(255, 255, 255), background_value=color_value)
            if any(t_ant < s["t"] <= t + 1e-6 for s in sounds_value):
                _label(dd, (tw - 46, 3), "SOUND", font_p, color_value=(255, 255, 255), background_value=(30, 90, 220))
            _label(dd, (3, 3), mmss_short(t) if abs(t - round(t)) < 0.05 else mmss(t)[:-1], font_value)
            sheet_value.paste(im, ((j % cols) * tw, top + (j // cols) * th))
        destination = directory / f'sheet_{f + 1:02d}.jpg'
        sheet_value.save(destination, quality=88)
        outputs.append(destination)
    return outputs


def assemble_audit(cuts_value: list[dict], pairs: dict[int, Path], output_base: Path, W: int, H: int,
                       font_p) -> list[Path]:
    if not cuts_value:
        return []
    total_cuts = len(cuts_value)

    def distributed(listing: list[dict], n: int) -> list[dict]:
        if len(listing) <= n:
            return listing
        step_value = len(listing) / float(n)
        return [listing[int(i * step_value)] for i in range(n)]
    if total_cuts > 120:   # For long videos, sample likely events first.
        prov = [c for c in cuts_value if c["type"] != "cut"]
        strong_cuts = [c for c in cuts_value if c["type"] == "cut"]
        choice_value = distributed(prov, 90)
        choice_value += distributed(strong_cuts, 120 - len(choice_value))
        cuts_value = sorted(choice_value, key=lambda c: c["t"])
    vertical = H >= W
    tw = 140 if vertical else 220
    th = int(round(tw * H / W))
    cols = 5 if vertical else 3
    per_value = cols * 6
    outputs = []
    total = (len(cuts_value) + per_value - 1) // per_value
    for f in range(total):
        part_value = cuts_value[f * per_value:(f + 1) * per_value]
        cell_w, cell_h = 2 * tw + 12, th + 26
        sheet_value = Image.new("RGB", (cols * cell_w, 30 + cell_h * ((len(part_value) + cols - 1) // cols)), (18, 18, 18))
        d = ImageDraw.Draw(sheet_value)
        header_value = ('Each pair shows the last frame before a cut and the first after it. Verify the cut visually.'
               + (f'  Sample of {len(cuts_value)} of {total_cuts} cuts.' if total_cuts > len(cuts_value) else ""))
        d.text((8, 7), header_value, fill=(230, 230, 230), font=font_p)
        for j, c in enumerate(part_value):
            x = (j % cols) * cell_w
            y = 30 + (j // cols) * cell_h
            for side_value, idx in enumerate((c['frame'] - 1, c['frame'])):
                file_value = pairs.get(idx)
                if file_value and file_value.exists():
                    sheet_value.paste(Image.open(file_value).convert("RGB").resize((tw, th)), (x + side_value * (tw + 4), y))
            color_value = (235, 60, 60) if c["type"] == "cut" else (255, 160, 0)
            name_value = "cut" if c["type"] == "cut" else 'probable'
            d.text((x + 2, y + th + 4), f"{mmss(c['t'])}  {name_value}  score {format_number(c['note'], 2)}", fill=color_value, font=font_p)
        destination = output_base.parent / (output_base.stem + (f'_{f + 1:02d}' if total > 1 else "") + ".jpg")
        sheet_value.save(destination, quality=88)
        outputs.append(destination)
    return outputs


def save_cutouts(texts_value: dict | None, frames_value: list[tuple[float, Path]], directory: Path) -> list[dict]:
    """Caption and static-text crops from frames sampled once per second."""
    if not texts_value or cv2 is None:
        return []
    directory.mkdir(parents=True, exist_ok=True)
    per_t = {round(t, 1): file_value for t, file_value in frames_value}

    def frame_near(t: float) -> Path | None:
        for d in (0.0, 0.1, -0.1):
            file_value = per_t.get(round(t + d, 1))
            if file_value:
                return file_value
        return None

    completed = []

    def distribute(listing: list[dict], n: int) -> list[dict]:
        seen_value, unique_value = set(), []
        for e in listing:
            k = normal_txt(e["text"])
            if k and k not in seen_value and frame_near(e["t"]):
                seen_value.add(k)
                unique_value.append(e)
        if len(unique_value) > n:
            step_value = len(unique_value) / float(n)
            unique_value = [unique_value[int(i * step_value)] for i in range(n)]
        return unique_value
    ids_primary = {id(e) for e in texts_value.get("_primary", [])}
    choice_value = distribute(texts_value.get("_primary", []), 4)
    choice_value += distribute([e for e in texts_value.get('_caption', []) if id(e) not in ids_primary], 2)
    choice_value.sort(key=lambda e: e["t"])
    for n, e in enumerate(choice_value, 1):
        file_value = frame_near(e["t"])
        img = cv2.imread(str(file_value))
        h, w = img.shape[:2]
        cy, ch = e["center_y"] * h, e["height"] * h
        y0, y1 = int(max(0, cy - 1.6 * ch)), int(min(h, cy + 1.6 * ch))
        destination = directory / f"caption_{n:02d}_{mmss_short(e['t']).replace(':', 'm')}s.jpg"
        cv2.imwrite(str(destination), img[y0:y1, :])
        completed.append({"file": destination.name, "t": e["t"], "type": "captions"})
    for n, f in enumerate([f for f in texts_value.get("static_texts", []) if f['duration_s'] >= 1.0][:4], 1):
        file_value = frame_near(f["start"] + 0.5) or frame_near(f["start"])
        if not file_value:
            continue
        img = cv2.imread(str(file_value))
        h, w = img.shape[:2]
        cy, ch = f["center_y"] * h, f['height_fraction'] * h
        y0, y1 = int(max(0, cy - 2.0 * ch)), int(min(h, cy + 2.0 * ch))
        destination = directory / f"static_text_{n:02d}_{mmss_short(f['start']).replace(':', 'm')}s.jpg"
        cv2.imwrite(str(destination), img[y0:y1, :])
        completed.append({"file": destination.name, "t": f["start"], "type": "static text"})
    return completed


# ----------------------------------------------------------------------------- 10. reference rules and guide

STYLES_EV = {
    "impact": "tall narrow uppercase lettering with a thick black outline",
    "word_box": 'colored box following each spoken word',
    "giant_word": "one enormous word at a time",
    "pulse": "heavy rounded uppercase lettering with emphasis on the spoken word",
    "tape": 'phrase on a dark tape behind the text',
    "clean_caption": 'TV-style captions: a complete phrase in at most two lines on a dark background',
    "paper": 'heavy lowercase letters with a dark outline; the spoken word lights up',
}


def suggest_style_caption(captions: dict) -> tuple[str, str]:
    if not captions.get('has_captions'):
        return "impact", 'no captions measured in the reference; retaining editonny defaults'
    ppl = captions.get('median_words_per_line') or 2
    alt = (captions.get('line_height_fraction') or 0.03) * 1920
    uppercase = captions.get("uppercase_pct")
    box_value = captions.get("solid_background_pct", 0) >= 0.5
    narrow = (captions.get('letter_width_height_ratio') or 0.6) < 0.5
    if ppl <= 1.0 and alt >= 90:
        return "giant_word", "one large word at a time"
    if box_value and (ppl >= 5 or (captions.get('lines_per_group') or 1) >= 2):
        return "clean_caption", 'long phrase on a dark background'
    if box_value:
        return "tape", 'text on a solid background'
    if uppercase is not None and uppercase >= 0.7:
        if narrow or captions.get("dark_outline_pct", 0) >= 0.5:
            return "impact", 'narrow uppercase lettering or dark outlines'
        return "pulse", 'wide uppercase letters without an outline'
    if uppercase is not None and uppercase <= 0.35:
        return "paper", 'heavy lowercase lettering'
    return "impact", 'mixed case; verify in the extracted frames'


def fonts_similar(captions: dict) -> dict:
    prop = captions.get('letter_width_height_ratio')
    uppercase = captions.get("uppercase_pct") or 0
    if prop is None:
        return {"shape": 'not measured', "google_fonts": ["Montserrat", "Inter", "Anton"]}
    # Vision boxes cover the entire line; lowercase height includes ascenders and descenders.
    narrow, wide = (0.62, 0.90) if uppercase >= 0.7 else (0.45, 0.60)
    if prop < narrow:
        return {"shape": "narrow (condensed)", "google_fonts": ["Anton", "Bebas Neue", "Oswald"]}
    if prop > wide:
        return {"shape": 'wide, geometric', "google_fonts": ["Montserrat", "Poppins", "Archivo"]}
    return {"shape": 'medium', "google_fonts": ["Inter", "Figtree", "Manrope"]}


def assemble_rules(worksheet: dict, cuts_value: list[dict], pacing: dict, pacing_all: dict, framing: dict, captions: dict,
                  colors_value: dict, audio: dict, speech_value: dict | None, args) -> dict:
    style_value, reason_value = suggest_style_caption(captions)
    fonts_value = fonts_similar(captions)
    accent_value = captions.get('accent_color')   # Leave unmeasured caption colors empty so the editor can use the supplied brand color.
    text_color = captions.get('text_color') or "#FFFFFF"
    music_value = audio.get("music", {}) if audio.get("has_audio") else {}
    effects = audio.get("effects", {}) if audio.get("has_audio") else {}
    has_music = music_value.get("verdict") in ('probable music', 'loud continuous sound')
    vol_db = None
    if has_music and music_value.get("difference_db") is not None:
        vol_db = int(round(max(-24, min(-10, -(music_value["difference_db"] - 4)))))
    zoom = framing.get('zoom_at_cuts') or {}
    level_value = zoom.get('suggested_level')
    font_px = captions.get('estimated_standard_font')
    vertical = worksheet["vertical"] and worksheet["height"] / max(1, worksheet["width"]) > 1.1
    sizes_ev = {"impact": 134, "word_box": 86, "giant_word": 196, "pulse": 78, "tape": 78,
                   "clean_caption": 60, "paper": 88}
    scale_value = None
    if font_px and vertical:
        scale_value = round(max(0.9, min(1.15, font_px / sizes_ev.get(style_value, 86))), 2)
    return {
        "version": VERSION_VALUE,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by": "helpers/measure_reference.py",
        "reference": {"file": worksheet["file"], "sha256": worksheet["sha256"]},
        'warning': 'Measured style values. Do not copy speech, promises, brands, logos, music, people, or claimed results from the reference.',
        "metadata": {k: worksheet[k] for k in ("duration_s", "width", "height", "aspect_ratio", "fps", "has_audio")},
        "pacing": {
            "origin": "measured",
            'cuts_per_minute': pacing['cuts_per_minute'],
            'mean_shot_s': pacing['mean_shot_s'],
            'median_shot_s': pacing['median_shot_s'],
            'average_cut_interval_s': pacing['average_cut_interval_s'],
            'shortest_shot_s': pacing['shortest_shot_s'],
            'longest_shot_s': pacing['longest_shot_s'],
            'cuts_in_first_3s': pacing['cuts_in_first_3s'],
            'cuts_per_10s': pacing['cuts_per_10s'],
            "cuts": [c["t"] for c in cuts_value if c["type"] == "cut"],
            'probable_cuts': [c["t"] for c in cuts_value if c["type"] != "cut"],
            "including_probable": {'cuts_per_minute': pacing_all['cuts_per_minute'],
                              'mean_shot_s': pacing_all['mean_shot_s']},
            'scene_score_threshold': args.threshold_value,
        },
        "framing": {"origin": "estimated", **{k: v for k, v in framing.items() if not k.startswith("_")}},
        "captions": {
            "origin": {'word_position_and_size': "measured", 'style_font_colors': "estimated"},
            **{k: v for k, v in captions.items() if not k.startswith("_") and k not in ("static_texts", "bright_screen_intervals", "hook")},
            'similar_font': fonts_value,
        },
        "static_texts": [{k: v for k, v in f.items() if k != "text"} for f in captions.get("static_texts", [])],
        'written_hook': captions.get('hook'),
        "colors": {"origin": "measured pixels; estimated color roles", "text": text_color,
                  "accent": accent_value, **colors_value},
        'speech': speech_value,
        "audio": {
            "origin": {"loudness": "measured", 'music_and_effects': "estimated"},
            "has_audio": audio.get("has_audio", False),
            "loudness": audio.get("loudness"),
            "stereo": audio.get("stereo"),
            'left_right_correlation': audio.get('left_right_correlation'),
            "music": music_value,
            "effects": {k: v for k, v in effects.items() if k != "events"} if effects else None,
            'effect_times': [{"t": e["t"], "type": e["type"]} for e in effects.get("events", [])] if effects else [],
            'attacks_per_minute': audio.get('attacks_per_minute'),
        },
        "edit_data": {
            "note": 'Fields match edit_data.json. These are starting points, not mandatory settings.',
            "accent": accent_value,
            "camera": {"zoom_cuts": bool(zoom.get("likely_zoom")),
                       "zoom_levels": [1.0, level_value] if level_value else [1.0, 1.14],
                       "push_in": False},
            "captions": {"enabled": bool(captions.get('has_captions')), "style": style_value, 'style_reason': reason_value,
                         "center_y": captions.get("standard_center_y") if vertical else None, "font_scale": scale_value,
                         "position_note": None if vertical else
                         'nonvertical reference: convert using the height fraction and verify in the sample',
                         "max_words": int(min(3, max(1, round(captions.get('median_words_per_line') or 3))))
                         if style_value != "clean_caption" else 8},
            "sfx": {"enabled": (bool(effects.get("probable_count")) if effects.get("confidence") != "low" else None),
                    "volume": 0.6,
                    'target_events_per_minute': effects.get("per_minute") if effects.get("confidence") != "low" else None,
                    "confidence": effects.get("confidence"),
                    'sound_at_cuts_pct': effects.get('cuts_with_sound_pct')},
            "music": {"enabled": bool(has_music), "volume_db": vol_db if vol_db is not None else -15,
                      "duck": bool(music_value.get('ducks_during_speech', True)),
                      "fade_out": float(music_value.get('disappears_before_end_s') or 1.5) if has_music else 1.5},
            "loudness": {"i": -14, "tp": -1.0},
            "target_pacing": {'cuts_per_minute': pacing['cuts_per_minute'], 'mean_shot_s': pacing['mean_shot_s']},
        },
        "hyperframes": {
            "note": 'Color and typography tokens for a frame design; layout remains flexible.',
            "colors": {"text": text_color, "accent": accent_value,
                       "background": (colors_value.get("dominant") or [{}])[0].get("hex")},
            "typography": {"captions": {"fontFamily": fonts_value["google_fonts"][0],
                                       "weight": 800,
                                       "standard_canvas_px": font_px,
                                       "standard_canvas": captions.get("standard_canvas"),
                                       "upper": bool((captions.get("uppercase_pct") or 0) >= 0.7)}},
            'caption_center_y_fraction': captions.get("center_y"),
        },
    }


def _line_tab(*cols) -> str:
    return "| " + " | ".join(str(c) for c in cols) + " |"


def write_guide(path_value: Path, worksheet: dict, cuts_value: list[dict], pacing: dict, pacing_all: dict, framing: dict,
                  captions: dict | None, colors_value: dict, audio: dict, speech_value: dict | None, rules_value: dict,
                  files_value: dict, args, missing: list[str]) -> None:
    L: list[str] = []
    a = L.append
    name_value = worksheet["file"]
    short_value = name_value if len(name_value) <= 60 else name_value[:57] + "..."
    prov = [c for c in cuts_value if c["type"] != "cut"]
    captions = captions or {}
    engine = rules_value["edit_data"]
    a(f'# Measured style guide: {short_value}')
    a("")
    a(f"Generated at {time.strftime('%d/%m/%Y %H:%M')} by measure_reference.py from editonny-engine")
    a('Use this to edit another video with similar visual direction. Do not copy speech, promises,')
    a('brands, logos, music, people, or result figures from the reference.')
    a("")
    a("How to read this guide:")
    a('- **MEASURED**: calculated from the recording, within the stated precision.')
    a('- **ESTIMATED**: inferred from measurements; verify against frames and crops.')
    a('- **UNKNOWN**: listed at the end. Do not invent these details.')
    a("")

    # overview
    a("## Overview")
    a("")
    if pacing["cuts"]:
        a(f"- Pacing: {pacing['cuts']} cuts in {format_number(worksheet['duration_s'])} s, {format_number(pacing['cuts_per_minute'])} per minute, one new shot every {format_number(pacing['mean_shot_s'])} seconds on average. **MEASURED**")
    else:
        a(f"- Pacing: no hard cut detected in {format_number(worksheet['duration_s'])} seconds: one shot or only soft transitions. **MEASURED**")
    if prov:
        a(f"- Another {len(prov)} tentative cuts to inspect; including them would yield {format_number(pacing_all['cuts_per_minute'])} per minute. **ESTIMATED**")
    if framing.get('face_pct') is not None:
        a(f"- Face visible in {pct(framing['face_pct'])} of sampled seconds; typical size: {framing.get('shot_type', '?')}. **ESTIMATED**")
    if captions.get('has_captions'):
        a(f"- Captions with {format_number(captions.get('median_words_per_line'), 0)} words per line, centered at {pct(captions.get('center_y'))} of frame height, {pct(captions.get('uppercase_pct'))} uppercase. **MEASURED**")
    elif captions:
        a('- No spoken captions detected on screen. **MEASURED** through text recognition')
    if audio.get("has_audio"):
        tr = audio["music"]
        a(f"- Audio: {format_number(audio.get('loudness', {}).get('integrated_lufs'))} LUFS; {tr['verdict']} ({tr['explanation']}). **ESTIMATED**")
        ef = audio["effects"]
        a(f"- Probable sound effects: {ef['probable_count']} ({format_number(ef['per_minute'])} per minute). **ESTIMATED**")
    if speech_value:
        a(f"- Speech: {format_number(speech_value['words_per_minute'], 0)} words per minute, first word at {format_number(speech_value['first_word_s'], 2)} s. **MEASURED**")
    a("")

    a('## 1. Technical measurements')
    a("")
    a(_line_tab("Item", "Value"))
    a(_line_tab("---", "---"))
    a(_line_tab("File", f'`{short_value}`'))
    a(_line_tab('Duration', f"{format_number(worksheet['duration_s'], 2)} s"))
    a(_line_tab('Resolution', f"{worksheet['width']} x {worksheet['height']} ({worksheet['aspect_ratio']})"
                 + (f", rotated {worksheet['rotation']} degrees" if worksheet["rotation"] else "")))
    a(_line_tab("Frames per second", format_number(worksheet["fps"], 2) + (' (variable)' if worksheet["variable_fps"] else "")))
    a(_line_tab("Audio", f"{worksheet['audio_codec']} {worksheet['audio_channels']} channels {worksheet['audio_sample_rate_hz']} Hz"
                 if worksheet["has_audio"] else 'no audio'))
    a(_line_tab('SHA-256 fingerprint', f"`{worksheet['sha256'][:16]}...`"))
    a("")

    a("## 2. Pacing and cuts")
    a("")
    a(f'Frame-by-frame FFmpeg scene detector. Cut = scene score at least {format_number(args.threshold_value, 2)}. Tentative cut: a weak peak at least {format_number(RATIO_PROBABLE, 0)} times its neighborhood level.')
    a("")
    a(_line_tab("Measurement", "Value", "Status"))
    a(_line_tab("---", "---", "---"))
    a(_line_tab("Hard cuts", pacing["cuts"], "MEASURED"))
    a(_line_tab("Cuts per minute", format_number(pacing['cuts_per_minute']), "MEASURED"))
    a(_line_tab('Average shot duration', f"{format_number(pacing['mean_shot_s'], 2)} s (median {format_number(pacing['median_shot_s'], 2)} s)", "MEASURED"))
    a(_line_tab('Average time between cuts', f"{format_number(pacing['average_cut_interval_s'], 2)} s"
                 if pacing['average_cut_interval_s'] else "fewer than two cuts", "MEASURED"))
    if pacing['shortest_shot_s'] is not None:
        a(_line_tab("Shortest / longest shot", f"{format_number(pacing['shortest_shot_s'], 2)} s / {format_number(pacing['longest_shot_s'], 2)} s", "MEASURED"))
    if pacing['longest_uncut_interval']:
        m = pacing['longest_uncut_interval']
        a(_line_tab('Longest interval without a cut', f"{mmss(m['start'])} to {mmss(m['end'])}", "MEASURED"))
    a(_line_tab('Shots shorter than 1.5 seconds', pct(pacing['short_shots_pct']), "MEASURED"))
    a(_line_tab("Cuts in the first three seconds", pacing['cuts_in_first_3s'], "MEASURED"))
    a(_line_tab('Tentative weak cuts', len(prov), "ESTIMATED, verify in `review-cuts.jpg`"))
    a("")
    if pacing['cuts_per_10s']:
        a("Cuts per 10 seconds: " + ", ".join(f'{i * 10}-{i * 10 + 10}s: {n}' for i, n in enumerate(pacing['cuts_per_10s'])) + ".")
        a("")
    if cuts_value:
        a("List: " + ", ".join(f"{mmss(c['t'])}" + ("" if c["type"] == "cut" else ' (probable)')
                                + (' (multiple frames)' if c.get("notes") else "") for c in cuts_value) + ".")
        a("")

    a("## 3. Framing and zoom (ESTIMATED, face detection)")
    a("")
    if framing.get('face_pct') is None:
        a('Face detection unavailable. Inspect the contact sheet.')
    else:
        a(_line_tab("Measurement", "Value"))
        a(_line_tab("---", "---"))
        a(_line_tab('Seconds with a face', pct(framing['face_pct'])))
        if framing.get('median_face_height'):
            a(_line_tab("Face height", f"{pct(framing['median_face_height'])} of frame height ({framing['shot_type']})"))
            a(_line_tab("Eye height", f"{pct(framing['median_eye_y'])} from the top"))
            a(_line_tab('Face position', framing.get("horizontal_position", "?")))
        if framing.get('shots_without_face') is not None:
            a(_line_tab('Shots without a face: supporting image, screen, or product',
                         f"{framing['shots_without_face']} of {framing['measured_shots']}"))
        z = framing.get('zoom_at_cuts') or {}
        if z.get("pairs"):
            a(_line_tab('Face-size changes at cuts',
                         f"median {pct(z['median_change'])} in {z['pairs']} cuts within the same scene with faces on both sides; {z['with_zoom']} changed by at least 6%"))
            a(_line_tab("Zoom at cuts", ('likely; suggested level ' + format_number(z['suggested_level'], 2))
                         if z.get("likely_zoom") else 'unlikely'))
    a("")

    a("## 4. Captions and on-screen text")
    a("")
    if not captions:
        a('Text recognition disabled or unavailable. Inspect the contact sheet and crops.')
    else:
        engine_value = 'Apple Vision text recognition' if captions.get("backend") == "vision" else 'text-shaped regions without word recognition'
        a(f"Recognized using {engine_value}, {format_number(captions.get('reading_fps'), 0)} samples per second. Sizes in source pixels and converted to the standard canvas {captions.get('standard_canvas', '1080x1920')}.")
        a("")
        if captions.get('has_captions'):
            a(_line_tab("Measurement", "Value", "Status"))
            a(_line_tab("---", "---", "---"))
            a(_line_tab('Time with captions', pct(captions['captioned_time_pct']) + " of samples"
                         + (f", {pct(captions['captioned_speech_pct'])} of speech intervals" if captions.get('captioned_speech_pct') is not None else ""), "MEASURED"))
            a(_line_tab("Do captions transcribe speech?", 'yes, matches the transcript' if captions.get('matched_speech')
                         else 'not checked: no transcript', "MEASURED" if captions.get('matched_speech') else "ESTIMATED"))
            a(_line_tab('Main text position: line center', f"{pct(captions['center_y'], 1)} of frame height, y = {captions['center_y_px']} pixels (standard canvas: {captions['standard_center_y']}), {pct(captions['positions'][0]['caption_part'])} of captioned time", "MEASURED"))
            for pos in captions.get("positions", [])[1:4]:
                a(_line_tab('Another position', f"{pct(pos['center_y'], 1)} of frame height (standard canvas: {pos['standard_center_y']}), {pct(pos['caption_part'])} of captioned time, between {mmss(pos['start'])} and {mmss(pos['end'])}", "MEASURED"))
            a(_line_tab("Recognized line height", f"{captions['line_height_px']} pixels (standard canvas: {captions['standard_line_height']})", "MEASURED"))
            a(_line_tab('Words per line at the main position', f"median {format_number(captions['median_words_per_line'], 0)}, maximum {captions['max_words_per_line']}", "MEASURED"))
            a(_line_tab("Simultaneous lines", ", ".join(f'{k} lines in {pct(v)}' for k, v in
                                                          (captions.get('lines_per_group_pct') or {}).items()) or "?", "MEASURED"))
            a(_line_tab("Uppercase", pct(captions["uppercase_pct"]) + " of letters", "MEASURED"))
            a(_line_tab("Text changes per minute", format_number(captions['sampled_changes_per_minute'], 0)
                         + ' (sampled; changes faster than 0.5 seconds are not detected)', "MEASURED"))
            a(_line_tab("Font size", f"approximately {captions.get('estimated_standard_font', '?')} pixels on the standard canvas", "ESTIMATED"))
            a(_line_tab("Letter shape", rules_value["captions"]['similar_font']["shape"], "ESTIMATED"))
            a(_line_tab('Text color at the main position', captions.get('text_color') or "?", "ESTIMATED"))
            a(_line_tab("Accent color (spoken word)", (captions.get('accent_color') or 'not found')
                         + (f", in {pct(captions.get('two_colors_pct'))} of lines" if captions.get('accent_color') else ""), "ESTIMATED"))
            if captions.get('muted_color'):
                a(_line_tab('Muted color for upcoming words', captions['muted_color'], "ESTIMATED"))
            a(_line_tab("Dark outline or shadow", 'probable' if (captions.get("dark_outline_pct") or 0) >= 0.4 else 'unlikely', "ESTIMATED"))
            a(_line_tab('Solid background behind text: box or banner', 'probable' if (captions.get("solid_background_pct") or 0) >= 0.5 else 'unlikely', "ESTIMATED"))
            a(_line_tab("Closest editonny style", f"`{engine['captions']['style']}` ({engine['captions']['style_reason']})", "ESTIMATED"))
            a(_line_tab("Similar open fonts (Google Fonts)", ", ".join(rules_value["captions"]['similar_font']["google_fonts"]), "ESTIMATED"))
        else:
            a('No spoken captions detected on screen. **MEASURED**')
        a("")
        g = captions.get('hook') or {}
        if g.get("text_in_first_3s"):
            a(f"Text in the first three seconds: yes, in zone {g.get('zone')}, up to {g.get('max_height_px')} pixels high"
              + (', larger than captions: hook title.' if g.get('larger_than_captions') else ".") + " **MEASURED**")
            a("")
        fx = captions.get("static_texts") or []
        if fx:
            a(f'Text stationary for at least three seconds: titles, labels, brands: {len(fx)}. Recognized text is in the measured-interval CSV; this section records only appearance. **MEASURED**')
            a("")
            a(_line_tab("From", 'Up to', "Zone", "Height", "Words", "Notes"))
            a(_line_tab("---", "---", "---", "---", "---", "---"))
            for f in fx[:15]:
                a(_line_tab(mmss(f["start"]), mmss(f["end"]), f["zone"], f"{f['height_px']} px", f["words"],
                             'brand or profile; do not copy' if f["looks_like_brand_or_handle"] else ""))
            if len(fx) > 15:
                a(f'(another {len(fx) - 15} in measured-intervals.csv)')
            a("")
        if captions.get('bright_screen_intervals'):
            a('Intervals with an almost entirely light screen: '
              + ", ".join(f'{mmss(x0)} to {mmss(x1)}' for x0, x1 in captions['bright_screen_intervals']) + ". **MEASURED**")
            a("")

    a("## 5. Colors")
    a("")
    if colors_value:
        a('Pixel measurements at one frame per second are **MEASURED**. Color roles are **ESTIMATED**: skin, wall,')
        a('and clothing contribute to the measurement. Text accents are described in section 4.')
        a("")
        a(_line_tab("Color", "Image fraction", "Appearance"))
        a(_line_tab("---", "---", "---"))
        for c in colors_value.get("dominant", []):
            a(_line_tab(f"`{c['hex']}`", pct(c["fraction"]), c["name"]))
        a("")
        if colors_value.get("saturated"):
            a('Strong visible colors, potential accents: ' + ", ".join(
                f"`{c['hex']}` {c['name']}" for c in colors_value["saturated"]) + ".")
            a("")
        a(f"Overall appearance: mean brightness {pct(colors_value['mean_brightness'])}, mean saturation {pct(colors_value['mean_saturation'])}, {pct(colors_value['dark_frames_pct'])} dark frames, {pct(colors_value['bright_frames_pct'])} bright frames, temperature {colors_value['temperature']}.")
        a("")

    a("## 6. Audio")
    a("")
    if not audio.get("has_audio"):
        a('No audio. **MEASURED**')
    else:
        lo = audio.get("loudness", {})
        tr = audio["music"]
        ef = audio["effects"]
        a("All audio findings come from signal measurements; subjective listening was not performed.")
        a("")
        a(_line_tab("Measurement", "Value", "Status"))
        a(_line_tab("---", "---", "---"))
        a(_line_tab("Perceived loudness (integrated)", f"{format_number(lo.get('integrated_lufs'))} LUFS", "MEASURED"))
        a(_line_tab("True peak", f"{format_number(lo.get('true_peak_dbtp'))} dBTP", "MEASURED"))
        a(_line_tab('Volume variation', f"{format_number(lo.get('loudness_range_lu'))} LU", "MEASURED"))
        a(_line_tab('Stereo', ("yes" if audio["stereo"] else 'no, effectively mono')
                     + f" (left/right correlation {format_number(audio['left_right_correlation'], 3)})", "MEASURED"))
        a(_line_tab('Voice and background levels', f"{format_number(tr['voice_dbfs'])} / {format_number(tr['between_phrase_floor_dbfs'])} dBFS (difference {format_number(tr['difference_db'])} dB; {tr['method']})",
                     "MEASURED" if tr.get("reliable_floor") else "ESTIMATED"))
        a(_line_tab("Music", f"{tr['verdict']}: {tr['explanation']}", "ESTIMATED"))
        if "ducks_during_speech" in tr:
            a(_line_tab("Music ducks during speech", 'likely' if tr['ducks_during_speech'] else 'unlikely', "ESTIMATED"))
        if tr.get("bpm"):
            a(_line_tab("Beat", f"approximately {format_number(tr['bpm'], 0)} BPM (strength {format_number(tr['bpm_strength'], 2)})", "ESTIMATED"))
        if tr.get('disappears_before_end_s'):
            a(_line_tab("Music ends before the video", f"approximately {format_number(tr['disappears_before_end_s'])} seconds before the end", "ESTIMATED"))
        a(_line_tab('Probable sound effects', f"{ef['probable_count']} ({format_number(ef['per_minute'])} per minute), confidence {ef['confidence']}", "ESTIMATED"))
        if ef.get('cuts_with_sound_pct') is not None:
            a(_line_tab('Cuts accompanied by sound within 0.2 seconds', pct(ef['cuts_with_sound_pct']), "ESTIMATED"))
        a(_line_tab('Audio attacks per minute, including speech syllables', format_number(audio.get('attacks_per_minute'), 0), "MEASURED"))
        a("")
        if ef["events"]:
            a('Probable effects: timing and spectral family: ' + "; ".join(
                f"{mmss(e['t'])} {e['type'].split(' ')[0]}" for e in ef["events"][:40]) + (" ..." if len(ef["events"]) > 40 else "") + ".")
            a("")
        a(f"Effect detection method: {ef['method']}. Musical beats and audible breathing may also be detected.")
        a("")

    a("## 7. Speech")
    a("")
    if speech_value:
        a('Local Whisper transcription; only numerical measurements are reported here. **MEASURED**')
        a("")
        a(_line_tab("Measurement", "Value"))
        a(_line_tab("---", "---"))
        a(_line_tab("Language", speech_value.get("language") or "?"))
        a(_line_tab("Words per minute", format_number(speech_value['words_per_minute'], 0)))
        a(_line_tab("First word", f"{format_number(speech_value['first_word_s'], 2)} s"))
        a(_line_tab("Pauses longer than 0.3 seconds", f"{speech_value['pauses_over_0_3s']} ({format_number(speech_value['pauses_per_minute'])} per minute)"))
        a(_line_tab("Longest pause", f"{format_number(speech_value['longest_pause_s'], 2)} s"))
        a(_line_tab('Time after the final word', f"{format_number(speech_value['trailing_silence_s'], 2)} s"))
    else:
        a('No transcription: disabled or failed. Speech pacing was not measured.')
    a("")

    a("## 8. Visual review (complete after inspecting the contact sheets)")
    a("")
    a('Inspect the contact sheets and review crops. Describe what is visible without copying reference text.')
    a("")
    for p in ('Video type: speaker, screen, product, tutorial, or mixed:',
              'Do the detected cuts match the contact sheet? Are any tentative detections real cuts?',
              'Elements beyond captions: hook title, arrows, screenshots, emoji, progress bar, frame:',
              'Transitions: hard cut, zoom, whip, flash, or slide:',
              "Supporting images (B-roll): start times and durations:",
              'Opening direction in the first three seconds and closing call to action:',
              "What suits the user's video and what does not:"):
        a(f'- {p}')
    a("")

    a('## 9. What measurement cannot establish')
    a("")
    fixed_value = [
        'Exact font name; only apparent width and weight are measured.',
        'Exact animation curves. Generate a 10 or 30 fps filmstrip for closer inspection.',
        'Dissolves, soft transitions, and slow digital zooms; the detector identifies abrupt changes.',
        'Whether a sound is a whoosh, pop, or snap; only timing and frequency range were measured, not listened to.',
        'Music name, author, or license. Never reuse reference music without rights.',
        'Editing application, AI model, or whether an object is genuinely 3D.',
        'Sales or audience results of the reference.',
    ]
    for f in fixed_value + missing:
        a(f'- {f}')
    a("")

    a("## 10. Measurement files")
    a("")
    for k, v in files_value.items():
        a(f'- `{k}`: {v}')
    a("")
    text_value = "\n".join(L)
    text_value = text_value.replace("\u2014", ",").replace("\u2013", "-")
    path_value.write_text(text_value, encoding="utf-8")


def write_intervals_measured(path_value: Path, dur: float, frames_value: list[tuple[float, Path]],
                             faces_1s: dict[int, dict | None], cuts_value: list[dict], plans_edges: list[float],
                             texts_value: dict | None, audio: dict, words_value: list[dict] | None,
                             scores: tuple[np.ndarray, np.ndarray], brightness_1s: dict[int, float],
                             paper_1s: dict[int, float]) -> None:
    second_count = int(math.ceil(dur))
    items_value = (texts_value or {}).get("_items", [])
    leg_ids = {id(e) for e in (texts_value or {}).get('_caption', [])}
    sounds_value = (audio.get("effects") or {}).get("events", []) if audio.get("has_audio") else []
    per_seg = audio.get('_per_second', []) if audio.get("has_audio") else []
    tn, sn = scores
    with open(path_value, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["second", "time", "shot", "cut", "visible_content", "on_screen_text", "sound", "observation"])
        for s in range(second_count):
            plan_value = int(np.searchsorted(plans_edges, s + 0.5, side="right"))
            cs = [c for c in cuts_value if s <= c["t"] < s + 1]
            cut_txt = " ".join(mmss(c["t"]) + ("" if c["type"] == "cut" else '(probable)') for c in cs)
            parts_value = []
            r = faces_1s.get(s)
            if r:
                side_value = "left" if r["cx"] < 0.4 else ("right" if r["cx"] > 0.6 else "center")
                parts_value.append(f"{type_start_plan(r['height'])} ({pct(r['height'])} of frame height, {side_value})")
                if r.get('faces', 1) > 1:
                    parts_value.append(f"{r['faces']} faces")
            elif s in faces_1s:
                parts_value.append('no face')
            b = brightness_1s.get(s)
            if b is not None:
                parts_value.append("dark image" if b < 0.3 else ("bright image" if b > 0.75 else ""))
            if paper_1s.get(s, 0) >= 0.72:
                parts_value.append('light screen: graphic or screenshot')
            sel = (tn >= s) & (tn < s + 1)
            if sel.any():
                mov = float(np.mean(sn[sel]))
                if mov > 0.05:
                    parts_value.append("large movement")
            txts = []
            seen_value = set()
            for e in items_value:
                if s <= e["t"] < s + 1 and e.get("text"):
                    k = normal_txt(e["text"])
                    if k in seen_value:
                        continue
                    seen_value.add(k)
                    tag = "captions" if id(e) in leg_ids else ("fixed" if e["fixed"] else "text")
                    zone = "top" if e["center_y"] < 0.3 else ("bottom" if e["center_y"] > 0.7 else "middle")
                    txts.append(f"[{tag}, {zone}] {e['text']}")
            sound_value = []
            if words_value is not None:
                word_count = sum(1 for p in words_value if s <= p["start"] < s + 1)
                sound_value.append(f'speech: {word_count} words' if word_count else 'no speech')
            if s < len(per_seg) and per_seg[s]:
                sound_value.append(f"level {format_number(per_seg[s]['db'])} dBFS")
                if per_seg[s].get("side_db") is not None:
                    sound_value.append(f"stereo {format_number(per_seg[s]['side_db'])} dBFS")
            for e in sounds_value:
                if s <= e["t"] < s + 1:
                    sound_value.append(f"probable effect {mmss(e['t'])} {e['type'].split(' ')[0]}")
            w.writerow([s, mmss_short(s), plan_value, cut_txt, "; ".join(p for p in parts_value if p),
                        " | ".join(txts), "; ".join(sound_value), ""])


# ----------------------------------------------------------------------------- principal

def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Measure a reference video's editing style without copying its content.")
    ap.add_argument('video', type=Path, help='reference video: reel, ad, or VSL')
    ap.add_argument("--output", type=Path, required=True, help='output folder for the guide, measurements, rules, and images', dest='output_value')
    ap.add_argument("--threshold", type=float, default=0.20,
                    help='FFmpeg scene score from 0 to 1 for hard-cut detection (default 0.20)', dest='threshold_value')
    ap.add_argument("--without-speech", action="store_true", help='skip transcription; faster, but does not measure speech pacing', dest='without_speech')
    ap.add_argument("--without-text", action="store_true", help='skip on-screen text recognition', dest='without_text')
    ap.add_argument("--transcript", type=Path, help='existing word JSON in transcribe.py format', dest='transcript_value')
    ap.add_argument("--engine-dir", type=Path, default=EV_DEFAULT,
                    help='alternate engine folder (default: this engine or EDITONNY_ENGINE_DIR)', dest='engine_dir')
    ap.add_argument("--fps-text", type=float, default=2.0, help='text-recognition samples per second (default 2)', dest='fps_text')
    args = ap.parse_args(argv)

    video = args.video.expanduser().absolute()
    if not video.is_file():
        print(f'error: video not found {video}', file=sys.stderr)
        return 2
    output_value = args.output_value.expanduser().absolute()
    if output_value == video.parent:
        print("error: choose a dedicated measurement output folder, not the video's parent folder.", file=sys.stderr)
        return 2
    engine = args.engine_dir.expanduser().absolute()
    for b in ("ffmpeg", "ffprobe"):
        if shutil.which(b) is None:
            print(f'error: missing {b}. On macOS: brew install ffmpeg', file=sys.stderr)
            return 2
    if cv2 is None:
        print('OpenCV is missing. Run this helper with the locked engine environment.', file=sys.stderr)
        return 2
    output_value.mkdir(parents=True, exist_ok=True)
    measurements = output_value / "measurements"
    measurements.mkdir(exist_ok=True)
    # On repeated runs, remove only images generated by this script.
    for default_value in ("contact-sheets/sheet_*.jpg", "crops/caption_*.jpg", "crops/static_text_*.jpg",
                   "review-cuts*.jpg"):
        for old_file in output_value.glob(default_value):
            old_file.unlink()
    missing: list[str] = []
    start_value = time.time()
    font_value = load_font_value(engine, 18)
    font_p = load_font_value(engine, 14)

    try:
        announce('1/8 technical metadata...')
        worksheet = technical_metadata(video)
        dur, W, H = worksheet['duration_s'], worksheet["width"], worksheet["height"]
        save_json_value(measurements / "metadata.json", worksheet)

        announce("2/8 detecting cuts (frame-by-frame scene score)...")
        with tempfile.TemporaryDirectory(prefix="editonny-measure-") as tmp_s:
            tmp = Path(tmp_s)
            times_q, frame_scores = score_scenes(video, tmp)
            with open(measurements / "scene-scores.csv", "w", newline="", encoding='utf-8') as fh:
                wr = csv.writer(fh)
                wr.writerow(["frame", "t", "note"])
                for i, (tq, sq) in enumerate(zip(times_q, frame_scores)):
                    wr.writerow([i, round(float(tq), 4), round(float(sq), 4)])
            cuts_value = find_cuts(times_q, frame_scores, dur, args.threshold_value)
            strong_times = [c["t"] for c in cuts_value if c["type"] == "cut"]
            pacing = measure_pacing(strong_times, dur)
            pacing_all = measure_pacing([c["t"] for c in cuts_value], dur)
            save_json_value(measurements / "cuts.json", {"threshold": args.threshold_value, "cuts": cuts_value})

            announce("3/8 extracting one frame per second...")
            qw, qh = size_output(W, H, 1280)
            frames_value = frames_per_second(video, tmp / "q1s", qw, qh)

            announce("4/8 faces, framing, and colors...")
            face_detector = FaceDetector(engine)
            if not face_detector.ok:
                missing.append('Framing: the bundled YuNet face detector did not load.')
            faces_1s: dict[int, dict | None] = {}
            brightness_1s: dict[int, float] = {}
            for t, file_value in frames_value:
                img = cv2.imread(str(file_value))
                s = int(math.floor(t + 1e-6))
                faces_1s[s] = face_detector.measure_value(img) if face_detector.ok else None
                if img is not None:
                    brightness_1s[s] = float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].mean()) / 255.0
            # Frames before and after each cut, plus the middle of each shot.
            edges_value = [0.0] + [c["t"] for c in cuts_value] + [dur]
            middles = []
            for a0, a1 in zip(edges_value, edges_value[1:]):
                middles.append(int(min(len(times_q) - 1, np.searchsorted(times_q, (a0 + a1) / 2))))
            pw, ph = size_output(W, H, 640)
            idx_requests = [c['frame'] - 1 for c in cuts_value] + [c['frame'] for c in cuts_value] + middles
            extra = frames_by_index(video, times_q, idx_requests, tmp / "pairs", pw, ph)
            framing: dict = {}
            if face_detector.ok:
                vals = [r for r in faces_1s.values() if r]
                framing['face_pct'] = round(len(vals) / max(1, len(faces_1s)), 3)
                if vals:
                    alt = float(np.median([r["height"] for r in vals]))
                    cx = float(np.median([r["cx"] for r in vals]))
                    framing.update({'median_face_height': round(alt, 3), 'shot_type': type_start_plan(alt),
                                "median_eye_y": round(float(np.median([r["eye_y"] for r in vals])), 3),
                                'median_face_center_x': round(cx, 3),
                                "horizontal_position": "left" if cx < 0.4 else ("right" if cx > 0.6 else "center"),
                                'multiple_faces_pct': round(sum(1 for r in vals if r['faces'] > 1) / len(vals), 3)})
                without = 0
                for i in middles:
                    file_value = extra.get(i)
                    if file_value is not None and face_detector.measure_value(cv2.imread(str(file_value))) is None:
                        without += 1
                framing['measured_shots'] = len(middles)
                framing['shots_without_face'] = without
                change = []
                for c in cuts_value:
                    a_file, d_file = extra.get(c['frame'] - 1), extra.get(c['frame'])
                    if not a_file or not d_file:
                        continue
                    ia, idp = cv2.imread(str(a_file)), cv2.imread(str(d_file))
                    ra, rd = face_detector.measure_value(ia), face_detector.measure_value(idp)
                    if ra and rd and abs(ra["cx"] - rd["cx"]) < 0.2 and same_scene(ia, idp):
                        r = rd["height"] / ra["height"]
                        if 1 / 1.8 < r < 1.8:
                            change.append(max(r, 1 / r))
                if change:
                    large = [m for m in change if m >= 1.06]
                    level_value = round(float(np.median(large)), 2) if large else None
                    framing['zoom_at_cuts'] = {
                        "pairs": len(change), "median_change": round(float(np.median(change)) - 1, 3),
                        "with_zoom": len(large),
                        "likely_zoom": len(change) >= 3 and len(large) >= max(2, 0.4 * len(change)),
                        'suggested_level': round(min(1.25, max(1.04, level_value)), 2) if level_value else None,
                    }
            colors_value = image_palette(frames_value)

            announce('5/8 speech: local transcription...')
            file_tr = None
            language_value = None
            if args.transcript_value:
                file_tr = args.transcript_value.expanduser().absolute()
            elif not args.without_speech and worksheet["has_audio"]:
                file_tr = transcribe_value(video, engine, measurements)
                if file_tr is None:
                    missing.append('Transcription failed; speech pacing and caption-to-speech correspondence were not measured.')
            words_value = read_words(file_tr)
            if file_tr and file_tr.exists():
                try:
                    language_value = json.loads(file_tr.read_text(encoding="utf-8")).get("language")
                except Exception:
                    language_value = None
            speech_value = measure_speech(words_value, dur, language_value)

            announce("6/8 on-screen text...")
            texts_value = None
            if not args.without_text:
                doc = read_text_in_screen(video, engine, measurements, file_tr if words_value else None, args.fps_text)
                if doc:
                    texts_value = analyze_texts(doc, words_value, W, H)
                    if texts_value.get("backend") != "vision":
                        missing.append('Without Apple Vision, detection finds text-shaped regions only; words per line and capitalization were not measured.')
                    # Caption text colors.
                    if texts_value.get('has_captions'):
                        per_t = {round(t, 1): file_value for t, file_value in frames_value}
                        samples_color = []
                        for e in texts_value["_primary"]:
                            file_value = per_t.get(round(e["t"], 1))
                            if not file_value:
                                continue
                            img = cv2.imread(str(file_value))
                            h, w = img.shape[:2]
                            cy, ch = e["center_y"] * h, e["height"] * h
                            cx, cw = e["center_x"] * w, e["width"] * w
                            box_value = (int(max(0, cx - cw / 2 - 0.25 * ch)), int(max(0, cy - 0.75 * ch)),
                                     int(min(w, cx + cw / 2 + 0.25 * ch)), int(min(h, cy + 0.75 * ch)))
                            c = colors_of_text(img, box_value)
                            if c:
                                samples_color.append(c)
                        if samples_color:
                            roles = roles_of_colors(samples_color)
                            texts_value.update(roles)
                            texts_value["dark_outline_pct"] = round(sum(c["dark_outline"] for c in samples_color) / len(samples_color), 3)
                            texts_value["solid_background_pct"] = round(sum(c["solid_background"] for c in samples_color) / len(samples_color), 3)
                            texts_value['color_samples'] = len(samples_color)
                        alt1920 = texts_value.get('standard_line_height')
                        if alt1920:
                            texts_value['estimated_standard_font'] = int(round(alt1920 * 1.05))
            else:
                missing.append('Text recognition disabled with --without-text.')
            if texts_value is None and not args.without_text:
                missing.append('Text recognition did not run; captions were not measured.')

            announce('7/8 audio: levels, music, and effects...')
            audio = analyze_audio(video, dur, words_value, [c["t"] for c in cuts_value]) if worksheet["has_audio"] else {"has_audio": False}
            if audio.get("has_audio"):
                save_json_value(measurements / "audio.json", {k: v for k, v in audio.items() if k != "_per_second"})
                if not audio.get("stereo"):
                    missing.append('Mono audio: without a side channel, music and effect detection is less reliable.')
                if words_value is None:
                    missing.append('Without transcription, between-phrase levels were estimated from the quietest intervals.')

            announce("8/8 contact sheets, crops, measured intervals, rules, and guide...")
            sounds_value = (audio.get("effects") or {}).get("events", []) if audio.get("has_audio") else []
            sheets = assemble_sheets(frames_value, cuts_value, sounds_value, output_value / "contact-sheets", W, H, font_value, font_p)
            conf = assemble_audit(cuts_value, extra, output_value / "review-cuts.jpg", W, H, font_p)
            cutouts = save_cutouts(texts_value, frames_value, output_value / "crops")
            paper_1s: dict[int, float] = {}
            if texts_value:
                for e in (json.loads((measurements / "screen_text.json").read_text(encoding="utf-8")).get('samples', [])):
                    s = int(math.floor(e["t"]))
                    paper_1s[s] = max(paper_1s.get(s, 0.0), float(e.get("paper", 0)))
            plans_edges = [c["t"] for c in cuts_value]
            write_intervals_measured(output_value / FILE_INTERVALS, dur, frames_value, faces_1s, cuts_value, plans_edges, texts_value,
                                     audio, words_value, (times_q, frame_scores), brightness_1s, paper_1s)
            rules_value = assemble_rules(worksheet, cuts_value, pacing, pacing_all, framing, texts_value or {}, colors_value, audio, speech_value, args)
            save_json_value(output_value / FILE_RULES, rules_value)
            files_value = {
                "style-guide.md": "this guide",
                FILE_RULES: 'machine-readable measurements with suggested edit_data.json and HyperFrames settings',
                FILE_INTERVALS: 'one second per row: shot, cut, visible content, on-screen text, and sound',
                "contact-sheets/": f'{len(sheets)} contact sheets, one frame per second, marked cuts and effects',
            }
            if conf:
                files_value["review-cuts*.jpg"] = "frames before and after each detected cut"
            if cutouts:
                files_value["crops/"] = f'{len(cutouts)} caption and static-text crops at working size'
            files_value["measurements/"] = 'raw frame scene scores, cuts, audio, recognized text, and transcription'
            write_guide(output_value / "style-guide.md", worksheet, cuts_value, pacing, pacing_all, framing, texts_value, colors_value, audio,
                          speech_value, rules_value, files_value, args, missing)
    except MeasurementError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1

    seg = time.time() - start_value
    announce("")
    announce(f'Completed in {format_number(seg, 0)} s. Folder: {output_value}')
    announce(f"Cuts: {pacing['cuts']} ({format_number(pacing['cuts_per_minute'])} per minute; average shot {format_number(pacing['mean_shot_s'], 2)} s)"
          + (f"; another {len(cuts_value) - pacing['cuts']} tentative detections to inspect" if len(cuts_value) > pacing["cuts"] else ""))
    if texts_value and texts_value.get('has_captions'):
        announce(f"Captions: center at {pct(texts_value['center_y'])} of frame height, {format_number(texts_value['median_words_per_line'], 0)} words per line, line height {texts_value['standard_line_height']} pixels on the standard canvas {texts_value['standard_canvas']}")
    if audio.get("has_audio"):
        announce(f"Audio: {format_number(audio['loudness'].get('integrated_lufs'))} LUFS; {audio['music']['verdict']}; {audio['effects']['probable_count']} probable effects")
    announce('Inspect contact sheets and review crops, then complete the visual-review section.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
