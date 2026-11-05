"Find remaining silence in a rendered video using 10 ms mono RMS measurements.\n\nUse the waveform cutter's source-relative threshold and report runs at least the requested minimum duration. A finding is a review candidate; intentional expression and requested scope still govern editing. Explicit --threshold overrides measurement.\n\nInclude beginning and ending silence unless the final video has an intentional end hold. --end-hold reports that ending separately rather than treating it as accidental. Deliberately preserved dry=false passages also need contextual review.\n\nUsage: uv run python helpers/breaths.py VIDEO --min 0.25 --threshold -36 --json\nExit 0 for no findings, 1 for findings to review, and 2 for errors. Do not automatically delete every measured breath or pause."

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import waveform_cut as waveform  # noqa: E402
from common import fmt_time  # noqa: E402

MIN_S = 0.25


def find(db, th: float, min_s: float) -> list[tuple[float, float]]:
    out, run = [], None
    for i, v in enumerate(db):
        quiet = v < th
        if quiet and run is None:
            run = i
        if not quiet and run is not None:
            if (i - run) * waveform.HOP_S >= min_s:
                out.append((run * waveform.HOP_S, i * waveform.HOP_S))
            run = None
    if run is not None and (len(db) - run) * waveform.HOP_S >= min_s:
        out.append((run * waveform.HOP_S, len(db) * waveform.HOP_S))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Find remaining silence in a finished video (target: no unintended interval of 0.25 seconds or longer).')
    ap.add_argument('video', type=Path)
    ap.add_argument("--min", type=float, default=MIN_S, help='minimum silence duration in seconds (default: 0.25)', dest='min')
    ap.add_argument("--threshold", type=float, help='silence threshold in dB (default: measured)', dest='threshold_value')
    ap.add_argument("--end-hold", action="store_true",
                    help='final video with an intentional end hold (endHold); exclude ending silence', dest='end_hold')
    ap.add_argument("--json", action="store_true", dest='json')
    a = ap.parse_args(argv)
    if not a.video.is_file():
        print(f'ERROR: not found {a.video}', file=sys.stderr)
        return 2
    db = waveform.envelope(a.video, None)
    th = a.threshold_value if a.threshold_value is not None else waveform.threshold(db)
    found = find(db, th, a.min)
    end = len(db) * waveform.HOP_S
    tail = None
    if a.end_hold and found and found[-1][1] >= end - 0.05:        # silence up to the last frame
        tail, found = found[-1], found[:-1]
    if a.json:
        print(json.dumps({"thresholdDb": th, "min_s": a.min,
                          "breaths": [{"start": round(x, 2), "end": round(y, 2), "dur": round(y - x, 2)} for x, y in found],
                          'end_hold': ({"start": round(tail[0], 2), "dur": round(tail[1] - tail[0], 2)} if tail else None)},
                         ensure_ascii=False))
        return 1 if found else 0
    tail_note = (f"  still ending: {fmt_time(tail[0])} through the end ({tail[1] - tail[0]:.2f} seconds); excluded. Check it against the project's endHold") if tail else ""
    if not found:
        print(f'breaths: no silence of {a.min:.2f} s or more (below {th:.0f} dB)')
        if tail_note:
            print(tail_note)
        return 0
    print(f'respiros: {len(found)} silences of {a.min:.2f} s or more (below {th:.0f} dB):')
    for x, y in found:
        print(f'  {fmt_time(x)} to {fmt_time(y)}  ({y - x:.2f} s)')
    if tail_note:
        print(tail_note)
    print('  Split each remaining breath in edl_manual.json or run waveform cutting with --threshold about 3 dB higher, or use the measured threshold above (references/cut.md)')
    return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
