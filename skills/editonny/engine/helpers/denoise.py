"""Background-noise removal for the voice: `audio.denoise` in edit_data.json ("off", "light", "strong").

The method was chosen by a measured comparison (afftdn with a fixed floor, anlmdn, noisereduce and a
spectral gate lost to it on clean and on noisy phone speech). It needs only ffmpeg filters that every
build has (highpass, asendcmd, afftdn):

    highpass 80 Hz (2 poles) -> afftdn with a noise profile sampled from the source's own pauses

The noise clip is put in front of the audio, afftdn samples it between `sn start` and `sn stop`
(asendcmd) and the clip is cut off again. afftdn delays the audio by a fixed number of samples (1200
at 48 kHz) and keeps the sample count, so every range is read with that many extra real samples and
trimmed: the voice stays on the same sample. A self-test checks the three filters exist and measures the
delay once per ffmpeg build.

  light:  nr 10, nf = round(noise RMS of the clip + 3 dB), clamped to [-80, -20]
  strong: nr 20, same nf. (afftdn's default nf -50 would silently cancel the sampled profile.)

Noise profile, one per source: the pauses of edit/regions/<stem>.json (only when its source_sig matches;
measured on the fly otherwise), 60 ms trimmed at both ends, longest first, up to 2 s, joined with 5 ms
crossfades, read with the same channel choice as render_cut. With less than 0.3 s of pause the quietest
50 ms windows are used instead (at most 2 s and 10% of the file), nf goes 3 dB higher and "strong" runs as
"light". When even the quietest windows sit close to the speech level the source is left untouched.

compose_final rebuilds the voice from the sources, one cut_timeline.json range at a time (denoise before
gain, limiter and fades, exactly like render_cut places them), mixes the ranges in samples, loudnorms it
like render_cut and uses it as the voice. cut.mp4 and its approval stay untouched. Without the sources it
cleans cut.mp4's own audio with a profile from its quietest windows (weaker, with a warning).

This module also measures noise for noise_check.py (measure_source).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import file_signature, has_ffmpeg_filter, load_json, probe_video, save_json  # noqa: E402
import render_cut as rc  # noqa: E402

SR = 48000
VERSION = "denoise/1"
MODES = ("off", "light", "strong")
LABEL = {"off": "off", "light": "light", "strong": "strong"}
NR = {"light": 10, "strong": 20}
HIGHPASS = "highpass=f=80:p=2"
NF_OFFSET_DB = 3.0            # nf sits this far above the measured noise of the clip
FALLBACK_EXTRA_DB = 3.0       # quietest windows read low (they catch the dips of the noise): nf 3 dB higher
NF_MIN, NF_MAX = -80, -20
PRE = 12000                   # 0.25 s of real audio read before each range (afftdn settles on it)
PAUSE_TRIM_S = 0.06
PAUSE_MIN_S = 0.10            # a trimmed pause shorter than this is not used
PROFILE_MAX_S = 2.0
PROFILE_MIN_S = 0.3           # pauses needed for a pause profile
CLIP_MIN_S = 0.2              # shortest clip afftdn gets to sample (after the crossfades)
XFADE = 240                   # 5 ms crossfade between joined noise pieces
WIN = 2400                    # 50 ms analysis windows
FALLBACK_SHARE = 0.10
FALLBACK_MIN_WINDOWS = 6
SN_STOP_MARGIN_S = 0.06
SPEECH_GAP_MIN_DB = 12.0      # quiet windows closer than this to the speech level are speech, not noise
EDGE_GUARD_S = 0.06           # the floor measurement skips this much around every range edge
FLOOR_PERCENTILE = 5          # noise floor = level only 5% of the 50 ms windows stay under
SELFTEST_EXPECTED_DELAY = 1200
BLOCK = SR                    # streaming decode block (1 s = 20 analysis windows)
PRE_EMPHASIS = 0.95           # the sync check compares x[n] - 0.95 x[n-1] (speech band, not rumble)
SYNC_MIN_CORR = 0.4           # a range of the rebuilt voice correlates at least this much with the cut
SYNC_MIN_DB = -60.0           # spans quieter than this are not compared
SYNC_MAX_LAG = 240
SYNC_LAG_TOL = 8              # the 80 Hz highpass alone shifts about 3 samples
SYNC_LAG_WINDOW = 20 * SR
FADE_GUARD = 1440             # the 30 ms fades at range edges are left out of the sync check
REPORT_KEYS = ("noise_floor_before_db", "noise_floor_after_db", "reduction_db", "sync")


class DenoiseError(Exception):
    pass


class SyncMismatch(DenoiseError):
    """The voice rebuilt from the sources is not the voice of cut.mp4 (wrong file, changed source)."""


def normalize_mode(value) -> str:
    """"off" | "light" | "strong" from edit_data (None and false are off). ValueError otherwise."""
    if value is None or value is False:
        return "off"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "off", "none"):
            return "off"
        if v in ("light", "strong"):
            return v
    shown = json.dumps(value, ensure_ascii=False) if isinstance(value, (str, int, float, bool)) else repr(value)
    raise ValueError(f'audio.denoise must be "off", "light", or "strong" (received {shown})')


def db(power: float) -> float:
    return float(10.0 * math.log10(max(float(power), 1e-12)))


def nf_for(noise_db: float, extra_db: float = 0.0) -> int:
    return int(min(NF_MAX, max(NF_MIN, round(noise_db + NF_OFFSET_DB + extra_db))))


_FF_VERSION: str | None = None
_FF_BUILD: str | None = None
FILTERS = ("highpass", "asendcmd", "afftdn")


def ffmpeg_version() -> str:
    global _FF_VERSION, _FF_BUILD
    if _FF_VERSION is None:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
        _FF_VERSION = (out.splitlines() or ["ffmpeg"])[0].strip()
        _FF_BUILD = hashlib.sha1(out.encode()).hexdigest()[:16]    # version line + configuration: tells builds apart
    return _FF_VERSION


def ffmpeg_build() -> str:
    ffmpeg_version()
    return _FF_BUILD or ""


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def graph_args(script: Path) -> list[str]:
    m = re.search(r"ffmpeg version n?(\d+)", ffmpeg_version())
    major = int(m.group(1)) if m else 7
    return ["-/filter_complex", str(script)] if major >= 7 else ["-filter_complex_script", str(script)]


def run_ffmpeg(args: list, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *[str(a) for a in args]], capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode("utf-8", "replace").strip().splitlines()[-8:])
        raise DenoiseError(f'FFmpeg failed to {what}: {tail}')
    return proc


def f32_stereo(raw: bytes) -> np.ndarray:
    x = np.frombuffer(raw[: len(raw) // 8 * 8], dtype="<f4")
    return x.reshape(-1, 2)


def write_wav(path: Path, x: np.ndarray) -> None:
    """48 kHz float32 WAV (IEEE float) of an (n, 2) array."""
    x = np.ascontiguousarray(np.asarray(x, dtype="<f4").reshape(-1, 2))
    data = x.tobytes()
    header = b"".join([
        b"RIFF", (36 + len(data)).to_bytes(4, "little"), b"WAVE",
        b"fmt ", (16).to_bytes(4, "little"), (3).to_bytes(2, "little"), (2).to_bytes(2, "little"),
        SR.to_bytes(4, "little"), (SR * 8).to_bytes(4, "little"), (8).to_bytes(2, "little"), (32).to_bytes(2, "little"),
        b"data", len(data).to_bytes(4, "little"),
    ])
    tmp = path.with_name(f'.tmp-{os.getpid()}-{path.name}')
    tmp.write_bytes(header + data)
    os.replace(tmp, path)


def read_audio(path: Path, af: str | None = None) -> np.ndarray:
    """Whole first audio track as float32 (n, 2) at 48 kHz."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0"]
    if af:
        cmd += ["-af", af]
    cmd += ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise DenoiseError(f'could not read audio from {Path(path).name}')
    return f32_stereo(proc.stdout)


# ============================================================== noise clip (profile)

def _join(pieces: list[np.ndarray]) -> np.ndarray:
    pieces = [p for p in pieces if len(p)]
    if not pieces:
        return np.zeros((0, 2), dtype=np.float32)
    out = pieces[0].astype(np.float32)
    ramp = np.linspace(0.0, 1.0, XFADE, dtype=np.float32)[:, None]
    for p in pieces[1:]:
        p = p.astype(np.float32)
        if len(out) > XFADE and len(p) > XFADE:
            mixed = out[-XFADE:] * (1.0 - ramp) + p[:XFADE] * ramp
            out = np.concatenate([out[:-XFADE], mixed, p[XFADE:]])
        else:
            out = np.concatenate([out, p])
    return out


def _intervals_to_samples(spans, total: int | None = None) -> list[tuple[int, int]]:
    out = []
    for a, b in spans:
        i0, i1 = int(round(float(a) * SR)), int(round(float(b) * SR))
        if total is not None:
            i1 = min(i1, total)
        if i1 > i0:
            out.append((max(0, i0), i1))
    return sorted(out)


def pause_intervals(regions: dict | None, duration: float) -> list[tuple[float, float]]:
    """Pauses of a regions file trimmed PAUSE_TRIM_S at both ends (time order)."""
    out = []
    for a, b in (regions or {}).get("silences") or []:
        a2, b2 = max(0.0, float(a) + PAUSE_TRIM_S), min(duration, float(b) - PAUSE_TRIM_S)
        if b2 - a2 >= PAUSE_MIN_S:
            out.append((a2, b2))
    return sorted(out)


def pick_pauses(pauses: list[tuple[float, float]], max_s: float = PROFILE_MAX_S) -> list[tuple[float, float]]:
    """Longest pauses first until max_s (the last one shortened), returned in time order."""
    chosen, total = [], 0.0
    for a, b in sorted(pauses, key=lambda ab: ab[0] - ab[1]):
        take = min(b - a, max_s - total)
        if take <= 0:
            break
        chosen.append((a, a + take))
        total += take
    return sorted(chosen)


def stream_analysis(src: Path, channel_mode: str, duration: float, picks: list[tuple[float, float]],
                    speech: list | None = None, pauses: list | None = None) -> dict:
    """One decoding pass of a source (render_cut's resampling and channel choice).

    Returns 50 ms window levels (dB, mono), the samples inside `picks` (stereo pieces), the quietest
    windows as fallback candidates (stereo) and the power inside `speech` and `pauses` spans."""
    chain = f'{rc.AUDIO_RESAMPLE},{rc.channel_filter(channel_mode)},aformat=sample_fmts=flt:channel_layouts=stereo'
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", "0:a:0", "-af", chain,
           "-f", "f32le", "-ar", str(SR), "-ac", "2", "-"]
    n_est = max(1, int(duration * SR) // WIN)
    k_quiet = max(FALLBACK_MIN_WINDOWS, min(int(PROFILE_MAX_S * SR) // WIN, int(FALLBACK_SHARE * n_est)))
    pick_s = _intervals_to_samples(picks)
    pieces: list[list[np.ndarray]] = [[] for _ in pick_s]
    sums = {"speech": [0.0, 0], "pauses": [0.0, 0]}
    spans = {"speech": _intervals_to_samples(speech or []), "pauses": _intervals_to_samples(pauses or [])}
    heap: list[tuple[float, int, np.ndarray]] = []       # (-level, index, samples): the k quietest windows
    levels: list[float] = []
    pos = 0
    carry = b""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        assert proc.stdout is not None
        while True:
            raw = proc.stdout.read(BLOCK * 8)
            if not raw:
                break
            raw = carry + raw
            usable = len(raw) // 8 * 8
            carry = raw[usable:]
            x = np.frombuffer(raw[:usable], dtype="<f4").reshape(-1, 2)
            n = len(x)
            mono = x.astype(np.float64).mean(axis=1)
            for j, (a, b) in enumerate(pick_s):
                lo, hi = max(a, pos), min(b, pos + n)
                if hi > lo:
                    pieces[j].append(x[lo - pos:hi - pos].copy())
            for key, lst in spans.items():
                for a, b in lst:
                    lo, hi = max(a, pos), min(b, pos + n)
                    if hi > lo:
                        seg = mono[lo - pos:hi - pos]
                        sums[key][0] += float(np.sum(seg * seg))
                        sums[key][1] += hi - lo
            w = n // WIN
            if w:
                powers = np.mean(mono[: w * WIN].reshape(w, WIN) ** 2, axis=1)
                for i, pw in enumerate(powers):
                    lv = db(pw)
                    levels.append(lv)
                    if lv < -90.0:          # digital silence is not noise
                        continue
                    idx = len(levels) - 1
                    item = (-lv, idx, x[i * WIN:(i + 1) * WIN].copy())
                    if len(heap) < k_quiet:
                        heapq.heappush(heap, item)
                    elif -heap[0][0] > lv:
                        heapq.heapreplace(heap, item)
            pos += n
        code = proc.wait()
    if code != 0 and pos == 0:
        raise DenoiseError(f'could not read audio from {src.name}')
    lv = np.array(levels) if levels else np.zeros(0)
    audible = lv[lv > -90.0]
    quiet = [samples for _, _, samples in sorted(heap, key=lambda t: t[1])]
    return {
        "samples": pos,
        "levels": lv,
        "speech_ref_db": float(np.percentile(audible, 90)) if audible.size else None,
        "pieces": [np.concatenate(p) if p else np.zeros((0, 2), np.float32) for p in pieces],
        "quiet": quiet,
        "speech_db": db(sums["speech"][0] / sums["speech"][1]) if sums["speech"][1] else None,
        "speech_seconds": sums["speech"][1] / SR,
        "pause_db": db(sums["pauses"][0] / sums["pauses"][1]) if sums["pauses"][1] else None,
        "pause_seconds": sums["pauses"][1] / SR,
    }


def _profile_prefix(src: Path) -> str:
    """Cache file prefix of a source's profile. The path hash keeps two sources apart when one stem starts like
    the other ("take" and "take_2") or the same name sits in two folders: the cleanup never deletes the other's."""
    return f'profile_{src.stem}_{_hash([str(Path(src).absolute())])[:8]}_'


def load_regions(src: Path, edit_dir: Path | None) -> tuple[dict | None, str]:
    """(regions, where) for a source: edit/regions/<stem>.json when it belongs to this file, else measured now."""
    if edit_dir is not None:
        path = Path(edit_dir) / "regions" / f'{src.stem}.json'
        try:
            reg = load_json(path)
        except (ValueError, OSError):
            reg = None
        if (isinstance(reg, dict) and isinstance(reg.get("silences"), list) and "start" not in reg
                and reg.get("source_sig") == file_signature(src)):
            return reg, "regions"
    try:
        import speech_regions
        return speech_regions.compute_regions(src, "auto", 0.25), 'measured now'
    except Exception:  # noqa: BLE001  (no regions: the quietest windows are used)
        return None, 'no measured pauses'


@dataclass
class Profile:
    stem: str
    how: str                    # "pauses" | "windows" | "none"
    seconds: float
    noise_db: float | None      # RMS of the clip (mono), dBFS
    extra_db: float
    nf: int | None
    usable: bool
    speech_ref_db: float | None
    pause_seconds: float
    wav: Path | None
    reason: str = ""

    def as_dict(self) -> dict:
        return {"how": self.how, "seconds": round(self.seconds, 3),
                "noise_db": None if self.noise_db is None else round(self.noise_db, 1),
                "nf": self.nf, "extra_db": self.extra_db, "usable": self.usable,
                "speech_ref_db": None if self.speech_ref_db is None else round(self.speech_ref_db, 1),
                "pause_seconds": round(self.pause_seconds, 3), "reason": self.reason}


def _profile_from_clip(stem: str, clip: np.ndarray, how: str, extra: float, speech_ref: float | None,
                       pause_s: float, wav: Path) -> Profile:
    noise = db(float(np.mean(clip.astype(np.float64).mean(axis=1) ** 2))) if len(clip) else None
    usable, reason = True, ""
    if noise is None or len(clip) < int(CLIP_MIN_S * SR):
        usable, reason = False, 'insufficient noise to measure'
    elif how == "windows" and speech_ref is not None and speech_ref - noise < SPEECH_GAP_MIN_DB:
        usable, reason = False, 'No noise-only interval exists; the quietest parts still contain speech'
    if usable:
        write_wav(wav, clip)
    return Profile(stem=stem, how=how, seconds=len(clip) / SR, noise_db=noise, extra_db=extra,
                   nf=nf_for(noise, extra) if noise is not None else None, usable=usable, speech_ref_db=speech_ref,
                   pause_seconds=pause_s, wav=wav if usable else None, reason=reason)


def learn_profile(src: Path, info: dict, channel_mode: str, edit_dir: Path | None, work: Path) -> Profile:
    """Noise clip of one source (cached in work/ by source signature, channel choice and regions)."""
    duration = float(info.get("duration") or 0.0)
    regions, _where = load_regions(src, edit_dir)
    pauses = pause_intervals(regions, duration)
    key = _hash([VERSION, file_signature(src), channel_mode, pauses, SR])
    work.mkdir(parents=True, exist_ok=True)
    prefix = _profile_prefix(src)
    meta_path = work / f'{prefix}{key}.json'
    wav = work / f'{prefix}{key}.wav'
    meta = load_json(meta_path) if meta_path.exists() else None
    if isinstance(meta, dict) and meta.get("key") == key and (not meta.get("usable") or wav.exists()):
        return Profile(stem=src.stem, how=meta["how"], seconds=meta["seconds"], noise_db=meta["noise_db"],
                       extra_db=meta["extra_db"], nf=meta["nf"], usable=meta["usable"],
                       speech_ref_db=meta.get("speech_ref_db"), pause_seconds=meta.get("pause_seconds", 0.0),
                       wav=wav if meta["usable"] else None, reason=meta.get("reason", ""))
    pause_total = sum(b - a for a, b in pauses)
    picks = pick_pauses(pauses) if pause_total >= PROFILE_MIN_S else []
    an = stream_analysis(src, channel_mode, duration, picks)
    if picks:
        prof = _profile_from_clip(src.stem, _join(an["pieces"]), "pauses", 0.0, an["speech_ref_db"], pause_total, wav)
    else:
        prof = _profile_from_clip(src.stem, _join(an["quiet"]), "windows", FALLBACK_EXTRA_DB, an["speech_ref_db"],
                                  pause_total, wav)
    for old in work.glob("profile_*"):
        if old.name.startswith(prefix) and not old.name.startswith(prefix + key):
            old.unlink(missing_ok=True)
    save_json(meta_path, {"key": key, "source": str(src), **prof.as_dict()})
    return prof


# ============================================================== graphs

def denoise_head(clip_samples: int, nr: int, nf: int) -> str:
    stop = max(0.0, clip_samples / SR - SN_STOP_MARGIN_S)
    return (f"{HIGHPASS},asendcmd=c='0.0 afftdn sn start;{stop:.4f} afftdn sn stop',afftdn=nr={nr}:nf={nf}")


def range_graph(clip_samples: int | None, nr: int | None, nf: int | None, pre: int, delay: int, samples: int,
                gain_db: float, channel_mode: str, fade_in: bool = True, fade_out: bool = True) -> str:
    """filter_complex of one range. Input 0: the source read from (audio_src_start - pre/SR); input 1: the
    noise clip. Without a clip (nr None) it is render_cut's own extraction, sample for sample."""
    head = f'{rc.AUDIO_RESAMPLE},{rc.channel_filter(channel_mode)}'
    tail = ",".join(rc.af_tail(gain_db, samples, fade_in, fade_out))   # no fade where the voice goes on
    if nr is None or clip_samples is None or nf is None:
        return (f'[0:a]{head},atrim=start_sample={pre}:end_sample={pre + samples},asetpts=N/SR/TB,{tail}[out]')
    need = pre + delay + samples
    start = clip_samples + pre + delay
    return (f'[1:a]aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo[nz];[0:a]{head},aformat=sample_fmts=flt:channel_layouts=stereo,atrim=end_sample={need},apad=whole_len={need}[v];[nz][v]concat=n=2:v=0:a=1,{denoise_head(clip_samples, nr, nf)},atrim=start_sample={start}:end_sample={start + samples},asetpts=N/SR/TB,{tail}[out]')


def whole_graph(clip_samples: int, nr: int, nf: int, delay: int, total: int) -> str:
    """filter_complex that cleans a whole track (input 0) with the clip of input 1, same length and sync."""
    start = clip_samples + delay
    return (f'[1:a]aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo[nz];[0:a]aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo,atrim=end_sample={total},apad=whole_len={total + delay}[v];[nz][v]concat=n=2:v=0:a=1,{denoise_head(clip_samples, nr, nf)},atrim=start_sample={start}:end_sample={start + total},asetpts=N/SR/TB[out]')


# ============================================================== self-test

def self_test(work: Path) -> dict:
    """Runs the chain once on 1 s of synthetic noise with a chirp (cached per ffmpeg build): the filters
    exist, sample count kept, delay measured (1200 expected at 48 kHz) and noise lowered."""
    work.mkdir(parents=True, exist_ok=True)
    version = ffmpeg_version()
    cache = work / "selftest.json"
    key = _hash([VERSION, ffmpeg_build(), HIGHPASS, NR, SN_STOP_MARGIN_S])
    old = load_json(cache) if cache.exists() else None
    if isinstance(old, dict) and old.get("key") == key:
        return old
    result = {"key": key, "ffmpeg": version, "ok": False, "delay": None, "reason": ""}
    try:
        missing = [name for name in FILTERS if not has_ffmpeg_filter(name)]
        if missing:
            raise DenoiseError(f"The installed FFmpeg lacks filter {', '.join(missing)}; install a complete FFmpeg build")
        rng = np.random.default_rng(20260915)
        sigma = 10 ** (-40 / 20)
        clip = rng.normal(0, sigma, (int(0.5 * SR), 1)).repeat(2, axis=1).astype(np.float32)
        sig = rng.normal(0, sigma, (SR, 1)).astype(np.float64)
        t = np.arange(int(0.3 * SR)) / SR
        chirp = 0.25 * np.sin(2 * np.pi * (300 * t + (3000 - 300) / (2 * 0.3) * t * t))
        a = int(0.5 * SR)
        sig[a:a + len(chirp), 0] += chirp
        sig = sig.repeat(2, axis=1).astype(np.float32)
        clip_wav, sig_wav = work / "selftest_clip.wav", work / "selftest_sig.wav"
        write_wav(clip_wav, clip)
        write_wav(sig_wav, sig)
        L, n = len(clip), len(sig)
        nf = nf_for(-40.0)

        def run(core: str) -> np.ndarray:
            graph = (f'[1:a]aformat=sample_fmts=flt:channel_layouts=stereo[nz];[0:a]aformat=sample_fmts=flt:channel_layouts=stereo[v];[nz][v]concat=n=2:v=0:a=1,{core}[out]')
            proc = run_ffmpeg(["-v", "error", "-i", sig_wav, "-i", clip_wav, "-filter_complex", graph, "-map", "[out]",
                               "-f", "f32le", "-ar", SR, "-ac", 2, "-"], 'test noise reduction')
            return f32_stereo(proc.stdout)[:, 0].astype(np.float64)

        ref = run(HIGHPASS)
        out = run(denoise_head(L, NR["light"], nf))
        if len(out) != L + n or len(ref) != L + n:
            raise DenoiseError(f'The filter changed the sample count ({len(out)} instead of {L + n})')
        r = ref[L + a - 2400: L + a + len(chirp) + 2400]
        o = out[L + a - 2400: L + a + len(chirp) + 2400 + 6000]
        dots = np.correlate(o, r, mode="valid")
        energy = np.concatenate([[0.0], np.cumsum(o * o)])
        norms = np.sqrt(energy[len(r):] - energy[:-len(r)]) * float(np.sqrt(np.sum(r * r))) + 1e-12
        corr = dots / norms[: len(dots)]
        lag = int(np.argmax(corr))
        best = float(corr[lag])
        if best < 0.8:
            raise DenoiseError(f'Could not measure filter delay (correlation {best:.2f})')
        q0, q1 = L + int(0.05 * SR), L + int(0.45 * SR)
        drop = db(float(np.mean(out[q0 + lag:q1 + lag] ** 2))) - db(float(np.mean(ref[q0:q1] ** 2)))
        if drop > -3.0:
            raise DenoiseError(f'Test noise was reduced by only {drop:.1f} dB')
        result.update(ok=True, delay=int(lag), drop_db=round(drop, 1), correlation=round(best, 3))
    except DenoiseError as e:
        result["reason"] = str(e)
    except Exception as e:  # noqa: BLE001
        result["reason"] = f'{e.__class__.__name__}: {e}'
    for name in ("selftest_clip.wav", "selftest_sig.wav"):
        (work / name).unlink(missing_ok=True)
    save_json(cache, result)
    return result


# ============================================================== voice rebuild (compose_final)

@dataclass
class VoiceResult:
    path: Path | None
    report: dict
    warnings: list[str] = field(default_factory=list)


def _edl_sources(edit_dir: Path, timeline: dict) -> tuple[dict | None, str]:
    """edl.json "sources" when the EDL is the one of the cut: same edl_sig, or the same ranges (a moved folder
    changes only the paths). The sync check in build_voice still confirms every range against cut.mp4."""
    path = edit_dir / "edl.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, 'edl.json was not found'
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), dict):
        return None, 'edl.json has no sources'
    if timeline.get("edl_sig") and rc.edl_signature(raw) != timeline.get("edl_sig"):
        segs = timeline.get("segments") or []
        ranges = raw.get("ranges") if isinstance(raw.get("ranges"), list) else []
        try:
            same = len(segs) == len(ranges) and all(
                str(s.get("id")) == str(r.get("id")) and s.get("source") == r.get("source")
                and abs(float(s["edl_start"]) - float(r["start"])) < 1e-6
                and float(s["edl_end"]) <= float(r["end"]) + 1e-6
                and abs(float(s.get("gain_db") or 0.0) - float(r.get("gain_db") or 0.0)) < 1e-6
                for s, r in zip(segs, ranges))
        except (KeyError, TypeError, ValueError):
            same = False
        if not same:
            return None, "edl.json changed after the cut"
    return raw["sources"], ""


def resolve_segment_sources(edit_dir: Path, timeline: dict) -> tuple[dict[str, Path] | None, str]:
    """Source path per timeline source key: segments[].source_path, else edl.json."""
    out: dict[str, Path] = {}
    edl_sources = None
    edl_reason = ""
    for s in timeline.get("segments") or []:
        key = str(s.get("source"))
        if key in out:
            continue
        path = Path(str(s["source_path"])) if s.get("source_path") else None
        if path is None or not path.exists():
            if edl_sources is None and not edl_reason:
                edl_sources, edl_reason = _edl_sources(edit_dir, timeline)
            if edl_sources is None:
                return None, (f'source {key} was not found ({path})' if path is not None else edl_reason)
            if key not in edl_sources:
                return None, f'source {key} is not in edl.json'
            path = rc.resolve_source(str(edl_sources[key]), edit_dir)
        if not path.exists():
            return None, f'source {key} was not found ({path})'
        out[key] = path
    return out, ""


def _segments_ok(timeline: dict | None) -> tuple[list[dict], int, str]:
    if not timeline or not timeline.get("segments"):
        return [], 0, 'no cut_timeline.json'
    try:
        fps = int(round(float(timeline.get("fps"))))
    except (TypeError, ValueError):
        return [], 0, 'cut_timeline.json has no fps'
    if fps <= 0 or SR % fps:
        return [], 0, f'fps {fps} of the cut'
    segs = []
    for s in timeline["segments"]:
        try:
            segs.append({"id": str(s.get("id")), "source": str(s["source"]),
                         "audio_src_start": float(s["audio_src_start"]), "lead": int(s["lead_frames"]),
                         "frames": int(s["frames"]), "out_start_frame": int(s["out_start_frame"]),
                         "gain_db": float(s.get("gain_db") or 0.0), "audio_mode": s.get("audio_mode"),
                         "fade_in": bool(s.get("audio_fade_in", True)), "fade_out": bool(s.get("audio_fade_out", True))})
        except (KeyError, TypeError, ValueError):
            return [], fps, 'cut_timeline.json is missing audio fields; rebuild the timeline'
    return segs, fps, ""


def _mono(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float64).mean(axis=1) if x.ndim == 2 else x.astype(np.float64)


def _levels(x: np.ndarray) -> np.ndarray:
    n = len(x) // WIN
    return 10 * np.log10(np.mean(x[: n * WIN].reshape(n, WIN) ** 2, axis=1) + 1e-12) if n else np.zeros(0)


def noise_floors(before: np.ndarray, after: np.ndarray, edges: list[int], shift_db: float = 0.0) -> tuple[float | None, float | None]:
    """Noise floor (dBFS) before and after: the level only FLOOR_PERCENTILE% of the 50 ms windows stay under,
    each track on its own, leaving out range edges (fades) and digital silence. `after` moves by shift_db so
    both are read at the same loudness."""
    b, a = _mono(before), _mono(after)
    m = min(len(a), len(b))
    lb, la = _levels(b[:m]), _levels(a[:m])
    ok = lb > -90.0
    guard = int(EDGE_GUARD_S * SR)
    for e in edges:
        ok[max(0, (e - guard) // WIN): min(len(ok), (e + guard) // WIN + 1)] = False
    if int(ok.sum()) < FALLBACK_MIN_WINDOWS:
        return None, None
    return float(np.percentile(lb[ok], FLOOR_PERCENTILE)), float(np.percentile(la[ok], FLOOR_PERCENTILE)) + shift_db


def sync_check(before: np.ndarray, after: np.ndarray, spans: list[tuple[str, int, int]]) -> dict:
    """Is the rebuilt voice the cut's voice, on the same samples? Correlation (pre-emphasized, lag 0) per range
    span, and the best lag of the whole track within +-240 samples."""
    b, a = _mono(before), _mono(after)
    m = min(len(a), len(b))
    b = b[1:m] - PRE_EMPHASIS * b[: m - 1]
    a = a[1:m] - PRE_EMPHASIS * a[: m - 1]
    worst = None
    for rid, s0, s1 in spans:
        s0, s1 = max(0, s0), min(len(b), s1)
        if s1 - s0 < WIN:
            continue
        x, y = b[s0:s1], a[s0:s1]
        ex, ey = float(np.dot(x, x)), float(np.dot(y, y))
        if db(ex / (s1 - s0)) < SYNC_MIN_DB:          # (almost) silent span: nothing to compare
            continue
        c = float(np.dot(x, y)) / math.sqrt(ex * ey + 1e-24)
        if worst is None or c < worst[1]:
            worst = (rid, c)
    lag = None
    if m > 4 * SYNC_MAX_LAG:
        k = min(len(a), SYNC_LAG_WINDOW)          # the lag is the same all along: 20 s are enough
        fa, fb = np.fft.rfft(a[:k]), np.fft.rfft(b[:k])
        cc = np.fft.irfft(fa * np.conj(fb), n=k)
        lags = np.concatenate([cc[-SYNC_MAX_LAG:], cc[:SYNC_MAX_LAG + 1]])
        lag = int(np.argmax(lags)) - SYNC_MAX_LAG
    return {"worst_range": worst[0] if worst else None, "min_correlation": None if worst is None else round(worst[1], 3),
            "lag_samples": lag,
            "ok": (worst is None or worst[1] >= SYNC_MIN_CORR) and (lag is None or abs(lag) <= SYNC_LAG_TOL)}


def _loudness(path: Path) -> float | None:
    m = rc.loudnorm_measure(path)
    try:
        return float(m["input_i"]) if m else None
    except (KeyError, TypeError, ValueError):
        return None


def _cached_voice(res: VoiceResult, voice: Path, meta_path: Path, key: str) -> bool:
    cached = load_json(meta_path) if meta_path.exists() and voice.exists() else None
    if isinstance(cached, dict) and cached.get("key") == key:
        res.report.update({k: cached[k] for k in REPORT_KEYS if cached.get(k) is not None})
        res.path = voice
        return True
    return False


def _finish(res: VoiceResult, base: Path, voice: Path, edges: list[int], spans: list[tuple[str, int, int]] | None,
            key: str, meta_path: Path, work: Path) -> None:
    """Noise floors before/after, sync check (raises when the rebuilt voice is not the cut's voice), cache."""
    report = res.report
    after = read_audio(voice)
    if spans is not None:
        # the cut through the same 80 Hz highpass: rumble under 80 Hz (street, traffic) that the rebuilt voice no
        # longer has would otherwise pull the correlation of a range with little speech under the limit
        check = sync_check(read_audio(base, f'aresample={SR},{HIGHPASS}'), after, spans)
        report["sync"] = check
        if not check["ok"]:
            voice.unlink(missing_ok=True)
            where = f" (interval {check['worst_range']}, correlation {check['min_correlation']})" if check["worst_range"] else ""
            raise SyncMismatch(f'Voice rebuilt from sources does not match cut audio{where}')
    i_before, i_after = _loudness(base), _loudness(voice)
    shift = (i_before - i_after) if (i_before is not None and i_after is not None) else 0.0
    fb, fa = noise_floors(read_audio(base, f'aresample={SR}'), after, edges, shift)
    if fb is not None and fa is not None:
        report.update(noise_floor_before_db=round(fb, 1), noise_floor_after_db=round(fa, 1),
                      reduction_db=round(fa - fb, 1))
    res.path = voice
    save_json(meta_path, {"key": key, **{k: report.get(k) for k in REPORT_KEYS}})
    for old in work.glob("voice_*"):
        if key not in old.name:
            old.unlink(missing_ok=True)


def build_voice(edit_dir: Path, base: Path, timeline: dict | None, fps: int, total: int, mode: str,
                work: Path, jobs: int = 4, keep_temp: bool = False) -> VoiceResult:
    """The cleaned voice track (48 kHz float stereo WAV, `total` samples) or path None when nothing was cleaned."""
    edit_dir = Path(edit_dir)
    work.mkdir(parents=True, exist_ok=True)
    report = {"mode": mode, "applied": "off", "method": None, "noise_floor_before_db": None,
              "noise_floor_after_db": None, "reduction_db": None, "delay_samples": None, "sources": [], "warnings": []}
    res = VoiceResult(path=None, report=report)

    def warn(msg: str) -> None:
        res.warnings.append(msg)
        report["warnings"].append(msg)

    if mode == "off":
        return res
    test = self_test(work)
    if not test.get("ok"):
        warn(f"Noise reduction skipped: FFmpeg test failed ({test.get('reason')}). Preserving original audio")
        return res
    delay = int(test["delay"])
    report["delay_samples"] = delay

    segs, tl_fps, reason = _segments_ok(timeline)
    sources = None
    if not reason:
        tl_total = int((timeline or {}).get("frames") or sum(s["frames"] for s in segs)) * SR // tl_fps
        if abs(tl_total - total) > SR // max(1, fps):
            reason = 'cut_timeline.json does not match the base video'
    if not reason:
        sources, reason = resolve_segment_sources(edit_dir, timeline or {})
    infos: dict[str, dict] = {}
    for key, path in (sources or {}).items():
        try:
            infos[key] = probe_video(path)
        except Exception:  # noqa: BLE001
            reason = f'Could not read source {key} ({path.name})'
            break
    if reason:
        warn(f'Applied lighter noise reduction to the cut than to the sources: {reason}')
        return _clean_cut(res, base, mode, delay, total, work, warn, keep_temp)
    try:
        return _from_sources(res, edit_dir, base, timeline or {}, segs, tl_fps, sources or {}, infos, mode, delay,
                             total, work, jobs, keep_temp, warn)
    except SyncMismatch as e:
        warn(f'Applied lighter noise reduction to the cut than to the sources: {e}')
        report["sources"] = []
        report["sync_sources_refused"] = report.pop("sync", None)
        return _clean_cut(res, base, mode, delay, total, work, warn, keep_temp)


def _from_sources(res: VoiceResult, edit_dir: Path, base: Path, timeline: dict, segs: list[dict], tl_fps: int,
                  sources: dict[str, Path], infos: dict[str, dict], mode: str, delay: int, total: int, work: Path,
                  jobs: int, keep_temp: bool, warn) -> VoiceResult:
    report = res.report
    report["method"] = "sources"
    spf = SR // tl_fps
    modes: dict[str, str] = {}
    for s in segs:
        key = s["source"]
        if key not in modes:
            if not infos[key].get("has_audio"):
                modes[key] = "silent"
            elif s.get("audio_mode") in ("stereo", "mono", "left", "right", "multi"):
                modes[key] = s["audio_mode"]
            else:
                modes[key] = rc.channel_mode(sources[key], infos[key])

    plans: dict[str, dict] = {}
    for key, path in sources.items():
        entry = {"source": key, "file": path.name, "applied": "off", "profile": None, "nr": None, "nf": None,
                 "ranges": [s["id"] for s in segs if s["source"] == key]}
        report["sources"].append(entry)
        plans[key] = {"prof": None, "nr": None}
        if modes[key] == "silent":
            entry["reason"] = 'no audio'
            continue
        prof = learn_profile(path, infos[key], modes[key], edit_dir, work)
        entry["profile"] = prof.as_dict()
        if not prof.usable:
            entry["reason"] = prof.reason
            warn(f'{key}: {prof.reason}. This video did not receive noise reduction')
            continue
        applied = mode
        if prof.how == "windows":
            if mode == "strong":
                applied = "light"
            warn(f'{key} has almost no pauses ({prof.pause_seconds:.2f} seconds); noise was measured in the quietest regions'
                 + ('; light cleanup replaced strong cleanup to avoid muffling speech' if mode == "strong" else ""))
        entry.update(applied=applied, nr=NR[applied], nf=prof.nf)
        plans[key] = {"prof": prof, "nr": NR[applied]}

    applied_set = {e["applied"] for e in report["sources"] if e["applied"] != "off"}
    if not applied_set:
        if all(m == "silent" for m in modes.values()):
            warn('Noise reduction skipped: cut sources have no audio')
        return res
    report["applied"] = next(iter(applied_set)) if len(applied_set) == 1 else "mixed"

    ranges = []
    for s in segs:
        pl = plans[s["source"]]
        prof = pl["prof"]
        samples = (s["lead"] + s["frames"]) * spf
        pre = min(PRE, int(round(s["audio_src_start"] * SR)))
        src = sources[s["source"]]
        rk = [VERSION, str(src), file_signature(src), round(s["audio_src_start"], 6), samples, pre, s["gain_db"],
              modes[s["source"]], pl["nr"], prof.nf if prof else None, prof.wav.name if prof and prof.wav else None, delay]
        if not (s["fade_in"] and s["fade_out"]):
            rk += [s["fade_in"], s["fade_out"]]
        ranges.append({"seg": s, "src": src, "prof": prof, "nr": pl["nr"], "samples": samples, "pre": pre,
                       "delay_out": (s["out_start_frame"] - s["lead"]) * spf, "key": _hash(rk)})

    voice_key = _hash([VERSION, ffmpeg_version(), denoise_head(0, 0, 0), total, [(r["key"], r["delay_out"]) for r in ranges],
                       timeline.get("edl_sig"), timeline.get("rendered_at"), file_signature(base), rc.LOUDNORM])
    voice = work / f'voice_{voice_key}.wav'
    meta_path = work / f'voice_{voice_key}.json'
    if _cached_voice(res, voice, meta_path, voice_key):
        return res

    tmp_dir = work / f'tmp_{voice_key}'
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def render_range(r: dict) -> Path:
        s = r["seg"]
        out = tmp_dir / f"range_{s['id']}_{r['key']}.wav"
        if modes[s["source"]] == "silent":
            write_wav(out, np.zeros((r["samples"], 2), dtype=np.float32))
            return out
        prof = r["prof"]
        clean = prof is not None and r["nr"] is not None
        graph = range_graph(rc.wav_samples(prof.wav) if clean else None, r["nr"], prof.nf if clean else None,
                            r["pre"], delay, r["samples"], s["gain_db"], modes[s["source"]], s["fade_in"], s["fade_out"])
        script = tmp_dir / f"range_{s['id']}_{r['key']}.txt"
        script.write_text(graph + "\n", encoding="utf-8")
        need = r["pre"] + (delay if clean else 0) + r["samples"]
        args = ["-v", "error", "-y", "-ss", f"{s['audio_src_start'] - r['pre'] / SR:.6f}", "-t", f'{need / SR + 0.5:.6f}',
                "-i", r["src"]]
        if clean:
            args += ["-i", prof.wav]
        args += [*graph_args(script), "-map", "[out]", "-c:a", "pcm_f32le", "-ar", SR, "-ac", 2, out]
        run_ffmpeg(args, f"denoise interval {s['id']}")
        got = rc.wav_samples(out)
        if got != r["samples"]:
            raise DenoiseError(f"interval {s['id']} produced {got} samples; expected {r['samples']}")
        return out

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        wavs = list(pool.map(render_range, ranges))

    mix = tmp_dir / "mix.wav"
    inputs, chains, labels = [], [], []
    for i, (r, w) in enumerate(zip(ranges, wavs)):
        inputs += ["-i", w]
        chains.append(f"[{i}:a]adelay=delays={r['delay_out']}S:all=1[a{i}]")
        labels.append(f'[a{i}]')
    if len(ranges) == 1:
        graph = f'{chains[0]};[a0]apad=whole_len={total},atrim=end_sample={total}[out]'
    else:
        graph = (";".join(chains) + ";" + "".join(labels) + f'amix=inputs={len(ranges)}:normalize=0:duration=longest:dropout_transition=0,apad=whole_len={total},atrim=end_sample={total}[out]')
    script = tmp_dir / "mix.txt"
    script.write_text(graph + "\n", encoding="utf-8")
    run_ffmpeg(["-v", "error", "-y", *inputs, *graph_args(script), "-map", "[out]", "-c:a", "pcm_f32le", "-ar", SR,
                "-ac", 2, mix], "join the cleaned voice")
    measured = rc.loudnorm_measure(mix)
    tmp_voice = voice.with_name(".tmp-" + voice.name)
    if measured:
        L = rc.LOUDNORM
        af = (f"loudnorm=I={L['I']}:TP={L['TP']}:LRA={L['LRA']}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true,aresample={SR},apad=whole_len={total},atrim=end_sample={total}")
        run_ffmpeg(["-v", "error", "-y", "-i", mix, "-af", af, "-c:a", "pcm_f32le", "-ar", SR, "-ac", 2, "-f", "wav",
                    tmp_voice], "normalize the cleaned voice")
    else:
        os.replace(mix, tmp_voice)
    os.replace(tmp_voice, voice)
    if not keep_temp:
        for d in work.glob("tmp_*"):
            shutil.rmtree(d, ignore_errors=True)
    edges = [e for r in ranges for e in (r["delay_out"], r["delay_out"] + r["samples"])]
    # each range is compared where it plays alone: from the end of the previous range's audio to where the next one starts
    starts = [r["delay_out"] for r in ranges] + [total]
    spans = []
    for i, r in enumerate(ranges):
        lo = max(r["delay_out"], ranges[i - 1]["delay_out"] + ranges[i - 1]["samples"] if i else 0)
        spans.append((r["seg"]["id"], lo + FADE_GUARD, min(r["delay_out"] + r["samples"], starts[i + 1]) - FADE_GUARD))
    _finish(res, base, voice, edges, spans, voice_key, meta_path, work)
    return res


def _clean_cut(res: VoiceResult, base: Path, mode: str, delay: int, total: int, work: Path, warn, keep_temp: bool) -> VoiceResult:
    """No usable sources: clean the cut's own audio with a profile from its quietest windows, always light."""
    report = res.report
    report["method"] = "cut"
    report["applied"] = "off"
    try:
        info = probe_video(base)
    except Exception:  # noqa: BLE001
        warn('Noise reduction skipped: could not read cut audio')
        return res
    if not info.get("has_audio"):
        warn('Noise reduction skipped: the cut has no audio')
        return res
    an = stream_analysis(base, "stereo", float(info.get("duration") or 0.0), [])
    prof = _profile_from_clip("cut", _join(an["quiet"]), "windows", FALLBACK_EXTRA_DB, an["speech_ref_db"], 0.0,
                              work / f'profile_cut_{_hash([file_signature(base)])}.wav')
    entry = {"source": "cut", "file": base.name, "applied": "off", "profile": prof.as_dict(), "nr": None, "nf": prof.nf,
             "ranges": []}
    report["sources"].append(entry)
    if not prof.usable:
        warn(f'Noise reduction skipped: {prof.reason}')
        return res
    applied = "light"
    if mode == "strong":
        warn('Cut audio uses light rather than strong denoising to avoid muffling the voice')
    entry.update(applied=applied, nr=NR[applied])
    report["applied"] = applied
    key = _hash([VERSION, ffmpeg_version(), denoise_head(0, 0, 0), "cut", file_signature(base), total, delay, prof.nf,
                 NR[applied]])
    voice = work / f'voice_{key}.wav'
    meta_path = work / f'voice_{key}.json'
    if _cached_voice(res, voice, meta_path, key):
        return res
    script = work / f'cut_{key}.txt'
    script.write_text(whole_graph(rc.wav_samples(prof.wav), NR[applied], prof.nf, delay, total) + "\n",   # type: ignore[arg-type]
                      encoding="utf-8")
    tmp_voice = voice.with_name(".tmp-" + voice.name)
    run_ffmpeg(["-v", "error", "-y", "-i", base, "-i", prof.wav, *graph_args(script), "-map", "[out]",
                "-c:a", "pcm_f32le", "-ar", SR, "-ac", 2, "-f", "wav", tmp_voice], 'denoise the cut audio')
    os.replace(tmp_voice, voice)
    if not keep_temp:
        script.unlink(missing_ok=True)
        for old in work.glob("profile_cut_*"):
            if old != prof.wav and re.fullmatch(r"profile_cut_[0-9a-f]{16}\.wav", old.name):
                old.unlink(missing_ok=True)
    _finish(res, base, voice, [], None, key, meta_path, work)
    return res


# ============================================================== noise_check

def measure_source(src: Path, edit_dir: Path | None) -> dict:
    """Noise floor in the pauses, speech level and their difference (dB) for one video."""
    try:
        info = probe_video(src)
    except Exception:  # noqa: BLE001
        return {"source": str(src), "stem": src.stem, "error": 'Could not read the file'}
    out = {"source": str(src), "stem": src.stem, "duration": round(float(info.get("duration") or 0.0), 2)}
    if not info.get("has_audio"):
        return {**out, "error": 'no audio'}
    mode = rc.channel_mode(src, info)
    regions, where = load_regions(src, edit_dir)
    duration = float(info.get("duration") or 0.0)
    pauses = pause_intervals(regions, duration)
    speech = [(float(a), float(b)) for a, b in (regions or {}).get("speech") or []]
    an = stream_analysis(src, mode, duration, [], speech=speech, pauses=pauses)
    pause_s = an["pause_seconds"]
    how = "pauses"
    noise = an["pause_db"]
    if pause_s < PAUSE_MIN_S or noise is None:
        how = "quietest regions"
        clip = _join(an["quiet"])
        noise = (db(float(np.mean(clip.astype(np.float64).mean(axis=1) ** 2))) + FALLBACK_EXTRA_DB) if len(clip) else None
    speech_db = an["speech_db"] if an["speech_db"] is not None else an["speech_ref_db"]
    out.update(regions=where, noise_from=how, pause_seconds=round(pause_s, 2),
               noise_db=None if noise is None else round(noise, 1),
               speech_db=None if speech_db is None else round(speech_db, 1))
    if noise is None or speech_db is None:
        return {**out, "error": 'no speech detected'}
    snr = speech_db - noise
    out["snr_db"] = round(snr, 1)
    if snr >= 30.0:
        out.update(level="clean", advice='already clean; not needed', suggest="off")
    elif snr >= 20.0:
        out.update(level="light", advice='light noise', suggest="light")
    else:
        out.update(level="strong", advice='strong noise', suggest="strong")
    return out
