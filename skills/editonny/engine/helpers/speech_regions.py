"""Acoustic speech/silence intervals of a video's audio.

Speech = complement of the silences found by ffmpeg `silencedetect`. This is the
source of truth for cut EDGES: Whisper word times drift, these do not.

Phone recordings carry room noise, so a fixed threshold is wrong for most files.
`--noise auto` (default) measures the audio in 50 ms windows, finds the noise
floor, and sets the threshold just above the PEAKS of that noise. silencedetect
compares every single sample against the threshold, so it has to clear the
noise peaks, not the noise RMS. The threshold is also kept a safe distance below
the typical speech level.

This module also holds the audio utilities shared by transcribe.py and
voice_levels.py (decode, channel choice, noise estimate, silencedetect).

Usage:
    uv run python helpers/speech_regions.py <video> [--edit-dir DIR] [--noise auto|-35]
        [--min-silence 0.25] [--start S --end E] [--json]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from common import file_signature, load_json, probe_video, require_binary, save_json

SAMPLE_RATE = 16000
WINDOW_S = 0.05            # analysis window for the noise estimate
MIN_SPEECH_S = 0.06        # speech islands shorter than this (clicks, taps) become silence
DEFAULT_NOISE_DB = -35.0   # used only when the audio is too short to measure
SILENT_CHANNEL_DB = 20.0   # a channel this far below the loudest one counts as mute
PHASE_DROP_DB = 6.0        # mix this far below the best channel = phase cancellation


class AudioError(RuntimeError):
    """Readable (PT-BR) problem with a file's audio."""


# ---------------------------------------------------------------- decoding

def probe_average(path: Path | str) -> dict:
    """common.probe_video with a readable error for files ffprobe cannot open."""
    require_binary("ffprobe")
    try:
        return probe_video(path)
    except (RuntimeError, ValueError) as exc:
        raise AudioError(f'could not read file; verify that it is valid media: {path}') from exc


def decode_audio(path: Path | str, sr: int = SAMPLE_RATE, start: float | None = None,
                 end: float | None = None, af: str | None = None) -> np.ndarray:
    """Decode the first audio track to int16, shape (samples, channels)."""
    require_binary("ffmpeg")
    info = probe_average(path)
    if not info.get("has_audio"):
        raise AudioError(f'file has no audio track: {path}')
    channels = max(1, int(info.get("audio_channels") or 1))
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None and start > 0:
        cmd += ["-ss", f'{start:.3f}']
    cmd += ["-i", str(path)]
    if end is not None:
        dur = end - (start or 0.0)
        if dur <= 0:
            raise AudioError(f'empty interval: start {start} and end {end}')
        cmd += ["-t", f'{dur:.3f}']
    cmd += ["-map", "0:a:0", "-vn", "-ac", str(channels), "-ar", str(sr)]
    if af:
        cmd += ["-af", af]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-800:]
        raise AudioError(f'FFmpeg could not read audio from {path}:\n{tail}')
    data = np.frombuffer(proc.stdout, dtype=np.int16)
    usable = (len(data) // channels) * channels
    return data[:usable].reshape(-1, channels)


def _rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    xf = x.astype(np.float64) / 32768.0
    return float(10.0 * np.log10(np.mean(xf * xf) + 1e-12))


def choose_mono(data: np.ndarray) -> tuple[np.ndarray, dict]:
    """Pick how to fold the channels to mono.

    Measures each channel and the mix of the audible ones. A mute channel is left
    out; if the mix is more than 6 dB below the best channel (one mic in inverted
    phase cancels the other), only the best channel is used.
    Returns float32 mono in [-1, 1] and a description dict.
    """
    n_ch = data.shape[1] if data.ndim == 2 else 1
    if n_ch == 1:
        mono = data.reshape(-1).astype(np.float32) / 32768.0
        return mono, {"channels": 1, "used": "mono", "levels_db": [round(_rms_db(data), 1)]}
    levels = [_rms_db(data[:, c]) for c in range(n_ch)]
    best = int(np.argmax(levels))
    names = ["left", "right"] if n_ch == 2 else [f'ch{c}' for c in range(n_ch)]
    audible = [c for c in range(n_ch) if levels[c] >= levels[best] - SILENT_CHANNEL_DB]
    mix = np.zeros(data.shape[0], dtype=np.float32)
    for c in audible:
        mix += data[:, c].astype(np.float32)
    mix /= (32768.0 * len(audible))
    mix_db = float(10.0 * np.log10(np.mean(mix.astype(np.float64) ** 2) + 1e-12))
    desc = {"channels": n_ch, "levels_db": [round(v, 1) for v in levels], "mix_db": round(mix_db, 1)}
    if levels[best] - mix_db > PHASE_DROP_DB:
        desc.update(used=names[best], reason="phase inversion between channels")
        return data[:, best].astype(np.float32) / 32768.0, desc
    if len(audible) == 1:
        desc.update(used=names[best], reason="canal mudo")
        return data[:, best].astype(np.float32) / 32768.0, desc
    desc["used"] = "mix" if len(audible) == n_ch else "mix:" + ",".join(names[c] for c in audible)
    return mix, desc


def load_mono(path: Path | str, sr: int = SAMPLE_RATE, start: float | None = None,
              end: float | None = None, af: str | None = None) -> tuple[np.ndarray, dict]:
    return choose_mono(decode_audio(path, sr=sr, start=start, end=end, af=af))


# ---------------------------------------------------------------- noise estimate

def window_levels(mono: np.ndarray, sr: int = SAMPLE_RATE, win_s: float = WINDOW_S) -> tuple[np.ndarray, np.ndarray]:
    """Per-window RMS dB and peak dB (non-overlapping windows)."""
    w = max(1, int(round(win_s * sr)))
    n = len(mono) // w
    if n == 0:
        return np.zeros(0), np.zeros(0)
    blocks = mono[: n * w].reshape(n, w).astype(np.float64)
    rms_db = 10.0 * np.log10(np.mean(blocks * blocks, axis=1) + 1e-12)
    peak_db = 20.0 * np.log10(np.max(np.abs(blocks), axis=1) + 1e-6)
    return rms_db, peak_db


def _long_runs(mask: np.ndarray, min_len: int) -> np.ndarray:
    """Keep only True runs at least min_len long."""
    out = np.zeros_like(mask)
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_len:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def estimate_noise(mono: np.ndarray, sr: int = SAMPLE_RATE) -> dict:
    """Estimate the silencedetect threshold for this audio.

    floor      = 10th percentile of 50 ms RMS (ignoring digital silence)
    noise      = windows within 6 dB of the floor, in runs of >= 200 ms
    threshold  = 90th percentile of the noise PEAKS + 3 dB,
                 never closer than 6 dB to the median speech level.
    """
    rms_db, peak_db = window_levels(mono, sr)
    out = {"floor_db": None, "noise_peak_db": None, "speech_db": None, "threshold_db": DEFAULT_NOISE_DB}
    valid = rms_db > -90.0
    if valid.sum() < 8:
        out["threshold_db"] = -50.0 if len(rms_db) >= 8 else DEFAULT_NOISE_DB
        return out
    floor = float(np.percentile(rms_db[valid], 10))
    loud = valid & (rms_db >= floor + 15.0)
    speech = float(np.median(rms_db[loud])) if loud.sum() >= 4 else None
    quiet = valid & (rms_db <= floor + 6.0)
    noise = _long_runs(quiet, 4)
    if noise.sum() >= 4:
        noise_peak = float(np.percentile(peak_db[noise], 90))
        thr = noise_peak + 3.0
        if speech is not None:
            thr = min(thr, speech - 6.0)
        out["noise_peak_db"] = round(noise_peak, 1)
    else:
        thr = (speech - 18.0) if speech is not None else floor + 10.0
    out.update(floor_db=round(floor, 1), speech_db=None if speech is None else round(speech, 1),
               threshold_db=round(float(np.clip(thr, -70.0, -12.0)), 1))
    return out


# ---------------------------------------------------------------- silencedetect

_RE_START = re.compile(r"silence_start:\s*(-?[\d.]+(?:e-?\d+)?)")
_RE_END = re.compile(r"silence_end:\s*(-?[\d.]+(?:e-?\d+)?)")


def detect_silences(mono: np.ndarray, noise_db: float, min_silence: float, sr: int = SAMPLE_RATE) -> list[list[float]]:
    """Run ffmpeg silencedetect on a float32 mono array. Times relative to the array start.

    A silence still open at the end of the audio (no silence_end) is closed at the end.
    """
    require_binary("ffmpeg")
    duration = len(mono) / sr
    if len(mono) == 0:
        return []
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-f", "f32le", "-ar", str(sr), "-ac", "1",
           "-i", "pipe:0", "-af", f'silencedetect=noise={noise_db:.1f}dB:d={min_silence:.3f}',
           "-f", "null", "-"]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(mono, dtype=np.float32).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise AudioError("silencedetect failed:\n" + proc.stderr.decode("utf-8", "replace")[-800:])
    silences: list[list[float]] = []
    open_start: float | None = None
    for line in proc.stderr.decode("utf-8", "replace").splitlines():
        m = _RE_START.search(line)
        if m:
            open_start = min(max(0.0, float(m.group(1))), duration)
            continue
        m = _RE_END.search(line)
        if m and open_start is not None:
            silences.append([open_start, min(float(m.group(1)), duration)])
            open_start = None
    if open_start is not None and duration - open_start >= min_silence * 0.5:
        silences.append([open_start, duration])
    return _clean_silences(silences, duration)


def _clean_silences(silences: list[list[float]], duration: float) -> list[list[float]]:
    """Sort, merge overlaps, and absorb speech islands shorter than MIN_SPEECH_S."""
    merged: list[list[float]] = []
    for s, e in sorted(silences):
        if e <= s:
            continue
        if merged and s - merged[-1][1] < MIN_SPEECH_S:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if merged and merged[0][0] < MIN_SPEECH_S:
        merged[0][0] = 0.0
    if merged and duration - merged[-1][1] < MIN_SPEECH_S:
        merged[-1][1] = duration
    return merged


def complement(intervals: list[list[float]], start: float, end: float) -> list[list[float]]:
    out: list[list[float]] = []
    t = start
    for s, e in intervals:
        if s > t:
            out.append([t, s])
        t = max(t, e)
    if t < end:
        out.append([t, end])
    return out


def _r(x: float) -> float:
    return round(float(x), 3)


def regions_from_mono(mono: np.ndarray, noise: str | float = "auto", min_silence: float = 0.25,
                      offset: float = 0.0, sr: int = SAMPLE_RATE, noise_mono: np.ndarray | None = None,
                      total: float | None = None) -> dict:
    """Silences and speech for an already decoded mono array. Times get `offset` added.

    `total` is the container duration when it is a little longer than the decoded audio:
    a trailing silence is stretched to it and speech is measured up to it.
    """
    est = None
    if isinstance(noise, str):
        est = estimate_noise(noise_mono if noise_mono is not None else mono, sr)
        noise_db = est["threshold_db"]
    else:
        noise_db = float(noise)
    audio_len = len(mono) / sr
    duration = float(total) if total else audio_len
    sil = [[min(a, duration), min(b, duration)] for a, b in detect_silences(mono, noise_db, min_silence, sr)]
    sil = [x for x in sil if x[1] > x[0]]
    if sil and sil[-1][1] >= min(audio_len, duration) - 0.02:
        sil[-1][1] = duration
    speech = complement(sil, 0.0, duration)
    return {
        "noise_db": noise_db,
        "noise_mode": "auto" if est else "manual",
        "noise_estimate": est,
        "min_silence": min_silence,
        "speech": [[_r(s + offset), _r(e + offset)] for s, e in speech],
        "silences": [[_r(s + offset), _r(e + offset)] for s, e in sil],
    }


def regions_path(edit_dir: Path | str, stem: str) -> Path:
    return Path(edit_dir) / "regions" / f'{stem}.json'


def compute_regions(video: Path | str, noise: str | float = "auto", min_silence: float = 0.25,
                    start: float | None = None, end: float | None = None,
                    edit_dir: Path | str | None = None) -> dict:
    """Speech-region dict for a video (or a [start, end] part of it, times stay absolute).

    For a part with --noise auto, the threshold comes from the whole file (a short
    part is a bad sample of the noise); a cached regions JSON is reused for that.
    """
    video = Path(video).absolute()
    info = probe_average(video)
    duration = float(info.get("duration") or 0.0)
    partial = start is not None or end is not None
    s0 = max(0.0, start or 0.0)
    s1 = min(duration, end) if end is not None else duration
    if partial and s1 <= s0:
        raise AudioError(f'invalid interval: {s0:.2f} seconds to {s1:.2f} seconds (duration {duration:.2f}s)')
    mono, audio_desc = load_mono(video, start=s0 if partial else None, end=s1 if partial else None)
    noise_arg: str | float = noise
    if partial and isinstance(noise, str):
        cached = load_json(regions_path(edit_dir, video.stem)) if edit_dir else None
        if cached and cached.get("source_sig") == file_signature(video) and cached.get("noise_mode") == "auto":
            noise_arg = float(cached["noise_db"])
        else:
            full, _ = load_mono(video)
            noise_arg = estimate_noise(full)["threshold_db"]
    reg = regions_from_mono(mono, noise_arg, min_silence, offset=s0 if partial else 0.0,
                            total=(s1 - s0) if partial else duration)
    if partial and isinstance(noise, str):
        reg["noise_mode"] = "auto"
    out = {
        "source": str(video),
        "source_sig": file_signature(video),
        "stem": video.stem,
        "duration": _r(duration),
        "noise_db": reg["noise_db"],
        "noise_mode": reg["noise_mode"],
        "min_silence": min_silence,
        "speech": reg["speech"],
        "silences": reg["silences"],
        "audio": audio_desc,
    }
    if reg["noise_estimate"]:
        out["noise_estimate"] = reg["noise_estimate"]
    if partial:
        out["start"], out["end"] = _r(s0), _r(s1)
    return out


def load_or_compute_regions(video: Path | str, edit_dir: Path | str | None = None,
                            min_silence: float = 0.25) -> dict:
    """Cached auto regions for a whole file (edit/regions/<stem>.json), computed if missing or stale."""
    video = Path(video).absolute()
    edit_dir = Path(edit_dir) if edit_dir else video.parent / "edit"
    path = regions_path(edit_dir, video.stem)
    cached = load_json(path)
    if (cached and cached.get("source_sig") == file_signature(video) and cached.get("noise_mode") == "auto"
            and abs(float(cached.get("min_silence", -1)) - min_silence) < 1e-9 and "start" not in cached):
        return cached
    reg = compute_regions(video, "auto", min_silence)
    save_json(path, reg)
    return reg


# ---------------------------------------------------------------- CLI

def _parse_noise(value: str) -> str | float:
    if value.strip().lower() == "auto":
        return "auto"
    try:
        v = float(value.lower().replace("db", ""))
    except ValueError:
        raise argparse.ArgumentTypeError("use 'auto' or a numeric dB threshold such as -35")
    return v if v <= 0 else -v


def print_regions(reg: dict) -> None:
    speech_total = sum(e - s for s, e in reg["speech"])
    est = reg.get("noise_estimate") or {}
    how = f"automatic, noise {est['floor_db']} dB" if reg["noise_mode"] == "auto" and est.get("floor_db") is not None else reg["noise_mode"]
    span = f"{reg['start']:.2f}-{reg['end']:.2f}s" if "start" in reg else f"{reg['duration']:.2f}s"
    print(f"{reg['stem']} ({span}) · threshold {reg['noise_db']} dB ({how}) · minimum pause {reg['min_silence']}s · {len(reg['speech'])} speech intervals, {speech_total:.1f}s of speech")
    items = [("speech", s, e) for s, e in reg["speech"]] + [("pause ", s, e) for s, e in reg["silences"]]
    for kind, s, e in sorted(items, key=lambda x: x[1]):
        print(f'  {kind} [{s:06.2f}-{e:06.2f}] {e - s:5.2f}s')


def main() -> None:
    ap = argparse.ArgumentParser(
        description='Measure speech and pause intervals using FFmpeg silencedetect.')
    ap.add_argument('video', type=Path, help='video or audio file')
    ap.add_argument("--edit-dir", type=Path, default=None, help='edit folder (default: <video folder>/edit)', dest='edit_dir')
    ap.add_argument("--noise", type=_parse_noise, default="auto",
                    help='silence threshold: auto measures the source noise; alternatively provide dB such as -35', dest='noise')
    ap.add_argument("--min-silence", type=float, default=0.25, help='minimum pause in seconds (default 0.25)', dest='min_silence')
    ap.add_argument("--start", type=float, default=None, help='optional interval start in seconds', dest='start')
    ap.add_argument("--end", type=float, default=None, help="optional interval end in seconds", dest='end')
    ap.add_argument("--json", action="store_true", help="print JSON instead of the list", dest='json')
    args = ap.parse_args()

    video = args.video.absolute()
    if not video.exists():
        sys.exit(f'file not found: {video}')
    if args.min_silence <= 0:
        sys.exit('--min-silence must be positive')
    edit_dir = (args.edit_dir or video.parent / "edit").absolute()
    try:
        reg = compute_regions(video, args.noise, args.min_silence, args.start, args.end, edit_dir=edit_dir)
    except AudioError as exc:
        sys.exit(str(exc))
    partial = args.start is not None or args.end is not None
    if not partial:
        save_json(regions_path(edit_dir, video.stem), reg)
    if args.json:
        import json
        print(json.dumps(reg, ensure_ascii=False, indent=2))
    else:
        print_regions(reg)
        if partial:
            print('  partial interval: not saved in regions/; only complete-file measurements are cached')
        else:
            print(f'  saved at {regions_path(edit_dir, video.stem)}')


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
