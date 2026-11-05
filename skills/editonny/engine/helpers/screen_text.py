"Measure existing on-screen content so captions neither duplicate nor cover it.\n\nSample video at two frames per second by default. Identify occupied vertical bands, existing burned-in caption intervals, and light fullscreen graphics where white captions would disappear.\n\nThe macOS Vision backend requires Apple's command-line tools and reads actual words, allowing comparison with speech. The cross-platform OpenCV heuristic backend detects text-like regions without reading them and estimates existing captions from their position and changes over time. Clearly distinguish measured reading from heuristic inference.\n\nUsage: uv run python helpers/screen_text.py --video cut.mp4 --edit-dir EDIT --fps 2 --json\nUse --transcript to supply word timing, --backend to choose an available reader, and --force to refresh measurements. Preserve source language rather than assuming all text is English."

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (edit_dir_for, file_signature, load_json, now_iso, probe_video,  # noqa: E402
                    require_binary, run, save_json)

VERSION = 1
# Candidate bands expressed as frame-height fractions; captions are centered within a band.
BANDS: list[tuple[str, float, float]] = [
    ("captions", 0.655, 0.760),   # Default: chest height, below the face.
    ("high", 0.525, 0.630),
    ("low", 0.780, 0.885),
    ("footer", 0.865, 0.960),
]
PAPER_MIN = 0.72        # Minimum fraction of bright neutral pixels for a screen to count as a light graphic.
INK_FREE = 0.02      # Maximum ink coverage in a paper band suitable for dark captions.
CONF_MIN = 0.45
WINDOW_SPEECH = 1.5       # Timing tolerance in seconds when comparing written and spoken text.


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _normal(text_value: str) -> str:
    return unicodedata.normalize("NFD", text_value.lower()).encode("ascii", "ignore").decode()


def _bin_dir() -> Path:
    d = Path.home() / ".editonny" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def vision_available() -> bool:
    return sys.platform == "darwin" and shutil.which("swiftc") is not None


def compile_vision(source: Path | None = None) -> Path | None:
    'Compile the Apple text reader once and return its executable path.'
    if not vision_available():
        return None
    source = source or Path(__file__).resolve().parent / "assets_ocr" / "ocr_vision.swift"
    if not source.exists():
        return None
    target_value = _bin_dir() / "ocr_vision"
    if target_value.exists() and target_value.stat().st_mtime >= source.stat().st_mtime:
        return target_value
    try:
        run(["swiftc", "-O", str(source), "-o", str(target_value)])
    except (subprocess.CalledProcessError, OSError):
        return None
    return target_value if target_value.exists() else None


def extract_frames(video: Path, destination: Path, fps: float, width_value: int = 608) -> list[float]:
    require_binary("ffmpeg")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", f'fps={fps},scale={width_value}:-2', "-q:v", "3", str(destination / "f_%05d.jpg")])
    names_value = sorted(destination.glob("f_*.jpg"))
    return [round(i / fps, 3) for i in range(len(names_value))]


def _read_vision(binary: Path, directory: Path) -> dict[str, list[dict]]:
    output_value = directory / "ocr.json"
    run([str(binary), str(directory), str(output_value)])
    return json.loads(output_value.read_text(encoding='utf-8'))


def _read_heuristic(directory: Path) -> dict[str, list[dict]]:
    import cv2  # noqa: PLC0415 — imported only when this engine is selected
    import numpy as np  # noqa: PLC0415

    out: dict[str, list[dict]] = {}
    for file_value in sorted(directory.glob("f_*.jpg")):
        img = cv2.imread(str(file_value), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        h, w = img.shape
        grad = cv2.morphologyEx(img, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        line_value = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (21, 3)))
        cont, _ = cv2.findContours(line_value, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        items_value: list[dict] = []
        for c in cont:
            x, y, cw, ch = cv2.boundingRect(c)
            if ch < h * 0.012 or ch > h * 0.12 or cw < ch * 1.2 or cw > w * 0.96:
                continue
            density = float(np.count_nonzero(bw[y:y + ch, x:x + cw])) / max(1, cw * ch)
            if not 0.08 < density < 0.62:
                continue
            items_value.append({"t": "", "c": 0.5, "x": x / w, "y": 1 - (y + ch) / h, "w": cw / w, "h": ch / h})
        out[file_value.name] = items_value
    return out


def _paper_and_ink(directory: Path, names_value: list[str]) -> tuple[list[float], list[dict[str, float]]]:
    from PIL import Image  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    paper: list[float] = []
    ink_value: list[dict[str, float]] = []
    for name_value in names_value:
        im = np.asarray(Image.open(directory / name_value).convert("RGB")).astype(int)
        sat = im.max(2) - im.min(2)
        light_value = im.min(2)
        paper.append(round(float(((light_value > 205) & (sat < 40)).mean()), 3))
        h = im.shape[0]
        track_value: dict[str, float] = {}
        for name_track, a, b in BANDS:
            block_value = im[int(a * h):int(b * h)]
            s = block_value.max(2) - block_value.min(2)
            c = block_value.min(2)
            track_value[name_track] = round(float(((c < 200) | (s > 45)).mean()), 3)
        ink_value.append(track_value)
    return paper, ink_value


def _intervals(marks_value: list[tuple[float, bool]], minimum_value: float = 0.6,
                join_value: float = 2.5, dilate: int = 1) -> list[list[float]]:
    if dilate:
        live = [i for i, (_, v) in enumerate(marks_value) if v]
        marks_value = [(t, any(abs(i - j) <= dilate for j in live)) for i, (t, v) in enumerate(marks_value)]\
            if live else marks_value
    intervals: list[list[float]] = []
    open_value: list[float] | None = None
    for t, v in marks_value:
        if v and open_value is None:
            open_value = [t, t]
        elif v:
            open_value[1] = t
        elif open_value is not None:
            intervals.append(open_value)
            open_value = None
    if open_value is not None:
        intervals.append(open_value)
    joined_value: list[list[float]] = []
    for f in intervals:
        if joined_value and f[0] - joined_value[-1][1] <= join_value:
            joined_value[-1][1] = f[1]
        else:
            joined_value.append(f)
    return [f for f in joined_value if f[1] - f[0] >= minimum_value]


def _caption_per_word(samples_value: list[dict], words_value: list[dict]) -> list[tuple[float, bool]]:
    'Existing captions are detected when visible text matches recorded speech.'
    marks_value: list[tuple[float, bool]] = []
    for am in samples_value:
        t = am["t"]
        spoken = {_normal(re.sub(r"\W", "", w.get("word", w.get("text", ""))))
                 for w in words_value
                 if t - WINDOW_SPEECH <= float(w.get("start", w.get("s", -9))) <= t + WINDOW_SPEECH}
        spoken = {p for p in spoken if len(p) >= 3}
        found_value = False
        for it in am["items"]:
            written = {_normal(x) for x in re.findall(r"\w{3,}", it.get("text", ""))}
            if written & spoken:
                found_value = True
                break
        marks_value.append((t, found_value))
    return marks_value


def _caption_per_movement(samples_value: list[dict]) -> list[tuple[float, bool]]:
    'Without speech, detect changing text-shaped regions in the caption area. Both readers provide normalized center coordinates and dimensions; use those keys consistently.'
    marks_value: list[tuple[float, bool]] = []
    previous: tuple[float, float] | None = None
    for am in samples_value:
        target_value = None
        for it in am["items"]:
            if 0.55 <= it["center_y"] <= 0.92 and 0.25 <= it["center_x"] <= 0.75:
                if target_value is None or it["width"] > target_value[0]:
                    target_value = (it["width"], it["height"])
        switch = bool(target_value and previous and abs(target_value[0] - previous[0]) > 0.02)
        marks_value.append((am["t"], switch))
        previous = target_value
    return marks_value


def map_value(video: Path, *, fps: float = 2.0, backend: str = "auto",
           words_value: list[dict] | None = None) -> dict:
    info = probe_video(video)
    width_value, height_value = int(info["width"]), int(info["height"])
    with tempfile.TemporaryDirectory(prefix="screen-text-") as tmp:
        directory = Path(tmp)
        times_value = extract_frames(video, directory, fps)
        names_value = [f'f_{i + 1:05d}.jpg' for i in range(len(times_value))]
        binary = compile_vision() if backend in ("auto", "vision") else None
        if backend == "vision" and binary is None:
            raise RuntimeError('Apple Vision unavailable; macOS with swiftc is required')
        used_value = "vision" if binary else "heuristic"
        raw_value = _read_vision(binary, directory) if binary else _read_heuristic(directory)
        paper, ink_value = _paper_and_ink(directory, names_value)
    samples_value: list[dict] = []
    for i, (t, name_value) in enumerate(zip(times_value, names_value)):
        items_value = []
        for it in raw_value.get(name_value, []):
            if float(it.get("c", 1)) < CONF_MIN:
                continue
            text_value = str(it.get("t", "")).strip()
            center_value = 1 - (it["y"] + it["h"] / 2)          # 0 at the top, 1 at the bottom
            items_value.append({"text": text_value, "center_y": round(center_value, 4),
                          "height": round(it["h"], 4), "center_x": round(it["x"] + it["w"] / 2, 4),
                          "width": round(it["w"], 4)})
        samples_value.append({"t": t, "items": items_value, "paper": paper[i], "ink": ink_value[i]})
    if words_value:
        marks_value = _caption_per_word(samples_value, words_value)
    else:
        marks_value = _caption_per_movement(samples_value)
    doc = {
        "version": VERSION, "created_at": now_iso(), "video": video.name,
        "video_sig": file_signature(video), "backend": used_value, "fps": fps,
        "width": width_value, "height": height_value, "duration": round(float(info["duration"]), 3),
        "bands": [{"name": n, "a": a, "b": b} for n, a, b in BANDS],
        'samples': samples_value,
        'existing_captions': _intervals(marks_value),
        "fullscreen": _intervals([(a["t"], a["paper"] >= PAPER_MIN) for a in samples_value], minimum_value=1.0),
    }
    doc["summary"] = summary_value(doc)
    return doc


def _samples_between(doc: dict, a: float, z: float) -> list[dict]:
    return [s for s in doc['samples'] if a - 0.3 <= s["t"] <= z + 0.3] or\
           [min(doc['samples'], key=lambda s: abs(s["t"] - (a + z) / 2))] if doc['samples'] else []


def covers(intervals: list[list[float]], a: float, z: float, minimum_value: float = 0.45) -> bool:
    if z <= a:
        return False
    remainder = sum(max(0.0, min(z, f[1]) - max(a, f[0])) for f in intervals)
    return remainder > minimum_value * (z - a)


def choose_track(doc: dict, a: float, z: float, *, preferred: str | None = None,
                   margin_value: float = 0.05) -> dict | None:
    'Find a free caption band between the bounds; return None if no band fits.'
    order_value = [n for n, _, _ in BANDS]
    if preferred and preferred in order_value:
        order_value = [order_value[0]] + ([preferred] if preferred != order_value[0] else []) +\
                [n for n in order_value[1:] if n != preferred]
    samples_value = _samples_between(doc, a, z)
    paper = bool(samples_value) and all(s["paper"] >= PAPER_MIN for s in samples_value)
    for name_value in order_value:
        track_value = next(f for f in BANDS if f[0] == name_value)
        lo, hi = track_value[1] - margin_value, track_value[2] + margin_value
        free_value = True
        for s in samples_value:
            if paper:
                if s["ink"].get(name_value, 1.0) > INK_FREE:
                    free_value = False
                    break
            else:
                for it in s["items"]:
                    if it["center_y"] + it["height"] / 2 > lo and it["center_y"] - it["height"] / 2 < hi:
                        free_value = False
                        break
            if not free_value:
                break
        if free_value:
            return {"track": name_value, "center_y": round((track_value[1] + track_value[2]) / 2, 4),
                    "paper": paper, "color": "dark" if paper else "light"}
    return None


def summary_value(doc: dict) -> str:
    caption_count = len(doc['existing_captions'])
    fullscreen_count = len(doc["fullscreen"])
    caption_seconds = sum(b - a for a, b in doc['existing_captions'])
    fullscreen_seconds = sum(b - a for a, b in doc["fullscreen"])
    engine_value = "recognized text (Vision)" if doc["backend"] == "vision" else "text-shaped regions (OpenCV)"
    parts_value = [f'{engine_value}',
              f'{caption_count} intervals already contain on-screen captions ({caption_seconds:.0f}s)',
              f'{fullscreen_count} intervals contain full-screen graphics ({fullscreen_seconds:.0f}s)']
    if doc["backend"] != "vision":
        parts_value.append('words were not recognized; inspect marked intervals before hiding captions')
    return "; ".join(parts_value) + "."


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description='Existing on-screen text for caption avoidance')
    ap.add_argument("--video", type=Path, required=True, dest='video')
    ap.add_argument("--edit-dir", type=Path, dest='edit_dir')
    ap.add_argument("--transcript", type=Path, help='JSON containing spoken words and their start times', dest='transcript')
    ap.add_argument("--fps", type=float, default=2.0, dest='fps')
    ap.add_argument("--backend", choices=["auto", "vision", "heuristic"], default="auto", dest='backend')
    ap.add_argument("--force", action="store_true", dest='force')
    ap.add_argument("--json", action="store_true", dest='json')
    args = ap.parse_args(argv)
    video = args.video.expanduser().absolute()
    if not video.exists():
        print(f'error: not found {video}', file=sys.stderr)
        return 2
    edit_dir = (args.edit_dir.expanduser().absolute() if args.edit_dir else edit_dir_for(video))
    destination = edit_dir / "screen_text.json"
    words_value: list[dict] | None = None
    if args.transcript and args.transcript.exists():
        data_value = load_json(args.transcript)
        words_value = data_value.get("words") if isinstance(data_value, dict) else data_value
    # The cache also belongs to the requested backend and transcript: caption detection reads the words.
    request = {"backend": args.backend, "words": None if words_value is None else
               hashlib.sha256(json.dumps(words_value, sort_keys=True).encode()).hexdigest()}
    if destination.exists() and not args.force:
        old_value = load_json(destination)
        if (old_value.get("video_sig") == file_signature(video) and old_value.get("version") == VERSION
                and abs(float(old_value.get("fps", 0)) - args.fps) < 1e-6 and old_value.get("request") == request):
            print(old_value.get("summary", 'already ready'))
            if args.json:
                print(json.dumps(old_value, ensure_ascii=False, indent=2))
            return 0
    try:
        doc = map_value(video, fps=args.fps, backend=args.backend, words_value=words_value)
    except RuntimeError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    doc["request"] = request
    try:
        save_json(destination, doc)
    except OSError:
        pass
    print(doc["summary"])
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
