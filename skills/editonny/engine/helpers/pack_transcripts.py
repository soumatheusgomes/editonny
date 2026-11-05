# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
"""Pack the word-level transcripts of <edit>/transcripts/ into one phrase-level reading view.

Words are grouped into phrases, breaking on any gap >= --silence seconds between words.
Each phrase gets a [start-end] prefix in source seconds. This is the file the agent reads
to pick cuts: a fraction of the tokens of the raw JSON, with word-boundary precision.

Only source transcripts are packed; cut.json (the transcript of the cut) is skipped. With `--cut`,
or when cut.json is the only transcript in the folder, the speech of the cut is packed instead: short
phrases (a pause of 0.3 s, a sentence end or 12 words) with the time each one starts in cut.mp4, to read
against the `quote` of every range.

Output: <edit>/takes_packed.md (the cut: <edit>/cut_speech.md)

Usage:
    uv run --directory '<SKILL>' python helpers/pack_transcripts.py --edit-dir <edit_dir> [--silence 0.5] [-o FILE]
    uv run --directory '<SKILL>' python helpers/pack_transcripts.py --edit-dir <edit_dir> --cut
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKIP_STEMS = {"cut"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
RUN_FIRST = (f"Run first: uv run --directory '{SKILL_ROOT}' python helpers/transcribe_batch.py '<VIDEOS>' --edit-dir '<EDIT>'")
CUT_STEM = "cut"
CUT_SILENCE_S = 0.3        # the cut has joints of ~0.12 s: a shorter pause breaks the phrase
CUT_MAX_WORDS = 12


def format_time(seconds: float) -> str:
    """Seconds as zero-padded "NNN.NN" (fixed width for alignment)."""
    return f'{max(0.0, seconds):06.2f}'


def format_duration(seconds: float) -> str:
    """Seconds with one decimal, as in the contract ("70.8s")."""
    return f'{seconds:.1f}s'


def _plural(n: int, one: str, many: str) -> str:
    return f'{n} {(one if n == 1 else many)}'


_SENTENCE_END = (".", "!", "?", "…")


def group_into_phrases(words: list[dict], silence: float = 0.5, segment_starts: set[float] | None = None,
                       sentence_breaks: bool = True) -> list[dict]:
    """Break the word list into phrases. Returns [{start, end, text, words}].

    Breaks on a gap >= silence between words. Fluent ad speech rarely pauses that long,
    so it also breaks after a sentence end (. ! ? …) and where Whisper started a new
    segment (segment_starts, times rounded to 3 decimals).
    """
    phrases: list[dict] = []
    current: list[dict] = []
    prev_end: float | None = None
    seg_starts = segment_starts or set()

    def flush() -> None:
        nonlocal current
        parts = [(w.get("text") or "").strip() for w in current]
        parts = [p for p in parts if p]
        if parts:
            phrases.append({
                "start": float(current[0]["start"]),
                "end": float(max(w.get("end", w["start"]) for w in current)),
                "text": " ".join(parts),
                "words": current,
            })
        current = []

    for w in words:
        start = w.get("start")
        if start is None:
            continue
        if current and prev_end is not None:
            last_text = (current[-1].get("text") or "").strip()
            if (start - prev_end >= silence
                    or (sentence_breaks and last_text.endswith(_SENTENCE_END))
                    or round(float(start), 3) in seg_starts):
                flush()
        current.append(w)
        prev_end = max(prev_end or 0.0, w.get("end", start))
    flush()
    return phrases


def split_long(phrases: list[dict], max_words: int = CUT_MAX_WORDS) -> list[dict]:
    """Phrases of at most max_words, broken after a comma when one is near the limit (a line you read at once)."""
    out: list[dict] = []
    for p in phrases:
        words = [w for w in p["words"] if (w.get("text") or "").strip()]
        while len(words) > max_words:
            cut = max_words
            for k in range(max_words, max(1, max_words // 2) - 1, -1):
                if (words[k - 1].get("text") or "").strip().endswith((",", ";", ":")):
                    cut = k
                    break
            out.append(_phrase(words[:cut]))
            words = words[cut:]
        if words:
            out.append(_phrase(words))
    return out


def _phrase(words: list[dict]) -> dict:
    return {"start": float(words[0]["start"]), "end": float(max(w.get("end", w["start"]) for w in words)),
            "text": " ".join((w.get("text") or "").strip() for w in words), "words": words}


def cut_phrases(data: dict, silence: float = CUT_SILENCE_S, max_words: int = CUT_MAX_WORDS) -> list[dict]:
    """Short phrases of the transcript of the cut (used by --cut and by audit_cut.py)."""
    return split_long(phrases_for_transcript(data, silence), max_words)


def render_cut_markdown(data: dict, phrases: list[dict]) -> str:
    n_words = len(data.get("words", []))
    lines = [f"# Cut speech · {Path(str(data.get('source') or 'cut.mp4')).name}, {format_duration(float(data.get('duration') or 0))}, {_plural(n_words, 'word', 'words')}",
             'Time is the phrase onset in the cut. Transcription may lead by 0.3 seconds; measure animation timing from the waveform.', ""]
    if not phrases:
        lines.append("  _no speech detected_")
    for p in phrases:
        lines.append(f"  [{format_time(p['start'])}] {p['text']}")
    return "\n".join(lines) + "\n"


def source_transcripts(transcripts_dir: Path) -> list[Path]:
    return sorted((p for p in transcripts_dir.glob("*.json")
                   if p.stem not in SKIP_STEMS and not p.name.startswith(".")),
                  key=lambda p: p.stem.lower())


def phrases_for_transcript(data: dict, silence: float = 0.5) -> list[dict]:
    """Phrases of a word-level transcript dict (used by pack and voice_levels)."""
    words = sorted(data.get("words", []), key=lambda w: w.get("start", 0.0))
    seg_starts = {round(float(s["start"]), 3) for s in data.get("segments", []) if "start" in s}
    return group_into_phrases(words, silence, seg_starts)


def pack_one_file(json_path: Path, silence: float) -> tuple[str, float, list[dict]]:
    """(stem, source duration, phrases) for one transcript file."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    phrases = phrases_for_transcript(data, silence)
    duration = float(data.get("duration") or (phrases[-1]["end"] if phrases else 0.0))
    return data.get("stem") or json_path.stem, duration, phrases


def render_markdown(entries: list[tuple[str, float, list[dict]]]) -> str:
    total = sum(e[1] for e in entries)
    lines = [f"# Takes · {_plural(len(entries), 'source', 'sources')}, {format_duration(total)} total", ""]
    for name, duration, phrases in entries:
        lines.append(f"## {name}  ({format_duration(duration)}, {_plural(len(phrases), 'phrase', 'phrases')})")
        if not phrases:
            lines.append("  _no speech detected_")
        for p in phrases:
            lines.append(f"  [{format_time(p['start'])}-{format_time(p['end'])}] {p['text']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description='Combine transcripts into takes_packed.md, one phrase per line.')
    ap.add_argument("--edit-dir", type=Path, required=True, help='edit folder containing transcripts/', dest='edit_dir')
    ap.add_argument("--silence", type=float, default=0.5,
                    help='split phrases at pauses at least this many seconds long (default 0.5)', dest='silence')
    ap.add_argument("-o", "--output", type=Path, default=None, help='output file (default: <edit-dir>/takes_packed.md)', dest='output')
    ap.add_argument("--cut", action="store_true",
                    help='pack the cut transcript into cut_speech.md with phrase onset times', dest='cut_value')
    args = ap.parse_args()

    edit_dir = args.edit_dir.absolute()
    transcripts_dir = edit_dir / "transcripts"
    if not transcripts_dir.is_dir():
        sys.exit(f'transcript folder not found: {transcripts_dir}\n{RUN_FIRST}')
    if args.silence <= 0:
        sys.exit('--silence must be positive')
    files = source_transcripts(transcripts_dir)
    cut_json = transcripts_dir / f'{CUT_STEM}.json'
    if args.cut_value or (not files and cut_json.is_file()):
        if not cut_json.is_file():
            sys.exit(f"could not find {cut_json}\nRun first: uv run --directory '{SKILL_ROOT}' python helpers/audit_cut.py '{edit_dir}'")
        if not args.cut_value:
            print('only the cut transcript is present; packing cut speech. Run transcribe_batch.py on source videos to include original takes', file=sys.stderr)
        data = json.loads(cut_json.read_text(encoding="utf-8"))
        silence = args.silence if args.silence != 0.5 else CUT_SILENCE_S
        phrases = cut_phrases(data, silence)
        out_path = (args.output or edit_dir / "cut_speech.md").absolute()
        out_path.write_text(render_cut_markdown(data, phrases), encoding="utf-8")
        print(f"cut speech: {_plural(len(phrases), 'phrase', 'phrases')}, {format_duration(float(data.get('duration') or 0))} · {out_path}")
        return
    if not files:
        sys.exit(f'no transcript in {transcripts_dir}\n{RUN_FIRST}')

    entries = []
    for path in files:
        try:
            entries.append(pack_one_file(path, args.silence))
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            print(f'notice: skipped {path.name} (invalid file: {exc})', file=sys.stderr)
    if not entries:
        sys.exit('no valid transcript to combine')

    out_path = (args.output or edit_dir / "takes_packed.md").absolute()
    out_path.write_text(render_markdown(entries), encoding="utf-8")
    n_phrases = sum(len(e[2]) for e in entries)
    total = sum(e[1] for e in entries)
    print(f"{_plural(len(entries), 'source', 'sources')}, {_plural(n_phrases, 'phrase', 'phrases')}, {format_duration(total)} total · {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
