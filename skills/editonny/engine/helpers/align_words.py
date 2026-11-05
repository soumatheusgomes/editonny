"Align word starts to the measured waveform rather than estimated transcript timing.\n\nWhisper may place a word up to 0.3 seconds early, especially after a cut or pause. Inspect audio around each predicted start, from 0.10 seconds before to 0.40 seconds after, without crossing the previous word. After a pause, choose the first sustained sound. In connected speech, use a sufficiently deep nearby valley; if evidence is weak, retain the transcript time. Use the waveform cutter's 10 ms mono RMS measurement and source-relative threshold.\n\nPrint words moved by at least 0.05 seconds and write edit/waveform_words.json with word indices, text, predicted and measured times, differences, and measurement method. Use measured seconds for precise animation, sound, and cut anchors. Rerun after every new cut.\n\nUsage: uv run --directory ENGINE python helpers/align_words.py EDIT --word WORD --start 2 --end 6\nUse --all for all words, or --video VIDEO --transcript FILE for a separate recording."

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import waveform_cut as waveform  # noqa: E402
from common import fmt_time, load_json, save_json  # noqa: E402

BEFORE_S = 0.10          # Whisper can also be a little late
AFTER_S = 0.40         # ... and is up to 0.3 s early
QUIET_MIN_S = 0.03     # a quiet run this long before the word is a pause
SOUND_MIN_S = 0.04        # sound this long after it is the word (not a click)
VALLEY_DB_VALUE = 10.0          # a dip this deep between two words marks the start of the second
SHOW_S = 0.05        # the table shows words that moved at least this much


def _normal(text_value: str) -> str:
    t = unicodedata.normalize("NFKD", (text_value or "").lower())
    return re.sub(r"[^\w]+", "", "".join(c for c in t if not unicodedata.combining(c)))


def measure_value(db: np.ndarray, th: float, whisper: float, after_start: float, end_value: float) -> tuple[float, str]:
    "Find a word's onset after the preceding onset and before the next word; do not consume its neighbor's attack."
    hop = waveform.HOP_S
    lo = max(after_start, whisper - BEFORE_S, 0.0)
    hi = max(lo + hop, end_value)
    i0, i1 = int(round(lo / hop)), min(len(db), int(round(hi / hop)))
    if i1 - i0 < 2:
        return round(whisper, 3), "whisper"
    seg = db[i0:i1]
    quiet_need, sound_need = int(round(QUIET_MIN_S / hop)), int(round(SOUND_MIN_S / hop))
    # 1. pause -> sound: first sustained sound after a quiet run; the one nearest after the Whisper start wins
    cands = []
    run = 0
    for k in range(len(seg)):
        if seg[k] < th:
            run += 1
            continue
        if run >= quiet_need and k + sound_need <= len(seg) and bool((seg[k:k + sound_need] >= th).all()):
            cands.append(k)
        run = 0
    if cands:
        w = int(round(whisper / hop)) - i0
        after_value = [k for k in cands if k >= w - int(round(0.05 / hop))]
        k = after_value[0] if after_value else cands[-1]
        return round((i0 + k) * hop, 3), "pause"
    # 2. no pause: the rise after the deepest dip near the Whisper start
    j0 = max(0, int(round((whisper - 0.08) / hop)) - i0)
    j1 = min(len(seg), int(round((whisper + 0.20) / hop)) - i0)
    if j1 - j0 >= 3:
        k = j0 + int(np.argmin(seg[j0:j1]))
        background_value = float(seg[k])
        ahead_value = seg[k:min(len(seg), k + int(round(0.15 / hop)))]
        if len(ahead_value) and float(ahead_value.max()) - background_value >= VALLEY_DB_VALUE:
            middle = background_value + (float(ahead_value.max()) - background_value) / 2
            rises = int(np.argmax(ahead_value >= middle))
            return round((i0 + k + rises) * hop, 3), "vale"
    return round(whisper, 3), "whisper"


def measure_words(db: np.ndarray, th: float, words: list[dict]) -> list[dict]:
    out = []
    ws = sorted(words, key=lambda w: float(w["start"]))
    for i, w in enumerate(ws):
        s, e = float(w["start"]), float(w.get("end", w["start"]))
        after_start = out[-1]["start"] + 0.05 if out else 0.0
        # up to 0.4 s after the Whisper start (more when Whisper stretched the word over the silence before it,
        # as the first word of a cut), never into the next word
        end_value = max(s + AFTER_S, e - 0.02)
        if i + 1 < len(ws):
            end_value = min(end_value, float(ws[i + 1]["start"]))
        end_value = max(end_value, s + 0.05)
        start, how_value = measure_value(db, th, s, after_start, end_value)
        out.append({"i": i, "text": w.get("text", ""), "whisper": round(s, 3), "start": start,
                    "end": round(max(e, start), 3), "dif": round(start - s, 3), 'method': how_value})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Measure word onsets from the waveform; Whisper can lead by up to 0.3 seconds.')
    ap.add_argument('edit', nargs="?", type=Path, help='cut edit folder containing cut.mp4 and transcripts/cut.json')
    ap.add_argument("--video", type=Path, help='another video; requires --transcript', dest='video')
    ap.add_argument("--transcript", type=Path, help='video transcript: transcripts/<name>.json', dest='transcript_value')
    ap.add_argument("--word", action="append", default=[], help='only this word; repeatable and accent-insensitive', dest='word_value')
    ap.add_argument("--start", type=float, help='only words starting at or after this second', dest='start_value')
    ap.add_argument("--end", type=float, help='only words before this second', dest='end_value')
    ap.add_argument("--all", action="store_true", help='show all words, not only adjusted ones', dest='all_value')
    ap.add_argument("--threshold", type=float, help='pause threshold in dB (default: measured from the waveform)', dest='threshold_value')
    ap.add_argument("-o", "--output", type=Path, help='output JSON (default: <edit>/waveform_words.json)', dest='output_value')
    a = ap.parse_args(argv)

    if a.edit:
        edit = a.edit.resolve()
        video = a.video or edit / "cut.mp4"
        tr = a.transcript_value or edit / "transcripts" / f'{Path(video).stem}.json'
        output_value = a.output_value or edit / "waveform_words.json"
    elif a.video and a.transcript_value:
        video, tr = a.video, a.transcript_value
        output_value = a.output_value or Path(tr).with_name(Path(tr).stem + "_waveform.json")
    else:
        ap.error('provide the edit folder, or --video and --transcript')
    video, tr = Path(video), Path(tr)
    for p in (video, tr):
        if not p.is_file():
            print(f'ERROR: not found {p}' + (" (run audit_cut.py first to transcribe the cut)"
                                            if p == tr else ""), file=sys.stderr)
            return 2
    data = load_json(tr)
    words = data.get("words", []) if isinstance(data, dict) else []
    if not words:
        print(f'ERROR: {tr.name} contains no words', file=sys.stderr)
        return 2
    db = waveform.envelope(video, None)
    th = a.threshold_value if a.threshold_value is not None else waveform.threshold(db)
    res = measure_words(db, th, words)
    save_json(output_value, {"video": str(video), "transcript": str(tr), "thresholdDb": th, "words": res})

    target_value = {_normal(x) for x in a.word_value}
    def wants(r: dict) -> bool:
        if target_value and _normal(r["text"]) not in target_value:
            return False
        if a.start_value is not None and r["start"] < a.start_value - 0.3:
            return False
        if a.end_value is not None and r["start"] > a.end_value:
            return False
        return a.all_value or bool(target_value) or a.start_value is not None or a.end_value is not None or abs(r["dif"]) >= SHOW_S
    lines_value = [r for r in res if wants(r)]
    n_mov = sum(1 for r in res if abs(r["dif"]) >= SHOW_S)
    print(f'{video.name}: {len(res)} words, {n_mov} with onset at least 0.05 seconds from Whisper timing; pause threshold {th:.0f} dB) · {output_value}')
    if lines_value:
        print(f"  {'waveform':>8s}  {'whisper':>8s}  {'diff':>6s}  {'method':7s}  word")
    for r in lines_value:
        print(f"  {fmt_time(r['start']):>8s}  {fmt_time(r['whisper']):>8s}  {r['dif']:+.2f}  {r['method']:7s}  {r['text']}")
    if target_value and not lines_value:
        print('  no matching word found; check the spelling in the cut transcript')
    print('  use waveform-measured seconds in the project for word-aligned animation, sound, or cuts.')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
