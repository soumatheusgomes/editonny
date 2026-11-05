"""Voice level per phrase (or per EDL range) against the speaker's reference level.

The level of a range is the RMS of its SPEECH samples only (pauses inside the range are
left out, using the speech regions of the source), after an 80 Hz high-pass so room
rumble does not count as voice. The reference is the speech-weighted median of the levels
(the level half of the speech time sits at or above), so a cut split between a loud take and
a quiet take is measured against the take with more speech, not against the middle.
Ranges at least --drop-db below the reference are listed with the `gain_db` to write in the
EDL (the whole difference, capped at +-12).

Modes:
- phrases: `voice_levels.py <video>` uses edit/transcripts/<stem>.json (phrases as in
  takes_packed.md). Without a transcript it falls back to the speech regions.
- EDL: `voice_levels.py [<video>] --edl edit/edl.json` measures the EDL ranges WITH their
  current gain_db applied, so a gain already set is not suggested again. Ranges --drop-db
  above the reference are listed too. The suggestion is the final gain_db (current + change).
  Sources resolve like render_cut (absolute, or relative to the edit dir or its parent). With
  a video only that source is listed.

Writes edit/verify/voice_levels.json with every range measured.

Usage:
    uv run python helpers/voice_levels.py <video> [--edit-dir DIR] [--edl edl.json] [--drop-db 5] [--all]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from common import load_json, now_iso, save_json
from pack_transcripts import phrases_for_transcript
from render_cut import resolve_source
from speech_regions import SAMPLE_RATE, AudioError, load_mono, load_or_compute_regions
from verify_cut import weighted_median

GAIN_CAP_DB = 12.0
MIN_SPEECH_S = 0.2
HIGHPASS = "highpass=f=80"


def _db(x: float) -> float:
    return float(10.0 * np.log10(x + 1e-12))


def measure_range(mono: np.ndarray, speech: list[list[float]], start: float, end: float) -> dict:
    """Speech-only RMS and peak of [start, end]."""
    n = len(mono)
    total_sq, total_n = 0.0, 0
    for s, e in speech:
        a, b = max(s, start), min(e, end)
        if b <= a:
            continue
        i, j = int(a * SAMPLE_RATE), min(int(b * SAMPLE_RATE), n)
        if j <= i:
            continue
        seg = mono[i:j].astype(np.float64)
        total_sq += float(np.dot(seg, seg))
        total_n += j - i
    i0, j0 = int(max(start, 0.0) * SAMPLE_RATE), min(int(end * SAMPLE_RATE), n)
    peak = float(np.max(np.abs(mono[i0:j0]))) if j0 > i0 else 0.0
    speech_s = total_n / SAMPLE_RATE
    return {
        "speech_s": round(speech_s, 2),
        "level_db": round(_db(total_sq / total_n), 1) if speech_s >= MIN_SPEECH_S else None,
        "peak_db": round(20.0 * np.log10(peak + 1e-9), 1),
    }


def _source_audio(path: Path, edit_dir: Path, cache: dict) -> tuple[np.ndarray, list[list[float]]]:
    key = str(path)
    if key not in cache:
        regions = load_or_compute_regions(path, edit_dir)
        mono, _ = load_mono(path, af=HIGHPASS)
        cache[key] = (mono, regions["speech"])
    return cache[key]


def ranges_from_edl(edl_path: Path, edit_dir: Path) -> list[dict]:
    edl = load_json(edl_path)
    if not isinstance(edl, dict) or not isinstance(edl.get("ranges"), list):
        raise ValueError(f"Invalid EDL: missing 'ranges': {edl_path}")
    sources = edl.get("sources") or {}
    out = []
    for i, r in enumerate(edl["ranges"]):
        stem = r.get("source")
        src = sources.get(stem)
        if not src:
            raise ValueError(f"range {r.get('id', i + 1)} uses source '{stem}', which is absent from 'sources'")
        path = resolve_source(str(src), edl_path.parent)
        out.append({"id": r.get("id") or f'r{i + 1}', "source": stem, "path": path,
                    "start": float(r["start"]), "end": float(r["end"]),
                    "text": r.get("quote") or r.get("beat") or "", "edl_gain_db": float(r.get("gain_db") or 0.0)})
    return out


def ranges_from_video(video: Path, edit_dir: Path) -> tuple[list[dict], str]:
    transcript = load_json(edit_dir / "transcripts" / f'{video.stem}.json')
    if isinstance(transcript, dict) and transcript.get("words"):
        phrases = phrases_for_transcript(transcript)
        return [{"id": f'f{i + 1}', "source": video.stem, "path": video, "start": p["start"], "end": p["end"],
                 "text": p["text"]} for i, p in enumerate(phrases)], "phrases"
    regions = load_or_compute_regions(video, edit_dir)
    merged: list[list[float]] = []
    for s, e in regions["speech"]:
        if merged and s - merged[-1][1] < 0.5:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [{"id": f't{i + 1}', "source": video.stem, "path": video, "start": s, "end": e, "text": ""}
            for i, (s, e) in enumerate(merged)], "regions"


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare each phrase or EDL interval's voice level against the speaker's reference level.")
    ap.add_argument('video', type=Path, nargs="?", help='video; optional with --edl, which measures all sources')
    ap.add_argument("--edit-dir", type=Path, default=None, help='edit folder (default: beside the EDL or <video folder>/edit)', dest='edit_dir')
    ap.add_argument("--edl", type=Path, default=None, help="edl.json: measure cut ranges instead of phrases", dest='edl')
    ap.add_argument("--drop-db", type=float, default=5.0, help='flag intervals at least this many dB below the reference, or above it in an EDL (default 5)', dest='drop_db')
    ap.add_argument("--all", action="store_true", help='list all intervals, not only quiet ones', dest='all')
    args = ap.parse_args()

    if args.video is None and args.edl is None:
        sys.exit('provide a video or --edl edit/edl.json')
    video = args.video.absolute() if args.video else None
    if video is not None and not video.exists():
        sys.exit(f'video not found: {video}')
    edl_path = None
    if args.edl is not None:
        edl_path = args.edl.absolute()
        if not edl_path.exists() and video is not None and (video.parent / "edit" / args.edl).exists():
            edl_path = (video.parent / "edit" / args.edl).absolute()
        if not edl_path.exists():
            sys.exit(f'EDL not found: {edl_path}')
    if args.edit_dir:
        edit_dir = args.edit_dir.absolute()
    elif edl_path is not None:
        edit_dir = edl_path.parent
    else:
        edit_dir = video.parent / "edit"

    try:
        if edl_path is not None:
            ranges, mode = ranges_from_edl(edl_path, edit_dir), "edl"
        else:
            ranges, mode = ranges_from_video(video, edit_dir)
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(str(exc))
    if not ranges:
        sys.exit('no intervals to measure; no speech detected')

    cache: dict = {}
    try:
        for r in ranges:
            if not Path(r["path"]).exists():
                sys.exit(f"source not found: {r['path']}")
            mono, speech = _source_audio(Path(r["path"]), edit_dir, cache)
            r.update(measure_range(mono, speech, r["start"], r["end"]))
    except AudioError as exc:
        sys.exit(str(exc))

    edl_mode = mode == "edl"
    for r in ranges:
        # EDL mode: the level heard in the cut includes the gain already in the EDL.
        current = float(r.get("edl_gain_db") or 0.0) if edl_mode else 0.0
        r["current_gain_db"] = current
        r["heard_db"] = None if r["level_db"] is None else round(r["level_db"] + current, 1)
    measured = [r for r in ranges if r["heard_db"] is not None]
    if not measured:
        sys.exit('no interval contains enough speech to measure')
    reference = weighted_median([r["heard_db"] for r in measured], [r["speech_s"] for r in measured])
    for r in ranges:
        if r["heard_db"] is None:
            r.update(diff_db=None, low=False, high=False, gain_db=r["current_gain_db"])
            continue
        diff = round(r["heard_db"] - reference, 1)
        low = diff <= -args.drop_db
        high = edl_mode and diff >= args.drop_db
        final = r["current_gain_db"] - diff if (low or high) else r["current_gain_db"]
        r.update(diff_db=diff, low=low, high=high, gain_db=round(min(GAIN_CAP_DB, max(-GAIN_CAP_DB, final)), 1))

    shown = [r for r in ranges if video is None or r["source"] == video.stem]
    unit = {"edl": ("EDL range", "EDL ranges"), "phrases": ("phrase", "phrases"),
            "regions": ("speech interval", "speech intervals")}[mode][0 if len(shown) == 1 else 1]
    scope = video.stem if video is not None else "all sources"
    gain_note = ', including EDL gain_db' if edl_mode else ""
    print(f'Voice level · {scope} · {len(shown)} {unit} · reference {reference:.1f} dB (speech-only RMS{gain_note}; median weighted by speech duration)')
    if mode == "regions":
        print('  no transcript: measured speech regions. Run transcribe.py for phrase-level measurements')
    listed = shown if args.all else [r for r in shown if r["low"] or r["high"]]
    for r in listed:
        where = f"{r['id']} {r['source']} " if edl_mode else ""
        if r["heard_db"] is None:
            print(f"  {where}[{r['start']:06.2f}-{r['end']:06.2f}] contains insufficient speech to measure")
            continue
        tip = ""
        if r["low"] or r["high"]:
            tip = (f"  gain_db final {r['gain_db']:+.1f} (currently {r['current_gain_db']:+.1f})" if edl_mode
                   else f"  gain_db {r['gain_db']:+.1f} if used in the EDL")
        text = f'''  "{r['text'][:60]}"''' if r["text"] else ""
        print(f"  {where}[{r['start']:06.2f}-{r['end']:06.2f}] {r['heard_db']:6.1f} dB ({r['diff_db']:+.1f}){tip}{text}")
    n_low = sum(1 for r in shown if r["low"])
    n_high = sum(1 for r in shown if r["high"])
    if n_low + n_high == 0:
        both = "below or above" if edl_mode else "below"
        print(f'  no interval {args.drop_db:g} dB or more {both} from the reference')
    elif not args.all:
        parts = []
        if n_low:
            parts.append(f"{n_low} {('quiet interval' if n_low == 1 else 'quiet intervals')}")
        if n_high:
            parts.append(f"{n_high} {('loud interval' if n_high == 1 else 'loud intervals')}")
        print(f"  {' and '.join(parts)}. In the EDL, replace the interval's gain_db with the displayed final value (maximum {GAIN_CAP_DB:g} dB above or below)")

    out = edit_dir / "verify" / "voice_levels.json"
    save_json(out, {
        "mode": mode,
        "edl": str(edl_path) if edl_path else None,
        "reference_db": round(reference, 1),
        "drop_db": args.drop_db,
        "measured_at": now_iso(),
        "ranges": [{k: (str(v) if k == "path" else v) for k, v in r.items()} for r in ranges],
    })
    print(f'  details in {out}')


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
