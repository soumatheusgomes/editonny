"""Numeric self-check of a rendered cut. Prints a short English report and writes
edit/verify/cut_report.json.

Usage:
    uv run python helpers/verify_cut.py <edit>/edl.json <edit>/cut.mp4 [--min-silence 0.7] [--ask-approval]

Checks (each problem becomes a FLAG with a timestamp on the cut):
  DURATION        video/audio/timeline durations and frame counts disagree
  STALE-TIMELINE  the EDL changed after the render (edl_sig differs) or no timeline found
  PREVIEW         the file is a low-resolution --preview render
  SOURCE-MISSING  a source of the EDL cannot be found or its audio cannot be read (edges unchecked)
  SILENT          the cut has no sound at all
  CLICK           sample jump at a junction boundary far above the local signal
  EDGE-IN-SPEECH  a range starts or ends while the source is still sounding (cut inside a word)
  EDGE-IN-WORD    a range edge falls inside a transcribed word and the gap there is only a
                  consonant closure (t, k, p): the rest of the word is cut off
  REPEAT          a range replays speech of the previous range of the same source
  DEAD-AIR        silence >= --min-silence inside the cut (with a concrete start/end to use)
  BLACK           black frames
  CLIPPING        clipped samples or true peak above -1.0 dBTP (ebur128 or the 2-decimal loudnorm reading)
  LOUDNESS        integrated loudness more than 1.5 LU away from -14 LUFS
  LOW-LEVEL       a range more than 4 dB under the reference level (speech-weighted median of the
                  heard range levels), or the farther one (from the reference) of two neighbours, or of
                  the quietest and loudest ranges, more than 6 dB apart. The message gives the final
                  gain_db to write in the EDL: it closes the gap between the SOURCE levels (gain_db
                  works before loudnorm, whose dynamic mode squeezes the gap heard in the cut)
  HIGH-LEVEL      the same, over the reference level
Notices that are not flags (HDR source, crop without a face) go to "warnings".

state.json: when the file is the cut of the edit dir, verify asks for the approval
(awaiting approval_cut) only when there are no flags, or with --ask-approval after the flags
were explained. Otherwise awaiting stays null.
Exit code 0 even with flags; 2 when the EDL or the video cannot be read.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from common import OUTPUT_FORMATS, file_signature, fmt_time, load_json, now_iso, probe_video, save_json
from contact_sheet import abspath, find_timeline

SR = 48000
TARGET_I = -14.0
TRUE_PEAK_LIMIT = -1.0
LEVEL_DELTA_DB = 4.0
ADJACENT_JUMP_DB = 6.0
GAIN_CAP_DB = 12.0
CLICK_RATIO = 12.0
CLICK_MIN_JUMP = 0.02
EDGE_FLOOR_DB = -50.0
EDGE_ABOVE_NOISE_DB = 15.0  # an edge is on speech only this far above the source's own noise floor:
                            # breath tails and room hiss 10 dB over the floor flooded the report, and
                            # so repeated false alarms can hide real clipped words.
WORD_MARGIN_S = 0.08      # the quiet run must sit at least this far inside the word on both sides
WORD_GAP_MAX_S = 0.12     # a quiet run shorter than this around the edge is a closure, not a pause
WORD_REACH_S = 0.15       # sound must exist on both sides within this distance
SILENT_DB = -60.0
SEAMLESS_S = 0.02         # next range restarts the source this close to where the previous ended
BLOCKS_APPROVAL = {"STALE-TIMELINE", "DURATION", "PREVIEW"}


def db(x: float) -> float:
    return 20 * math.log10(max(float(x), 1e-9))


def decode_audio(path: Path, start: float = 0.0, dur: float | None = None, mono: bool = True,
                 af: str | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f'{start:.6f}']
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", f'{dur:.6f}']
    if af:
        cmd += ["-af", af]
    cmd += ["-vn", "-ac", "1" if mono else "2", "-ar", str(SR), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        return np.zeros(0 if mono else (0, 2), dtype=np.float32)
    data = np.frombuffer(proc.stdout, dtype=np.float32)
    return data if mono else data.reshape(-1, 2)


def rms_frames(x: np.ndarray, win_s: float) -> np.ndarray:
    win = max(1, int(win_s * SR))
    n = x.size // win
    if n == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(x[: n * win].reshape(n, win) ** 2, axis=1))


def power_mean_db(values_db: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(10 ** (values_db / 10)))) if values_db.size else -120.0


def weighted_median(values: list[float], weights: list[float]) -> float:
    """Level that half of the speech time sits at or above (ties go to the louder level)."""
    pairs = sorted(zip(values, weights), key=lambda p: -p[0])
    total = sum(max(w, 1e-6) for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += max(w, 1e-6)
        if acc >= total / 2 - 1e-9:
            return v
    return pairs[-1][0]


def ffmpeg_stderr(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace').stderr


def fmt_db(value: float | None) -> str:
    return "--" if value is None else f'{value:.1f}'


# ------------------------------------------------------------------ checks


def loudness_stats(video: Path) -> dict:
    err = ffmpeg_stderr(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(video), "-vn",
                         "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"])
    tail = err[err.rfind("Summary:"):] if "Summary:" in err else err

    def grab(pattern: str) -> float | None:
        m = re.search(pattern, tail)
        if not m:
            return None
        return -120.0 if "inf" in m.group(1) else float(m.group(1))

    return {"integrated": grab(r"I:\s+(-?inf|-?[\d.]+) LUFS"), "lra": grab(r"LRA:\s+(-?inf|-?[\d.]+) LU"),
            "true_peak": grab(r"Peak:\s+(-?inf|-?[\d.]+) dBFS")}


def black_frames(video: Path) -> tuple[list[dict], int]:
    """Black intervals and the number of frames that really decode (full decode pass)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(video), "-an",
                           "-vf", "scale=180:-2,blackdetect=d=0.08:pic_th=0.97:pix_th=0.10",
                           "-progress", "pipe:1", "-f", "null", "-"], capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = []
    for m in re.finditer(r"black_start:(-?[\d.]+) black_end:(-?[\d.]+) black_duration:([\d.]+)", proc.stderr):
        out.append({"start": float(m.group(1)), "end": float(m.group(2)), "duration": float(m.group(3))})
    decoded = [int(x) for x in re.findall(r"^frame=(\d+)", proc.stdout, flags=re.M)]
    return out, (decoded[-1] if decoded else 0)


def dead_air(mono: np.ndarray, noise_db: float, min_silence: float) -> list[dict]:
    """Silence runs >= min_silence on 20 ms RMS windows (a 3-window median ignores single bumps).

    ffmpeg's silencedetect tests every sample's amplitude, so room noise peaks split real pauses
    into short pieces after loudness normalization; RMS windows measure what the ear hears.
    """
    win = int(0.02 * SR)
    n = mono.size // win
    if n < 3:
        return []
    vals = 20 * np.log10(np.maximum(np.sqrt(np.mean(mono[: n * win].reshape(n, win) ** 2, axis=1)), 1e-9))
    smooth = np.median(np.stack([np.r_[vals[:1], vals[:-1]], vals, np.r_[vals[1:], vals[-1:]]]), axis=0)
    quiet = smooth < noise_db
    out, i = [], 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        a, b = i * 0.02, min(j * 0.02, mono.size / SR)
        if b - a >= min_silence - 1e-3:
            out.append({"start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3)})
        i = j
    return out


def click_probe(audio: np.ndarray, t: float) -> dict:
    """Max first-difference in +-10 ms around t vs the RMS of the first difference in +-300 ms."""
    c = int(round(t * SR))
    lo, hi = max(1, c - int(0.3 * SR)), min(audio.shape[0], c + int(0.3 * SR))
    if hi - lo < int(0.05 * SR):
        return {"jump": 0.0, "ratio": 0.0}
    seg = audio[lo - 1:hi]
    d = np.abs(np.diff(seg, axis=0))
    if d.ndim == 2:
        d = d.max(axis=1)
    k0, k1 = max(0, c - lo - int(0.01 * SR)), min(d.size, c - lo + int(0.01 * SR))
    near = d[k0:k1]
    mask = np.ones(d.size, dtype=bool)
    mask[k0:k1] = False
    base = float(np.sqrt(np.mean(d[mask] ** 2))) if mask.any() else 0.0
    jump = float(near.max()) if near.size else 0.0
    return {"jump": round(jump, 5), "ratio": round(jump / max(base, 1e-5), 2)}


def loudest_channel(x: np.ndarray) -> np.ndarray:
    """Stereo -> the louder channel (a phase-cancelled pair would vanish in an L+R mix)."""
    if x.ndim == 1 or x.shape[0] == 0:
        return x.reshape(-1)
    return x[:, int(np.argmax(np.mean(x ** 2, axis=0)))]


RUMBLE_AF = "highpass=f=300,highpass=f=300"   # Same filter as precise_edges: remove car, road, and AC rumble.
RUMBLE_RANGE_DB = 20.0                         # a source whose voice stands less than this over its floor


def noise_floor_db(src: Path) -> tuple[float | None, str | None]:
    """(room noise of a whole source, audio filter its edges are measured with). The floor is the 10th
    percentile of the 10 ms level of the louder channel. A source with rumble (voice less than
    RUMBLE_RANGE_DB over the floor) is measured without the lows, or the rumble reads as speech at every edge."""
    for af in (None, RUMBLE_AF):
        x = loudest_channel(decode_audio(src, mono=False, af=af))
        vals = rms_frames(x, 0.01)
        if not vals.size:
            return None, None
        lo, hi = db(np.percentile(vals, 10)), db(np.percentile(vals, 90))
        if hi - lo >= RUMBLE_RANGE_DB or af:
            return float(lo), af
    return None, None


def edge_probe(src: Path, t_edge: float, side: str, ref_db: float, floor_db: float | None = None,
               af: str | None = None) -> dict:
    """Level just inside and just outside a source cut edge (40 ms each)."""
    w = 0.04
    x = loudest_channel(decode_audio(src, max(0.0, t_edge - w), 2 * w, mono=False, af=af))
    half = x.size // 2
    before = db(np.sqrt(np.mean(x[:half] ** 2))) if half else -120.0
    after = db(np.sqrt(np.mean(x[half:] ** 2))) if x.size - half > 0 else -120.0
    inside, outside = (after, before) if side == "start" else (before, after)
    gate = max(ref_db - 20.0, EDGE_FLOOR_DB,
               floor_db + EDGE_ABOVE_NOISE_DB if floor_db is not None else EDGE_FLOOR_DB)
    return {"inside_db": round(inside, 1), "outside_db": round(outside, 1), "gate_db": round(gate, 1),
            "in_speech": bool(inside > gate and outside > gate and t_edge > w)}


def word_at(transcript: dict | None, t: float) -> dict | None:
    if not transcript:
        return None
    for w in transcript.get("words", []):
        if float(w.get("start", 0)) < t < float(w.get("end", 0)):
            return w
    return None


def word_edge_probe(src: Path, t_edge: float, ref_db: float, transcript: dict | None,
                    af: str | None = None) -> dict | None:
    """Edge inside a transcribed word: is the quiet run around it only a consonant closure?

    A stop consonant (t, k, p) makes 50-60 ms of near silence inside a word, so the 40 ms level
    probe reads one side as quiet. Here the 10 ms level shows a quiet run shorter than
    WORD_GAP_MAX_S with sound on both sides, and that run sits inside a transcribed word with
    WORD_MARGIN_S of the word on each side: the word goes on past the edge. A pause between
    words is longer, or lies at a word boundary (Whisper word starts drift into short pauses,
    hence the margin). Calibrated on the test footage: 90% of mid-word edges caught, no hit on
    pause edges with the SKILL padding nor on the edges of the real sessions.
    """
    w = word_at(transcript, t_edge)
    if w is None:
        return None
    t0 = max(0.0, t_edge - WORD_REACH_S)
    x = loudest_channel(decode_audio(src, t0, t_edge + WORD_REACH_S - t0, mono=False, af=af))
    vals = np.array([db(v) for v in rms_frames(x, 0.01)])
    if vals.size < 4:
        return None
    gate = max(ref_db - 20.0, EDGE_FLOOR_DB)
    loud = vals > gate
    c = min(max(int((t_edge - t0) / 0.01), 0), vals.size - 1)
    lo = hi = c
    if not loud[c]:
        while lo > 0 and not loud[lo - 1]:
            lo -= 1
        while hi < vals.size - 1 and not loud[hi + 1]:
            hi += 1
        gap = (hi - lo + 1) * 0.01
    else:
        gap = 0.0
    g0, g1 = t0 + lo * 0.01, t0 + (hi + 1) * 0.01
    hit = bool(gap < WORD_GAP_MAX_S and loud[:lo].any() and loud[hi + 1:].any()
               and float(w["start"]) + WORD_MARGIN_S <= g0 and g1 <= float(w["end"]) - WORD_MARGIN_S)
    return {"word": str(w.get("text", "")).strip(), "word_start": float(w["start"]), "word_end": float(w["end"]),
            "gap_s": round(gap, 2), "in_word": hit}


# ------------------------------------------------------------------ state


def update_state(edit_dir: Path, video: Path, flags: list[dict], ask_approval: bool) -> tuple[str | None, bool | None]:
    """Ask for the approval of the cut in state.json only after this check.

    Returns (English status line, approval requested) or (None, None) when the video is not the cut of the edit dir."""
    state_path = edit_dir / "state.json"
    state = load_json(state_path, default={}) or {}
    if not isinstance(state, dict):
        state = {}
    if video.parent != edit_dir or video.name != (state.get("cut_video") or "cut.mp4"):
        return None, None
    blocking = sorted({f["code"] for f in flags if f["code"] in BLOCKS_APPROVAL})
    ready = not blocking and (not flags or ask_approval)
    if ready:
        state.update(phase="cut", awaiting="approval_cut",
                     message='Cut ready. Review it and identify any requested changes.')
        line = 'Preview: cut approval requested.'
    else:
        n = len(flags)
        state.update(phase="cut", awaiting=None,
                     message=f"Checking the cut: {n} {('point' if n == 1 else 'points')} to resolve before presentation")
        if blocking:
            line = f"Preview: approval NOT requested ({', '.join(blocking)}): render again."
        else:
            line = ('Preview: approval not requested yet. Fix and render again, or use --ask-approval after explaining accepted warnings.')
    state["updated_at"] = now_iso()
    save_json(state_path, state)
    return line, ready


# ------------------------------------------------------------------ main


def verify(edl_path: Path, video: Path, min_silence: float) -> tuple[dict, list[str]]:
    from render_cut import ASPECT_WARN, edl_signature, resolve_source, true_peak_db

    edl = load_json(edl_path)
    edit_dir = edl_path.parent
    from project_contract import managed_config, settings_hash
    contract = managed_config(edit_dir)
    if contract and edl.get('contractHash') != settings_hash(contract):
        raise ValueError('EDL does not match the confirmed contract')
    info = probe_video(video)
    if not info.get("has_video") or not info.get("duration"):
        raise ValueError('The video has no picture or duration')
    flags: list[dict] = []
    warnings: list[str] = []

    def flag(code: str, t: float | None, message: str, rid: str | None = None, end: float | None = None) -> None:
        item = {"code": code, "time": None if t is None else round(t, 3), "message": message}
        if end is not None:
            item["end"] = round(end, 3)
        if rid:
            item["range"] = rid
        flags.append(item)

    # ---- duration vs timeline
    tl_path = find_timeline(video)
    if tl_path is None and (edit_dir / "cut_timeline.json").exists():
        tl_path = edit_dir / "cut_timeline.json"
    timeline = load_json(tl_path) if tl_path else None
    fps = float((timeline or {}).get("fps") or info.get("fps") or 30)
    streams = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration,nb_frames", "-of", "json", str(video)],
        capture_output=True, text=True, encoding='utf-8', errors='replace').stdout or "{}").get("streams", [])
    v_dur = next((float(s.get("duration") or 0) for s in streams if s.get("codec_type") == "video"), info["duration"])
    a_dur = next((float(s.get("duration") or 0) for s in streams if s.get("codec_type") == "audio"), None)
    v_frames = next((int(s.get("nb_frames") or 0) for s in streams if s.get("codec_type") == "video"), 0)
    dur_info = {"video": round(v_dur, 3), "audio": None if a_dur is None else round(a_dur, 3), "frames": v_frames,
                "fps": fps}

    if timeline:
        segs = timeline.get("segments") or []
        dur_info.update(timeline=timeline.get("duration"), timeline_frames=timeline.get("frames"))
        if timeline.get("edl_sig") and edl_signature(edl) != timeline["edl_sig"]:
            flag("STALE-TIMELINE", None, "The EDL changed after rendering; render again before requesting approval")
        if abs(v_dur - float(timeline.get("duration") or 0)) > 1.0 / fps + 1e-3:
            flag("DURATION", v_dur, f"Video contains {v_dur:.3f}s, but the timeline reports {float(timeline['duration']):.3f}s")
        if timeline.get("frames") and v_frames and int(timeline["frames"]) != v_frames:
            flag("DURATION", None, f"Video contains {v_frames} frames, but the timeline reports {timeline['frames']}")
    else:
        flag("STALE-TIMELINE", None, 'Timeline missing: interval measurements use the EDL mapping')
        segs, pos = [], 0.0
        for r in edl.get("ranges", []):
            d = float(r["end"]) - float(r["start"])
            segs.append({"id": r.get("id"), "source": r["source"], "src_start": float(r["start"]),
                         "src_end": float(r["end"]), "out_start": pos, "out_end": pos + d,
                         "audio_out_start": pos, "audio_out_end": pos + d, "audio_src_start": float(r["start"])})
            pos += d
    if a_dur is None:
        flag("DURATION", None, 'The cut has no audio')
    elif abs(a_dur - v_dur) > 0.05:
        flag("DURATION", v_dur, f'audio ({a_dur:.3f}s) and video ({v_dur:.3f}s) have different durations')

    # ---- preview render (half resolution) passing as the approval file
    out_cfg = dict(edl.get("output") or {})
    if contract:   # Same size rules as render_cut: the confirmed snapshot, then the format, then the source (unknown here).
        out_cfg.update(width=contract['workflow']['video']['width'], height=contract['workflow']['video']['height'])
    fmt_wh = OUTPUT_FORMATS.get(out_cfg.get("format"), (0, 0))
    want_w, want_h = int(out_cfg.get("width") or fmt_wh[0]), int(out_cfg.get("height") or fmt_wh[1])
    got_w, got_h = int(info.get("display_width") or 0), int(info.get("display_height") or 0)
    if (timeline or {}).get("preview") or (want_w and got_w and (got_w < want_w - 2 or got_h < want_h - 2)):
        flag("PREVIEW", None, f'low-resolution preview ({got_w}x{got_h}, but the EDL requests {want_w}x{want_h}); render without --preview before requesting approval')

    # ---- full decode: black frames + how many frames really decode
    blacks, decoded_frames = black_frames(video)
    dur_info["decoded_frames"] = decoded_frames
    if decoded_frames == 0:
        raise ValueError('No frames decode; the file is incomplete or corrupt')
    if v_frames and decoded_frames < v_frames - 1:
        flag("DURATION", decoded_frames / fps, f'only {decoded_frames} of {v_frames} frames decode; file is incomplete or corrupt, render again')

    # ---- audio decode (stereo for peaks/clicks, mono mix for levels)
    stereo = decode_audio(video, mono=False) if a_dur else np.zeros((0, 2), dtype=np.float32)
    mono = stereo.mean(axis=1) if stereo.size else np.zeros(0, dtype=np.float32)
    if a_dur and stereo.shape[0] < (a_dur - 0.1) * SR:
        flag("DURATION", stereo.shape[0] / SR, f'only {stereo.shape[0] / SR:.2f}s of {a_dur:.2f}s of audio decode; file is incomplete or corrupt')

    loud = loudness_stats(video) if a_dur else {}
    peak = float(np.abs(stereo).max()) if stereo.size else 0.0
    clipped = int((np.abs(stereo) >= 0.999).sum()) if stereo.size else 0
    # ebur128 prints the true peak with 1 decimal (-0.96 reads -1.0); the loudnorm reading used by
    # render_cut's peak guard has 2. Either one above the limit is a flag.
    loud.update(sample_peak_db=round(db(peak), 2), clipped_samples=clipped,
                true_peak_loudnorm=true_peak_db(video) if a_dur else None)
    silent = a_dur is not None and (db(peak) < SILENT_DB or (loud.get("integrated") is not None
                                                               and loud["integrated"] <= -69.0))
    if silent:
        flag("SILENT", None, 'The cut is silent; check source audio')
    if clipped > 2:
        flag("CLIPPING", float(np.argmax(np.abs(stereo).max(axis=1) >= 0.999)) / SR,
             f'{clipped} clipped samples')
    tp_worst = max((v for v in (loud.get("true_peak"), loud.get("true_peak_loudnorm")) if v is not None), default=None)
    if not silent and tp_worst is not None and tp_worst > TRUE_PEAK_LIMIT:
        flag("CLIPPING", None, f'true peak {tp_worst:.2f} dBTP, above {TRUE_PEAK_LIMIT:.1f}: render again; the renderer reduces gain to keep peaks within bounds')
    if not silent and loud.get("integrated") is not None and abs(loud["integrated"] - TARGET_I) > 1.5:
        flag("LOUDNESS", None, f"loudness {loud['integrated']:.1f} LUFS, target {TARGET_I:.0f}")

    # ---- speech reference and adaptive silence threshold
    frames_db = np.array([db(v) for v in rms_frames(mono, 0.05)]) if mono.size else np.zeros(0)
    speech_ref = float(np.percentile(frames_db, 90)) if frames_db.size else -20.0
    floor_db = float(np.percentile(frames_db, 10)) if frames_db.size else -60.0
    noise_db = min(-20.0, max(floor_db + 10.0, speech_ref - 25.0))

    # ---- junction click probe
    junctions = []
    for a, b in zip(segs, segs[1:]):
        probes = [click_probe(stereo, float(b["out_start"]))]
        if float(b.get("audio_out_start", b["out_start"])) < float(b["out_start"]) - 1e-6:
            probes.append(click_probe(stereo, float(b["audio_out_start"])))
        worst = max(probes, key=lambda p: p["ratio"])
        click = worst["ratio"] > CLICK_RATIO and worst["jump"] > CLICK_MIN_JUMP
        junctions.append({"time": round(float(b["out_start"]), 3), "between": [a["id"], b["id"]], **worst,
                          "click": click})
        if click:
            flag("CLICK", float(b["out_start"]), f"click at junction {a['id']} -> {b['id']} (jump {worst['jump']:.3f}, {worst['ratio']:.0f}x the local reference)", b["id"])
    if segs and stereo.size and not silent:
        for t, label in ((0.0, 'start'), (v_dur, "end")):
            p = click_probe(stereo, min(max(t, 0.012), v_dur - 0.012))
            if p["ratio"] > CLICK_RATIO and p["jump"] > CLICK_MIN_JUMP:
                flag("CLICK", t, f'click at the {label} of the video')

    # ---- sources: resolved like render_cut, probed once
    sources = edl.get("sources") or {}
    src_info: dict[str, dict] = {}
    for key in dict.fromkeys(s["source"] for s in segs):
        value = sources.get(key)
        if not value:
            flag("SOURCE-MISSING", None, f'source {key!r} is not in EDL sources; boundaries unchecked')
            continue
        path = resolve_source(str(value), edit_dir)
        if not path.exists():
            flag("SOURCE-MISSING", None, f'source {key} was not found ({path}); its interval boundaries were not checked. Fix the EDL path')
            continue
        try:
            pinfo = probe_video(path)
        except Exception:  # noqa: BLE001
            flag("SOURCE-MISSING", None, f'Could not read source {key} ({path.name}); boundaries unchecked')
            continue
        sig = file_signature(path)
        tr = load_json(edit_dir / "transcripts" / f'{path.stem}.json')
        if isinstance(tr, dict) and tr.get("source_sig") and tr["source_sig"] != sig:
            tr = None        # the source changed after the transcription: word times are not trustworthy
        reg = load_json(edit_dir / "regions" / f'{path.stem}.json')
        speech = ([[float(a), float(b)] for a, b in reg["speech"]]
                  if isinstance(reg, dict) and isinstance(reg.get("speech"), list)
                  and reg.get("source_sig") in (None, sig) and "start" not in reg else None)
        src_info[key] = {"path": path, "info": pinfo, "transcript": tr if isinstance(tr, dict) else None,
                         "speech": speech}
        src_info[key]["floor"], src_info[key]["af"] = noise_floor_db(path) if pinfo.get("has_audio") else (None, None)
        if pinfo.get("is_hdr"):
            warnings.append(f'HDR source ({key}); colors were converted to SDR. Check preview color and consider disabling phone HDR for future recordings.')
    for s in segs:
        crop = s.get("crop") if isinstance(s.get("crop"), dict) else None
        if crop and crop.get("focus_from") == "center" and float(crop.get("kept") or 1) < 1 / ASPECT_WARN:
            warnings.append(f"interval {s['id']}: the image was center-cropped ({float(crop['kept']):.0%} retained) without a detected face. Inspect framing and adjust focus_x/focus_y if necessary")

    # ---- cut edges inside speech or inside a word (source side)
    edge_jobs = []
    for s in segs:
        si = src_info.get(s["source"])
        if si is None or not si["info"].get("has_audio"):
            continue
        a0 = float(s.get("audio_src_start", s["src_start"]))
        a1 = float(s["src_end"])
        edge_jobs.append((s, si, a0, a1))

    def edges_for(job):
        s, si, a0, a1 = job
        src = si["path"]
        body = loudest_channel(decode_audio(src, a0, a1 - a0, mono=False))
        if body.size == 0:
            return s, si, None
        body_db = np.array([db(v) for v in rms_frames(body, 0.05)])
        ref = float(np.percentile(body_db, 90)) if body_db.size else -20.0
        active = body_db[body_db > ref - 20.0]
        src_level = power_mean_db(active) if active.size >= 4 else None     # the voice level: unfiltered
        edge_ref = ref
        if si.get("af"):          # the edge gates read the same filtered audio the edge probes read
            fb = loudest_channel(decode_audio(src, a0, a1 - a0, mono=False, af=si["af"]))
            fdb = np.array([db(v) for v in rms_frames(fb, 0.05)])
            edge_ref = float(np.percentile(fdb, 90)) if fdb.size else ref
        probes = {side: (edge_probe(src, t, side, edge_ref, si.get("floor"), si.get("af")),
                          word_edge_probe(src, t, edge_ref, si["transcript"], si.get("af")))
                  for side, t in (("start", a0), ("end", a1))}
        return s, si, (ref, src_level, probes)

    # a range that picks up where the previous one of the same source stopped plays as one take
    seamless: set[tuple[str, str]] = set()
    for a, b in zip(segs, segs[1:]):
        if a["source"] == b["source"] and\
                abs(float(b.get("audio_src_start", b["src_start"])) - float(a["src_end"])) <= SEAMLESS_S:
            seamless |= {(a["id"], "end"), (b["id"], "start")}

    edges = []
    source_levels: dict[str, float | None] = {}
    gains = {str(r.get("id")): float(r.get("gain_db") or 0.0) for r in edl.get("ranges", [])}
    # waveform_cut.py ended these ranges in the valley between two words said together: the next word's attack
    # is right there, so "still sounding" at that end is expected (the transcription of the cut is the check)
    valley_ends = {str(r.get("id")) for r in edl.get("ranges", []) if r.get('ends_in_valley')}
    unreadable: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for s, si, result in pool.map(edges_for, edge_jobs):
            if result is None:
                if s["source"] not in unreadable:
                    unreadable.add(s["source"])
                    flag("SOURCE-MISSING", None, f"Could not read source audio: {s['source']} ({si['path'].name}); its interval boundaries were not checked")
                continue
            ref, src_level, probes = result
            source_levels[s["id"]] = None if src_level is None else src_level + gains.get(str(s["id"]), 0.0)
            for side, t_src, t_out in (("start", float(s.get("audio_src_start", s["src_start"])),
                                        float(s.get("audio_out_start", s["out_start"]))),
                                       ("end", float(s["src_end"]), float(s["out_end"]))):
                e, wp = probes[side]
                if (s["id"], side) in seamless:   # the audio runs on into the next range: no cut heard
                    e = {**e, "in_speech": False}
                    wp = None
                if side == "end" and str(s["id"]) in valley_ends:
                    e = {**e, "in_speech": False, 'ends_in_valley': True}
                    wp = None
                in_word = bool(wp and wp["in_word"])
                edges.append({"range": s["id"], "side": side, "src_time": round(t_src, 3), "out_time": round(t_out, 3),
                              **e, "word": wp["word"] if wp else None, "in_word": in_word,
                              "word_gap_s": wp["gap_s"] if wp else None})
                what = "start" if side == "start" else "end"
                where = 'starts' if side == "start" else "termina"
                if e["in_speech"]:
                    extra = f''' in the word "{wp['word']}"''' if wp else ""
                    flag("EDGE-IN-SPEECH", t_out, f"{s['id']} {where} while sound is still active{extra} (source {t_src:.2f}s, {e['inside_db']:.0f}/{e['outside_db']:.0f} dB): move {what} to a pause", s["id"])
                elif in_word:
                    edge_to = wp["word_start"] if side == "start" else wp["word_end"]
                    flag("EDGE-IN-WORD", t_out, f'''{s['id']} {where} inside the word "{wp['word']}" (source {t_src:.2f}s, word {wp['word_start']:.2f}-{wp['word_end']:.2f}s): move {what} closer to {edge_to:.2f}s and inspect with speech_regions.py''', s["id"])

    # ---- the same speech played twice (consecutive ranges of one source that overlap)
    for a, b in zip(segs, segs[1:]):
        if a["source"] != b["source"]:
            continue
        a0, a1 = float(a.get("audio_src_start", a["src_start"])), float(a["src_end"])
        b0, b1 = float(b.get("audio_src_start", b["src_start"])), float(b["src_end"])
        overlap = min(a1, b1) - max(a0, b0)
        if b0 < a1 and overlap > 1.0 / fps + 0.005:
            flag("REPEAT", float(b.get("audio_out_start", b["out_start"])),
                 f"{b['id']} repeats {overlap:.2f}s of speech from {a['id']} (same source, {max(a0, b0):.2f}-{min(a1, b1):.2f}s): move start of {b['id']} to {a1:.2f}s or later, or shorten end for {a['id']}",
                 b["id"])

    # ---- dead air, placed on each range's own audio span (up to where the next range's audio starts)
    def own_span(i: int) -> tuple[float, float]:
        s = segs[i]
        a = float(s.get("audio_out_start", s["out_start"]))
        b = float(s["out_end"])
        if i + 1 < len(segs):
            b = min(b, float(segs[i + 1].get("audio_out_start", segs[i + 1]["out_start"])))
        return a, b

    def to_src(i: int, t_out: float) -> float:
        s = segs[i]
        return float(s.get("audio_src_start", s["src_start"])) + (t_out - float(s.get("audio_out_start", s["out_start"])))

    def src_speech(i: int) -> list[list[float]] | None:
        return (src_info.get(segs[i]["source"]) or {}).get("speech")

    def new_end_for(i: int, t_out: float) -> float:
        """Where the speech before a silence ends in the source (regions/<stem>.json), plus 0.15 s.
        The cut's own silence starts late on soft word endings, so the regions win when present."""
        t = to_src(i, t_out)
        ends = [b for a, b in (src_speech(i) or []) if t - 1.0 < b <= t + 0.3]
        return (max(ends) if ends else t) + 0.15

    def new_start_for(i: int, t_out: float) -> float:
        """Where the speech after a silence starts in the source, minus 0.10 s (soft onsets like 'g'
        are heard in the cut later than they start)."""
        t = to_src(i, t_out)
        starts = [a for a, b in (src_speech(i) or []) if t - 0.3 <= a < t + 1.0]
        return (min(starts) if starts else t - 0.15) - 0.10

    silences = dead_air(mono, noise_db, min_silence) if mono.size and not silent else []
    for sil in silences:
        over = [i for i in range(len(segs)) if min(own_span(i)[1], sil["end"]) - max(own_span(i)[0], sil["start"]) > 0.02]
        mid = (sil["start"] + sil["end"]) / 2
        rid = next((s["id"] for s in segs if float(s["out_start"]) <= mid < float(s["out_end"])), None)
        sil["range"] = rid
        new_end = lambda i: new_end_for(i, sil["start"])  # noqa: E731
        new_start = lambda i: new_start_for(i, sil["end"])  # noqa: E731
        if not over:
            msg = "shorten the pause"
        elif len(over) >= 2:
            i, j = over[0], over[-1]
            msg = (f"at junction {segs[i]['id']} -> {segs[j]['id']}: shorten end of {segs[i]['id']} to {new_end(i):.2f}s and/or move start of {segs[j]['id']} to {new_start(j):.2f}s")
        else:
            i = over[0]
            a, b = own_span(i)
            sid = segs[i]["id"]
            if sil["end"] >= b - 0.1:
                where = 'at the end of the video' if sil["end"] >= v_dur - 0.05 else f'at the end of interval {sid}'
                msg = f'{where}: shorten end of {sid} to {new_end(i):.2f}s'
            elif sil["start"] <= a + 0.1:
                where = 'at the start of the video' if sil["start"] <= 0.05 else f'at the start of interval {sid}'
                msg = f'{where}: move start of {sid} to {new_start(i):.2f}s'
            else:
                msg = (f'inside interval {sid}: split into two, one ending at {new_end(i):.2f}s and the next starting at {new_start(i):.2f}s')
        flag("DEAD-AIR", sil["start"], f"silence of {sil['duration']:.2f}s {msg}", rid, sil["end"])

    # ---- black frames
    for b in blacks:
        flag("BLACK", b["start"], f"black frames for {b['duration']:.2f}s", end=b["end"])

    # ---- range level balance against a reference level (speech-weighted median of the heard levels)
    ranges_out = []
    reference = None
    if mono.size and segs and not silent:
        win = int(0.05 * SR)
        for i, s in enumerate(segs):
            a, b = float(s["out_start"]), own_span(i)[1]
            x = mono[int(a * SR):int(b * SR)]
            vals = np.array([db(v) for v in rms_frames(x, 0.05)]) if x.size >= win else np.zeros(0)
            gate = max(float(np.percentile(vals, 90)) - 20.0, floor_db + 6.0) if vals.size else 0.0
            active = vals[vals > gate]
            level = power_mean_db(active) if active.size >= 4 else None
            src_level = source_levels.get(s["id"])
            ranges_out.append({"id": s["id"], "level_db": None if level is None else round(level, 1),
                               "source_level_db": None if src_level is None else round(src_level, 1),
                               "speech_seconds": round(active.size * 0.05, 2),
                               "gain_db": gains.get(str(s["id"]), 0.0)})
        measured = [r for r in ranges_out if r["level_db"] is not None]
        if len(measured) >= 2:
            reference = weighted_median([r["level_db"] for r in measured], [r["speech_seconds"] for r in measured])
            ref_range = next(r for r in measured if r["level_db"] == reference)
            for r in measured:
                r["delta_db"] = round(r["level_db"] - reference, 1)
            # A jump between neighbours is heard even when both sit near the reference, and so is a
            # drift from the first ranges to the last in small steps: fix the one farther from the reference.
            jump_fix: dict[str, str] = {}
            farther = lambda a, b: (a, b) if abs(a["level_db"] - reference) >= abs(b["level_db"] - reference) else (b, a)  # noqa: E731
            for a, b in zip(measured, measured[1:]):
                if abs(a["level_db"] - b["level_db"]) > ADJACENT_JUMP_DB:
                    fix, other = farther(a, b)
                    jump_fix.setdefault(fix["id"], other["id"])
            lo, hi = min(measured, key=lambda r: r["level_db"]), max(measured, key=lambda r: r["level_db"])
            if hi["level_db"] - lo["level_db"] > ADJACENT_JUMP_DB:
                fix, other = farther(lo, hi)
                jump_fix.setdefault(fix["id"], other["id"])
            src_ref = ref_range.get("source_level_db")
            for r, s in zip(ranges_out, segs):
                if r["level_db"] is None:
                    continue
                delta = r["level_db"] - reference
                if abs(delta) <= LEVEL_DELTA_DB and r["id"] not in jump_fix:
                    continue
                # gain_db acts before loudnorm, which in its dynamic mode squeezes the gap heard in the cut.
                # The gap between the sources (gain included) is the one gain_db closes; it is used when it
                # points the same way and is not smaller than the heard one.
                change = delta
                src_gap = None if src_ref is None or r.get("source_level_db") is None else r["source_level_db"] - src_ref
                if src_gap is not None and src_gap * delta > 0 and abs(src_gap) >= abs(delta):
                    change = src_gap
                final = round(min(GAIN_CAP_DB, max(-GAIN_CAP_DB, r["gain_db"] - change)), 1)
                r["suggested_gain_db"] = final
                code, how = ("LOW-LEVEL", "below") if delta < 0 else ("HIGH-LEVEL", "above")
                why = (f'{abs(delta):.1f} dB {how} from reference level ({reference:.1f} dB)'
                       if abs(delta) > LEVEL_DELTA_DB else
                       f"more than {ADJACENT_JUMP_DB:.0f} dB different from interval {jump_fix[r['id']]} ({abs(delta):.1f} dB {how} from reference)")
                flag(code, float(s["out_start"]), f"{r['id']} is {why}: use gain_db {final:+.1f} in its EDL entry (currently {r['gain_db']:+.1f})", r["id"], float(s["out_end"]))

    flags.sort(key=lambda f: (f["time"] is None, f["time"] or 0.0))
    report = {
        "video": str(video), "edl": str(edl_path), "timeline": str(tl_path) if tl_path else None,
        "created_at": now_iso(), "min_silence": min_silence,
        "duration": dur_info, "loudness": loud,
        "speech_ref_db": round(speech_ref, 1), "silence_noise_db": round(noise_db, 1),
        "junctions": junctions, "edges": edges, "dead_air": silences, "black": blacks,
        "reference_level_db": None if reference is None else round(reference, 1),
        "ranges": ranges_out, "warnings": warnings, "flags": flags, "ok": not flags,
    }

    # ---- summary
    lines = [f'Cut verification · {video.name}']
    tl_txt = f" · timeline {float(timeline['duration']):.2f}s" if timeline else ' · no timeline'
    a_txt = f' · audio {a_dur:.2f}s' if a_dur is not None else ' · no audio'
    lines.append(f'Duration: {v_dur:.2f}s ({v_frames} frames){a_txt}{tl_txt}')
    if silent:
        lines.append('Loudness: no audio')
    elif loud.get("integrated") is not None:
        tp_ln = loud.get("true_peak_loudnorm")
        tp_txt = f'{tp_ln:.2f}' if tp_ln is not None else fmt_db(loud.get("true_peak"))
        lines.append(f"Loudness: {loud['integrated']:.1f} LUFS · true peak {tp_txt} dBTP · LRA {fmt_db(loud.get('lra'))} · clipped samples {clipped}")
    n_click = sum(1 for j in junctions if j["click"])
    lines.append(f'Junctions: {len(junctions)} · clicks {n_click}'
                 + (f" · worst {max((j['ratio'] for j in junctions)):.1f}x" if junctions else ""))
    n_edge = sum(1 for e in edges if e["in_speech"])
    n_word = sum(1 for e in edges if e["in_word"] and not e["in_speech"])
    lines.append(f'Interval boundaries: {len(edges)} · within speech {n_edge} · inside words {n_word}')
    if reference is not None:
        spread = [r["delta_db"] for r in ranges_out if r.get("delta_db") is not None]
        lines.append(f'Interval levels: from {min(spread):+.1f} to {max(spread):+.1f} dB from reference ({reference:.1f} dB)')
    lines.append(f'Silence >= {min_silence:g}s: {len(silences)} · black intervals: {len(blacks)}')
    for w in warnings:
        lines.append(f'notice: {w}')
    if flags:
        lines.append("")
        lines.append(f'FLAGS ({len(flags)}):')
        for f in flags:
            t = fmt_time(f["time"]) if f["time"] is not None else "  --  "
            lines.append(f"  {t}  {f['code']:15s} {f['message']}")
    else:
        lines.append("")
        lines.append("No problems found.")
    return report, lines


def main() -> int:
    ap = argparse.ArgumentParser(description='Verify a rendered cut and report timestamped findings.')
    ap.add_argument('edl', type=Path, help="edl.json used for this render")
    ap.add_argument('video', type=Path, help="cut.mp4")
    ap.add_argument("--min-silence", type=float, default=0.7, help='minimum silence duration to flag DEAD-AIR (default: 0.7s)', dest='min_silence')
    ap.add_argument("--ask-approval", action="store_true",
                    help='request preview approval despite warnings, after explaining accepted findings', dest='ask_approval')
    ap.add_argument("-o", "--output", type=Path, help='report JSON (default: edit/verify/cut_report.json)', dest='output')
    ap.add_argument("--without-approval", action="store_true",
                    help='check only without changing preview state; audit_cut.py uses this alongside speech review', dest='without_approval')
    ap.add_argument('--report-only', action='store_true', help='diagnostic report only; findings do not count as a passing gate')
    ap.add_argument("--approve-existing", action="store_true",
                    help='request approval using the existing report for this cut.mp4 without repeating verification', dest='approve')
    args = ap.parse_args()

    edl_path, video = abspath(args.edl), abspath(args.video)
    def identity():
        from common import content_hash
        from project_contract import digest
        inputs = {'video': content_hash(video), 'edl': content_hash(edl_path)}
        for path in (edl_path.parent / 'cut_timeline.json', edl_path.parent.parent / 'project/project.json'):
            if path.is_file():
                inputs[str(path)] = content_hash(path)
        return digest(inputs)
    if args.approve:
        out = abspath(args.output) if args.output else edl_path.parent / "verify" / "cut_report.json"
        report = load_json(out)
        if (not isinstance(report, dict) or not video.exists() or report.get("video") != str(video)
                or video.stat().st_mtime > out.stat().st_mtime or not edl_path.is_file()
                or report.get('verificationIdentity') != identity()):
            print(f'error: {out.name} does not belong to this {video.name}, or the cut has changed. Run verification without --approve-existing',
                  file=sys.stderr)
            return 2
        state_line, report["approval_requested"] = update_state(edl_path.parent, video, report["flags"],
                                                                args.ask_approval)
        save_json(out, report)
        print(state_line or "This video is not the folder's cut; there is nothing to approve.")
        return 0 if args.report_only or not report.get('flags') else 1
    if not edl_path.exists():
        print(f'Error: EDL not found: {edl_path}', file=sys.stderr)
        return 2
    if not video.exists():
        print(f'error: video not found: {video}', file=sys.stderr)
        return 2
    try:
        edl = json.loads(edl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f'Error: EDL is not valid JSON: {exc}', file=sys.stderr)
        return 2
    if not isinstance(edl, dict) or not isinstance(edl.get("ranges"), list):
        print('Error: EDL has no intervals in ranges', file=sys.stderr)
        return 2
    try:
        before_identity = identity()
        report, lines = verify(edl_path, video, args.min_silence)
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f'error: could not read {video.name}: {exc}', file=sys.stderr)
        return 2
    except (TypeError, KeyError) as exc:
        print(f'Error: EDL or timeline has a missing or invalid field ({exc.__class__.__name__}: {exc})',
              file=sys.stderr)
        return 2
    out = abspath(args.output) if args.output else edl_path.parent / "verify" / "cut_report.json"
    if identity() != before_identity:
        print('Error: inputs changed during verification; check the current files again', file=sys.stderr)
        return 2
    report['verificationIdentity'] = before_identity
    report['status'] = 'failed' if report['flags'] else 'passed'
    state_line, report["approval_requested"] = (None, None) if args.without_approval else\
        update_state(edl_path.parent, video, report["flags"], args.ask_approval)
    save_json(out, report)
    print("\n".join(lines))
    if state_line:
        print(state_line)
    print(f'\nReport: {out}')
    return 0 if args.report_only or not report['flags'] else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
