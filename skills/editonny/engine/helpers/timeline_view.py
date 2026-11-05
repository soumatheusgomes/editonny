# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
"""Filmstrip + waveform + words for one time range of a video, as a PNG.

Usage:
    uv run python helpers/timeline_view.py <video> <start> <end> [-o PNG] [--transcript JSON] [--frames N]

- Waveform: peak envelope (light) and RMS (strong) of the audio in the range.
- Silence bands (blue): from edit/regions/<stem>.json when present, else measured here
  (RMS under -35 dBFS for at least 0.25 s).
- Words: from --transcript, else edit/transcripts/<stem>.json (cut.mp4 -> transcripts/cut.json).
- On a cut with a timeline next to it (cut_timeline.json): segment boundaries (yellow lines with
  ids) and J-cut audio overlaps (orange bands, where the incoming audio starts early).
Default output: edit/verify/timeline_<stem>_<start>-<end>.png
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from common import file_signature, fmt_time, load_json, probe_video
from contact_sheet import (ACCENT, BG, DIM, FG, PANEL, abspath, default_verify_dir, extract_frames, find_timeline,
                           parse_time, ui_font)

WAVE_PEAK = (96, 128, 190)
WAVE_RMS = (150, 190, 255)
SILENCE = (40, 90, 170, 70)
JCUT = (255, 140, 40, 90)
ANALYSIS_SR = 16000


def load_audio(video: Path, start: float, end: float) -> np.ndarray:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-ss", f'{start:.4f}', "-i", str(video), "-t", f'{end - start:.4f}',
         "-vn", "-ac", "1", "-ar", str(ANALYSIS_SR), "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32)


def measured_silences(pcm: np.ndarray, start: float, noise_db: float = -35.0, min_len: float = 0.25) -> list:
    win = int(0.02 * ANALYSIS_SR)
    if pcm.size < win:
        return []
    n = pcm.size // win
    rms = np.sqrt(np.mean(pcm[: n * win].reshape(n, win) ** 2, axis=1))
    quiet = 20 * np.log10(np.maximum(rms, 1e-9)) < noise_db
    out, i = [], 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * 0.02 >= min_len:
                out.append([start + i * 0.02, start + j * 0.02])
            i = j
        else:
            i += 1
    return out


def resolve_transcript(video: Path, explicit: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.exists() else None
    cands = []
    if video.parent.name == "edit" or (video.parent / "transcripts").is_dir():
        cands.append(video.parent / "transcripts" / f'{video.stem}.json')
    cands.append(video.parent / "edit" / "transcripts" / f'{video.stem}.json')
    return next((c for c in cands if c.exists()), None)


def resolve_regions(video: Path) -> dict | None:
    for c in (video.parent / "edit" / "regions" / f'{video.stem}.json', video.parent / "regions" / f'{video.stem}.json'):
        data = load_json(c)
        if isinstance(data, dict) and isinstance(data.get("silences"), list):
            return data
    return None


def matches_video(data: dict, video: Path, duration: float) -> bool:
    """A regions/transcript JSON describes this exact file (a re-rendered cut.mp4 keeps its name)."""
    sig = data.get("source_sig")
    if sig:
        try:
            return sig == file_signature(video)
        except OSError:
            return False
    if data.get("duration") is not None and duration:
        return abs(float(data["duration"]) - duration) <= 0.05
    return True


def nice_step(span: float, target_ticks: int = 10) -> float:
    for step in (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
        if span / step <= target_ticks:
            return step
    return 600


def render(video: Path, start: float, end: float, out: Path, transcript: Path | None, n_frames: int) -> Path:
    info = probe_video(video)
    dur = info.get("duration") or end
    end = min(end, dur)
    span = end - start
    W = 1800
    pad = 40
    x0, x1 = pad, W - pad
    inner = x1 - x0

    def tx(t: float) -> float:
        return x0 + (t - start) / span * inner

    vertical = (info.get("display_height") or 0) > (info.get("display_width") or 0)
    if n_frames <= 0:
        n_frames = 12 if vertical else 7
    gap = 6
    tile_w = int((inner - gap * (n_frames - 1)) / n_frames)
    tile_w -= tile_w % 2
    times = [start + (i + 0.5) * span / n_frames for i in range(n_frames)]
    frames = extract_frames(video, times, width=tile_w) if info.get("has_video") else []
    tile_h = frames[0].size[1] if frames else 0

    strip_y = 64
    wave_y = strip_y + tile_h + (34 if frames else 0) + 44
    wave_h = 260
    words_y = wave_y + wave_h + 14
    ruler_y = words_y + 62
    H = ruler_y + 70

    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas, "RGBA")
    f_head, f_lab, f_word, f_small = ui_font(26, bold=True), ui_font(15), ui_font(17), ui_font(14)
    d.text((pad, 18), f'{video.name}  ·  {fmt_time(start)} to {fmt_time(end)}  ({span:.2f}s)', fill=FG, font=f_head)

    for i, (img, t) in enumerate(zip(frames, times)):
        x = x0 + i * (tile_w + gap)
        canvas.paste(img, (x, strip_y))
        d.rectangle((x, strip_y + tile_h, x + tile_w - 1, strip_y + tile_h + 28), fill=PANEL)
        d.text((x + 6, strip_y + tile_h + 6), fmt_time(t), fill=DIM, font=f_small)

    # waveform panel
    d.rectangle((x0, wave_y, x1, wave_y + wave_h), fill=(28, 28, 34))
    pcm = load_audio(video, start, end) if info.get("has_audio") else np.zeros(0, dtype=np.float32)

    regions = resolve_regions(video)
    stale_notes = []
    if regions and not matches_video(regions, video, dur):
        regions = None
        stale_notes.append('regions are stale because the video changed')
    if regions:
        silences = [[float(a), float(b)] for a, b in regions["silences"] if float(b) > start and float(a) < end]
        sil_note = 'measured silence from regions'
    else:
        silences = measured_silences(pcm, start)
        sil_note = 'locally measured silence: -35 dB, 0.25 seconds'
    for a, b in silences:
        d.rectangle((tx(max(a, start)), wave_y, tx(min(b, end)), wave_y + wave_h), fill=SILENCE)

    timeline = None
    has_overlap = False
    tl_path = find_timeline(video)
    if tl_path and (video.parent / tl_path.name).exists():
        timeline = load_json(tl_path)
    if timeline:
        for s in timeline.get("segments") or []:
            a0, a1 = float(s.get("audio_out_start", s["out_start"])), float(s["out_start"])
            if a1 - a0 > 1e-6 and a1 > start and a0 < end:
                has_overlap = True
                d.rectangle((tx(max(a0, start)), wave_y, tx(min(a1, end)), wave_y + wave_h), fill=JCUT)

    mid = wave_y + wave_h / 2
    amp = wave_h / 2 - 10
    if pcm.size:
        cols = inner
        per = max(1, pcm.size // cols)
        usable = (pcm.size // per) * per
        blocks = pcm[:usable].reshape(-1, per)
        peak = np.abs(blocks).max(axis=1)
        rms = np.sqrt((blocks ** 2).mean(axis=1))
        scale = max(float(peak.max()), 1e-4)
        scale = max(scale, 0.25)   # keep quiet audio looking quiet
        for i in range(blocks.shape[0]):
            x = x0 + i * inner / blocks.shape[0]
            hp = peak[i] / scale * amp
            hr = rms[i] / scale * amp
            d.line((x, mid - hp, x, mid + hp), fill=WAVE_PEAK)
            d.line((x, mid - hr, x, mid + hr), fill=WAVE_RMS)
    d.line((x0, mid, x1, mid), fill=(70, 70, 80))

    if timeline:
        for s in timeline.get("segments") or []:
            t = float(s["out_start"])
            if start <= t <= end:
                d.line((tx(t), strip_y - 6, tx(t), words_y + 44), fill=ACCENT, width=2)
                d.text((tx(t) + 5, wave_y + 6), f"{s['id']} {s.get('beat') or ''}".strip(), fill=ACCENT, font=f_lab)

    # words: two alternating rows, skip labels that would collide
    tr_path = resolve_transcript(video, transcript)
    words = []
    if tr_path:
        data = load_json(tr_path) or {}
        # An explicit --transcript of another file is trusted; one made from this path must match the file.
        own = transcript is None or str(data.get("source") or "") == str(video)
        if own and not matches_video(data, video, dur):
            stale_notes.append('transcript is stale because the video changed; transcribe again')
            data = {}
        words = [w for w in data.get("words", []) if w.get("end", 0) > start and w.get("start", 0) < end]
    last_x = [-1e9, -1e9]
    for w in words:
        ws, we = max(float(w["start"]), start), min(float(w["end"]), end)
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        xa, xb = tx(ws), tx(we)
        d.line((xa, wave_y + wave_h, xa, wave_y + wave_h + 8), fill=DIM)
        d.rectangle((xa, wave_y + wave_h - 4, max(xa + 1, xb), wave_y + wave_h - 1), fill=(200, 200, 210, 120))
        tw = d.textlength(text, font=f_word)
        row = 0 if xa > last_x[0] + 6 else 1 if xa > last_x[1] + 6 else None
        if row is None:
            continue
        d.text((xa, words_y + row * 24), text, fill=FG, font=f_word)
        last_x[row] = xa + tw

    step = nice_step(span)
    t = (int(start / step) + (1 if start % step > 1e-6 else 0)) * step
    while t <= end + 1e-6:
        x = tx(t)
        d.line((x, ruler_y, x, ruler_y + 8), fill=DIM)
        label = fmt_time(t)
        lx = min(max(x - d.textlength(label, font=f_small) / 2, 4), W - 4 - d.textlength(label, font=f_small))
        d.text((lx, ruler_y + 12), label, fill=DIM, font=f_small)
        t += step
    legend = f'azul = {sil_note}'
    if timeline:
        legend += '  ·  yellow = interval start'
    if has_overlap:
        legend += "  ·  amber = next interval's early audio entry: J-cut"
    if not words and not stale_notes:
        legend += '  ·  no transcript'
    for note in stale_notes:
        legend += f'  ·  {note}'
    d.text((pad, ruler_y + 40), legend, fill=DIM, font=f_small)
    for note in stale_notes:
        print(f'notice: {note}', file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='Create a PNG with frames, waveform, and words for a video interval.')
    ap.add_argument('video', type=Path)
    ap.add_argument('start', type=parse_time, help='start in seconds or mm:ss.cc')
    ap.add_argument('end', type=parse_time, help="end (seconds or mm:ss.cc)")
    ap.add_argument("-o", "--output", type=Path, help='output PNG (default: edit/verify/timeline_<name>_<start>-<end>.png)', dest='output')
    ap.add_argument("--transcript", type=Path, help='transcript JSON (default: edit/transcripts/<name>.json)', dest='transcript')
    ap.add_argument("--frames", type=int, default=0, help='frame count in the strip; automatic by default', dest='frames')
    args = ap.parse_args()

    video = abspath(args.video)
    if not video.exists():
        print(f'error: video not found: {video}', file=sys.stderr)
        return 2
    if args.end <= args.start:
        print('error: end must exceed start', file=sys.stderr)
        return 2
    try:
        info = probe_video(video)
    except Exception as exc:  # noqa: BLE001
        print(f'error: could not read {video.name}: {exc}', file=sys.stderr)
        return 2
    if args.start >= (info.get("duration") or 0):
        print(f"error: start {args.start:.2f} seconds exceeds video duration ({info.get('duration', 0):.2f}s)", file=sys.stderr)
        return 2
    out = args.output or default_verify_dir(video) / f'timeline_{video.stem}_{args.start:.2f}-{args.end:.2f}.png'
    try:
        path = render(video, args.start, args.end, abspath(out), args.transcript, args.frames)
    except RuntimeError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(f'image saved: {path}')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
