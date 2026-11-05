"""Caption cues on the cut timeline.

Where the words come from (`--from`, default `auto`):
  - sources: the word-level transcripts of the source videos (`transcripts/<stem>.json`, made with
    the brand-name prompt and already read in Phase 1), mapped onto cut.mp4 through
    `cut_timeline.json` (a word belongs to a segment when its middle falls inside the segment's
    audio; it is shifted by audio_out_start - audio_src_start and kept inside that audio). No new
    transcription is needed after a recut, and a word the cut chopped at a range edge is reported.
  - cut: a transcript of cut.mp4 itself (`--transcript`, default `transcripts/cut.json`).
  - auto: sources for every segment whose source transcript exists, cut.json for the rest.

Then `--replace "errado=certo"` (repeatable; several words allowed on each side) fixes words, and
the words are grouped into cues by measured width (with the chosen style's font), max words,
punctuation and pauses. A cue never spans a cut boundary. Words keep their original case; the cue
`text` is shown the way the style displays it (casing and punctuation), for the preview.

captions.json also records `words_from`, `timeline_key` (a fingerprint of the cut positions: the
composer refuses captions made for another cut), `duration`, `replacements`, `warnings` and
`differences` ([{"time", "sources", "cut"}]: places where the source transcripts and
transcripts/cut.json heard different words, printed as `check:` lines so the agent picks the right
one with --replace). A cut.json whose duration does not match cut_timeline.json is from another cut
and is not used.

Usage:
    uv run python helpers/captions.py --edit-dir DIR [-o DIR/captions.json] [--style ID]
        [--from auto|sources|cut] [--transcript DIR/transcripts/cut.json]
        [--replace "errado=certo" ...] [--max-words 3] [--pause 0.35]
    (the older form `--transcript edit/transcripts/cut.json -o edit/captions.json` still works and
    finds the edit dir from the transcript path; with --from auto it still prefers the sources)
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUTPUT_FORMATS, fmt_time, load_json, load_style_catalog, now_iso, save_json  # noqa: E402
import text_render as tr  # noqa: E402

PAUSE_CLOSE = 0.35
SENTENCE_END = (".", "!", "?", "…")
CLAUSE_END = (",", ";", ":")
MIN_WORD = 0.04
MIN_ACTIVE = 0.07          # a word pushed to a cut still gets about 2 frames of highlight
SHORT_CUE = 0.25           # one-word cues shorter than this join a neighbour
WIDTH_TOLERANCE = 1.08      # up to 8% wider than the safe width: the renderer shrinks slightly
WEAK_PAIR_TOLERANCE = 1.4   # "o acabamento", "as influenciadoras" stay together


def _normalize_words(raw: list[dict]) -> list[dict]:
    words = []
    for w in raw or []:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        try:
            start, end = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        words.append({"text": text, "start": start, "end": end})
    words.sort(key=lambda w: w["start"])
    for i, w in enumerate(words):
        if i and w["start"] < words[i - 1]["end"]:
            # overlapping whisper times: split the difference, keep order
            mid = (w["start"] + words[i - 1]["end"]) / 2.0
            words[i - 1]["end"] = max(words[i - 1]["start"] + MIN_WORD, mid)
            w["start"] = max(w["start"], words[i - 1]["end"])
        if w["end"] < w["start"] + MIN_WORD:
            w["end"] = w["start"] + MIN_WORD
    return words


def timeline_key(timeline: dict | None) -> str | None:
    """Fingerprint of where every segment sits in cut.mp4 and in its source. It changes when the
    cut changes, and not when only the grade, gain or labels of the EDL change."""
    if not timeline or not timeline.get("segments"):
        return None
    rows = []
    for s in timeline.get("segments") or []:
        try:
            rows.append((str(s.get("source")), round(float(s.get("audio_src_start", s["src_start"])), 3),
                         round(float(s["src_end"]), 3), round(float(s["out_start"]), 3), round(float(s["out_end"]), 3),
                         round(float(s.get("audio_out_start", s["out_start"])), 3)))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[3])
    return hashlib.sha1(repr(rows).encode("utf-8")).hexdigest()[:16]


def _timeline_segments(timeline: dict | None) -> list[dict]:
    segs = []
    for s in (timeline or {}).get("segments", []) or []:
        try:
            a, b = float(s["out_start"]), float(s["out_end"])
            aa = min(a, float(s.get("audio_out_start", a)))
            ab = float(s.get("audio_out_end", b))
            src_a = float(s.get("audio_src_start", float(s["src_start"]) - (a - aa)))
        except (KeyError, TypeError, ValueError):
            continue
        segs.append({"id": str(s.get("id") or ""), "source": str(s.get("source") or ""), "out_start": a, "out_end": b,
                     "audio_out_start": aa, "audio_out_end": max(ab, b), "audio_src_start": src_a})
    segs.sort(key=lambda s: s["out_start"])
    return segs


CHOP_KEEP = 0.6   # a word with less than this share of its time inside the range was chopped by the cut
CUT_DURATION_TOLERANCE = 0.1   # s: a cut transcript whose duration differs more than this is from another cut
MAX_DIFFERENCES = 12


def load_cut_words(path: Path | None, timeline: dict | None) -> tuple[list[dict] | None, str | None]:
    """Words of transcripts/cut.json, or (None, reason) when it is missing or was made for another cut
    (its duration does not match cut_timeline.json)."""
    data = load_json(path) if path and path.exists() else None
    if not isinstance(data, dict) or not data.get("words"):
        return None, None
    dur, tl_dur = data.get("duration"), (timeline or {}).get("duration")
    try:
        if dur is not None and tl_dur is not None and abs(float(dur) - float(tl_dur)) > CUT_DURATION_TOLERANCE:
            return None, (f'{path.name} belongs to another cut ({float(dur):.2f}s and the cut has {float(tl_dur):.2f}s); this transcript was ignored. Transcribe cut.mp4 again if needed')
    except (TypeError, ValueError):
        pass
    return _normalize_words(data["words"]), None


def transcript_differences(words: list[dict], cut_words: list[dict]) -> list[dict]:
    """Places where the source transcripts and the cut transcript heard different words (swaps of up
    to 3 words at about the same time). Either side may be the right one: the agent checks."""
    a = [_normal_token(w["text"]) for w in words]
    b = [_normal_token(w["text"]) for w in cut_words]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "replace" or i2 - i1 > 3 or j2 - j1 > 3 or any(w.get("replaced") for w in words[i1:i2]):
            continue   # words fixed with --replace are already decided
        if abs(words[i1]["start"] - cut_words[j1]["start"]) > 1.0:
            continue
        src = " ".join(w["text"].strip(".,;:!?…") for w in words[i1:i2])
        cut = " ".join(w["text"].strip(".,;:!?…") for w in cut_words[j1:j2])
        if _normal_token(src.replace(" ", "")) == _normal_token(cut.replace(" ", "")):
            continue
        out.append({"time": round(words[i1]["start"], 2), "sources": src, "cut": cut})
    return out


def words_from_sources(edit_dir: Path, timeline: dict, cut_transcript: Path | None, mode: str
                       ) -> tuple[list[dict], str, list[str]]:
    """Words on the cut timeline, each with its segment index ("seg").

    -> (words, words_from, warnings). mode "sources" requires every source transcript; "auto" uses
    the cut transcript for segments without one.
    """
    segs = _timeline_segments(timeline)
    cache: dict[str, list[dict] | None] = {}
    cut_words: list[dict] | None = None
    cut_problem: str | None = None
    words: list[dict] = []
    warnings: list[str] = []
    used = set()
    for i, seg in enumerate(segs):
        stem = seg["source"]
        if stem not in cache:
            data = load_json(edit_dir / "transcripts" / f'{stem}.json') if stem and stem != "cut" else None
            cache[stem] = _normalize_words(data.get("words", [])) if isinstance(data, dict) and data.get("words") else None
        src_words = cache[stem]
        aa, ab = seg["audio_out_start"], seg["audio_out_end"]
        if src_words is None:
            if mode == "sources":
                raise ValueError(f"missing source transcript: {stem!r} (transcripts/{stem}.json) of interval {seg['id']}. Run transcribe.py on that source or use --from auto")
            if cut_words is None:
                cut_words, cut_problem = load_cut_words(cut_transcript, timeline)
                cut_words = cut_words or []
                if cut_problem:
                    warnings.append(cut_problem)
            if not cut_words:
                warnings.append(f"interval {seg['id']} ({fmt_time(seg['out_start'])}) has no captions because its source transcript is missing: {stem!r} nor {('one for the current cut' if cut_problem else 'one for the cut')}")
                continue
            used.add("cut")
            for w in cut_words:
                if aa <= (w["start"] + w["end"]) / 2.0 < ab:
                    words.append(dict(w, seg=i))
            continue
        used.add("sources")
        shift = aa - seg["audio_src_start"]
        s0, s1 = seg["audio_src_start"], seg["audio_src_start"] + (ab - aa)
        for w in src_words:
            ws, we = w["start"], w["end"]
            if we <= s0 or ws >= s1:
                continue
            # Whisper stretches a word back over the pause before it (the first word of a file starts
            # at 0.0, the word after a pause starts where the previous one ended). A word crossing an
            # edge is judged by a plausible length for its letters, ending where Whisper ends it.
            plausible = 0.12 + 0.07 * max(1, len(re.sub(r"\W", "", w["text"])))
            es, ee = ws, we
            if ws < s0 or we > s1:
                es = max(ws, we - plausible)
            inside = max(0.0, min(ee, s1) - max(es, s0)) / max(1e-3, ee - es)
            if inside < 0.5:
                if inside > 1.0 - CHOP_KEEP:
                    edge = 'at the beginning' if es < s0 else 'at the end'
                    warnings.append(f'''the word "{w['text']}" lies almost entirely outside interval {seg['id']} ({edge}, {fmt_time(aa if es < s0 else ab)}): inspect the EDL boundary''')
                continue
            if inside < CHOP_KEEP:
                edge = 'at the beginning' if es < s0 else 'at the end'
                warnings.append(f'''the word "{w['text']}" was clipped {edge} of interval {seg['id']} ({fmt_time(aa if es < s0 else ab)}): inspect the EDL boundary''')
            words.append({"text": w["text"], "start": min(max(aa, es + shift), ab - MIN_WORD),
                          "end": max(min(ab, ee + shift), aa + MIN_WORD), "seg": i})
    words.sort(key=lambda w: (w["start"], w["seg"]))
    return words, "+".join(sorted(used, reverse=True)) or "none", warnings


def _normal_token(text: str) -> str:
    t = unicodedata.normalize("NFC", str(text)).lower()
    return t.strip(".,;:!?…\"'“”‘’()[]{}«»-–—")


def parse_replacements(values: list[str] | None) -> list[tuple[list[str], list[str], str]]:
    out = []
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f'--replace must use "incorrect=correct" (received {raw!r})')
        left, right = raw.split("=", 1)
        src = [t for t in (_normal_token(x) for x in left.split()) if t]
        if not src:
            raise ValueError(f"--replace is missing the original word before '=' ({raw!r})")
        out.append((src, right.split(), raw))
    return out


def apply_replacements(words: list[dict], reps: list[tuple[list[str], list[str], str]]) -> tuple[list[dict], list[dict]]:
    """Swap word sequences (case and edge punctuation ignored when matching). The new words share
    the time of the old ones, split by length; trailing punctuation of the last old word is kept."""
    report = []
    for src, dst, raw in reps:
        count, i, out = 0, 0, []
        n = len(src)
        while i < len(words):
            chunk = words[i:i + n]
            if (len(chunk) == n and all(_normal_token(w["text"]) == t for w, t in zip(chunk, src))
                    and len({w.get("seg") for w in chunk}) == 1):
                count += 1
                if dst:
                    t0, t1 = chunk[0]["start"], chunk[-1]["end"]
                    tail = re.search(r"[.,;:!?…]+$", chunk[-1]["text"])
                    new = list(dst)
                    if tail and not re.search(r"[.,;:!?…]$", new[-1]):
                        new[-1] += tail.group(0)
                    total = sum(len(x) for x in new) or 1
                    t = t0
                    for k, token in enumerate(new):
                        t_end = t1 if k == len(new) - 1 else t + (t1 - t0) * len(token) / total
                        out.append(dict(chunk[0], text=token, start=t, end=t_end, replaced=True))
                        t = t_end
                i += n
                continue
            out.append(words[i])
            i += 1
        words = out
        report.append({"from": raw.split("=", 1)[0], "to": raw.split("=", 1)[1], "count": count})
    return words, report


def _segments_from_timeline(timeline: dict | None) -> list[tuple[float, float, float]]:
    """(out_start, out_end, audio_out_start) per segment. With a J-cut the audio of a segment
    starts a few frames before its picture, so words spoken in that lead belong to it."""
    if not timeline:
        return []
    segs = []
    for s in timeline.get("segments", []) or []:
        try:
            a, b = float(s["out_start"]), float(s["out_end"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            aa = min(a, float(s.get("audio_out_start", a)))
        except (TypeError, ValueError):
            aa = a
        segs.append((a, b, aa))
    segs.sort()
    return segs


def _segment_index(t: float, segs: list[tuple[float, float, float]]) -> int:
    """Index of the segment whose audio contains time t. Inside a J-cut lead (two segments'
    audio overlap) the incoming segment wins. Nearest segment when t falls in a gap."""
    if not segs:
        return 0
    for i in range(len(segs) - 1, -1, -1):
        a, b, aa = segs[i]
        if aa <= t < b:
            return i
    best, best_d = 0, None
    for i, (a, b, _aa) in enumerate(segs):
        d = min(abs(t - a), abs(t - b))
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def _ends_with(text: str, marks: tuple[str, ...]) -> bool:
    t = text.rstrip("\"'”’)]»")
    return t.endswith(marks)


def build_cues(words: list[dict], style_id: str, max_words: int, canvas: tuple[int, int],
               segs: list[tuple[float, float]], pause: float = PAUSE_CLOSE) -> list[dict]:
    info = tr.caption_style_info(style_id)
    lines = max(1, int(info["lines"]))
    limit = tr.caption_width_limit(style_id, canvas)
    # multi-line styles fill up to `lines` lines; keep a margin so the balanced split fits
    width_budget = limit if lines == 1 else limit * lines * 0.88

    def fits(group: list[dict]) -> bool:
        if len(group) <= 1 or info["one_word"]:
            return True
        width = tr.measure_caption_width([w["text"] for w in group], style_id, canvas)
        if width <= width_budget * WIDTH_TOLERANCE:
            return True
        # a lone function word reads badly: keep it with the next word and let the renderer shrink
        return len(group) == 2 and tr.is_weak_word(group[0]["text"]) and width <= width_budget * WEAK_PAIR_TOLERANCE

    cues: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if not isinstance(w.get("seg"), int) or not 0 <= w["seg"] < max(1, len(segs)):
            w["seg"] = _segment_index((w["start"] + w["end"]) / 2.0, segs)
        if cur:
            prev = cur[-1]
            # one-line styles breathe on every comma; subtitle-like styles only on longer clauses
            clause = _ends_with(prev["text"], CLAUSE_END) and (lines == 1 or len(cur) >= 4)
            hard = (w["start"] - prev["end"] >= pause or w["seg"] != prev["seg"]
                    or _ends_with(prev["text"], SENTENCE_END) or clause)
            if hard:
                cues.append(cur)
                cur = []
            elif len(cur) >= max_words or not fits(cur + [w]):
                # soft break: do not leave a function word hanging at the end of a cue
                carry = []
                if (len(cur) >= 2 and max_words >= 2 and tr.is_weak_word(cur[-1]["text"])
                        and not tr.is_weak_word(cur[-2]["text"]) and fits([cur[-1], w])):
                    carry = [cur.pop()]
                cues.append(cur)
                cur = carry
        cur.append(w)
    if cur:
        cues.append(cur)

    # an orphan word closing a sentence ("... na próxima | página.") pulls words from the cue before
    for i in range(1, len(cues)):
        prev, group = cues[i - 1], cues[i]
        if len(group) != 1 or len(prev) < 3 or max_words < 2:
            continue
        if (group[0]["seg"] != prev[-1]["seg"] or group[0]["start"] - prev[-1]["end"] >= pause
                or _ends_with(prev[-1]["text"], SENTENCE_END + CLAUSE_END)):
            continue
        moved = 0
        while len(prev) > 2 and moved < 3 and len(group) < max_words:
            trial = [prev[-1]] + group
            if not fits(trial):
                break
            group.insert(0, prev.pop())
            moved += 1
            if not tr.is_weak_word(prev[-1]["text"]):
                break

    # a one-word cue shorter than SHORT_CUE ("ó,") flickers: join it to a neighbour in the same
    # segment when that stays within max words and width
    i = 0
    while i < len(cues):
        group = cues[i]
        dur = group[-1]["end"] - group[0]["start"]
        if len(group) == 1 and dur < SHORT_CUE and len(cues) > 1:
            nxt = cues[i + 1] if i + 1 < len(cues) else None
            prv = cues[i - 1] if i > 0 else None
            if (nxt and nxt[0]["seg"] == group[0]["seg"] and nxt[0]["start"] - group[-1]["end"] < pause
                    and len(nxt) < max(2, max_words) and fits(group + nxt)
                    and not _ends_with(group[-1]["text"], SENTENCE_END)):
                cues[i + 1] = group + nxt
                del cues[i]
                continue
            if (prv and prv[-1]["seg"] == group[0]["seg"] and group[0]["start"] - prv[-1]["end"] < pause
                    and len(prv) < max(2, max_words) and fits(prv + group)):
                prv.extend(group)
                del cues[i]
                continue
        i += 1

    out = []
    for group in cues:
        start, end = group[0]["start"], group[-1]["end"]
        if segs:
            a, b, _aa = segs[group[0]["seg"]]
            start, end = max(start, a), min(end, b)
            if end <= start:
                end = start + MIN_WORD
        # words spoken in a J-cut lead start before the picture cut: keep them inside the cue and
        # give each at least MIN_ACTIVE so the highlight still lands on it
        timed = []
        floor = start
        for k, w in enumerate(group):
            ws, we = w["start"], w["end"]
            if ws < floor:
                ws = floor
                we = max(we, ws + MIN_ACTIVE)
            elif we - ws < MIN_ACTIVE and k + 1 < len(group):
                # a near zero-length word ("ó,") borrows the gap before the next word
                we = max(we, min(ws + MIN_ACTIVE, group[k + 1]["start"]))
            ws = min(ws, end)
            we = min(max(we, ws + MIN_WORD), end)
            timed.append({"text": w["text"], "start": round(ws, 3), "end": round(we, 3)})
            floor = we
        disp = tr.display_words([w["text"] for w in group], style_id)
        out.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": " ".join(disp),
            "words": timed,
        })
    return out


def drop_already_captioned(cues: list[dict], screen_value: dict | None, coverage_value: float = 0.45) -> tuple[list[dict], list[dict]]:
    'Exclude speech already written on screen, including burned-in captions and visual lettering.'
    if not screen_value or not screen_value.get('existing_captions'):
        return cues, []
    import screen_text as tnt  # noqa: PLC0415

    intervals = screen_value['existing_captions']
    retained, exits = [], []
    for cue in cues:
        (exits if tnt.covers(intervals, float(cue["start"]), float(cue["end"]), coverage_value) else retained).append(cue)
    return retained, exits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Generate captions.json on the cut timeline using source transcripts or the cut transcript.')
    ap.add_argument("--edit-dir", help='session edit folder (default: parent of the --transcript transcripts/ directory)', dest='edit_dir')
    ap.add_argument("--transcript", help='cut.mp4 transcript for --from cut or intervals without a source (default: edit/transcripts/cut.json)', dest='transcript')
    ap.add_argument("--from", dest='words_from', choices=("auto", "sources", "cut"), default="auto",
                    help='word source: auto (sources, then cut fallback), sources, or cut (default: auto)')
    ap.add_argument("--replace", action="append", metavar="WRONG=CORRECT",
                    help="replace a misheard word, for example --replace 'Cacto=Cakto' (repeatable; supports multiple words)", dest='replace')
    ap.add_argument("-o", "--output", help='output file (default: edit/captions.json)', dest='output')
    ap.add_argument("--style", help='caption style (default: catalog choice)', dest='style')
    ap.add_argument("--max-words", type=int, help='maximum words per caption (default: style value, 3 for animated styles)', dest='max_words')
    ap.add_argument("--screen", help='existing screen text map (default: edit/screen_text.json, generated by screen_text.py); use --screen no to ignore it', dest='screen_value')
    ap.add_argument("--pause", type=float, default=PAUSE_CLOSE, help='pause in seconds that closes a caption (default: 0.35)', dest='pause')
    args = ap.parse_args(argv)

    if not args.edit_dir and not args.transcript:
        print('Error: provide --edit-dir or --transcript with the cut transcript', file=sys.stderr)
        return 2
    tpath = Path(args.transcript).expanduser().resolve() if args.transcript else None
    if args.edit_dir:
        edit_dir = Path(args.edit_dir).expanduser().resolve()
    else:
        edit_dir = tpath.parent.parent if tpath.parent.name == "transcripts" else tpath.parent
    if not edit_dir.is_dir():
        print(f'error: edit folder not found: {edit_dir}', file=sys.stderr)
        return 2
    if tpath is None:
        tpath = edit_dir / "transcripts" / "cut.json"
    try:
        reps = parse_replacements(args.replace)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    timeline = load_json(edit_dir / "cut_timeline.json")
    segs = _segments_from_timeline(timeline)
    warnings: list[str] = []
    if args.words_from != "cut" and timeline and segs:
        try:
            words, words_from, warnings = words_from_sources(edit_dir, timeline, tpath, args.words_from)
        except ValueError as e:
            print(f'error: {e}', file=sys.stderr)
            return 2
        for w in words:
            if w["end"] < w["start"] + MIN_WORD:
                w["end"] = w["start"] + MIN_WORD
    elif args.words_from == "sources":
        print(f'Error: --from sources requires cut_timeline.json in {edit_dir}. Render the cut first.', file=sys.stderr)
        return 2
    else:
        if not tpath.exists():
            print(f'Error: transcript not found: {tpath}', file=sys.stderr)
            return 2
        words = _normalize_words((load_json(tpath) or {}).get("words", []))
        words_from = "cut"
        _, problem = load_cut_words(tpath, timeline)
        if problem:
            warnings.append(problem.replace('this transcript was ignored', "captions may be mistimed"))
    if not words:
        print(f"Error: no timed caption words found ({('cut sources' if words_from != 'cut' else tpath)})",
              file=sys.stderr)
        return 2
    words, rep_report = apply_replacements(words, reps)
    for r in rep_report:
        if not r["count"]:
            warnings.append(f'''--replace '{r['from']}={r['to']}': could not find "{r['from']}" in the speech''')
    differences: list[dict] = []
    if "sources" in words_from:
        cut_words, _ = load_cut_words(tpath, timeline)
        if cut_words:
            differences = transcript_differences(words, cut_words)

    catalog = load_style_catalog()
    style_id = args.style or (catalog.get("defaults") or {}).get("caption_style") or tr.caption_style_ids()[0]
    try:
        info = tr.caption_style_info(style_id)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    max_words = args.max_words if args.max_words else int(info["max_words"])
    if info["one_word"] and not args.max_words:
        max_words = 3   # the style shows one word at a time; cues still group phrases
    if max_words < 1:
        print('Error: --max-words must be at least 1', file=sys.stderr)
        return 2

    canvas = OUTPUT_FORMATS["vertical"]
    if timeline and timeline.get("width") and timeline.get("height"):
        canvas = (int(timeline["width"]), int(timeline["height"]))

    cues = build_cues(words, style_id, max_words, canvas, segs, pause=args.pause)
    screen_value = None
    if (args.screen_value or "").lower() not in ("no", "off"):
        screen_path = Path(args.screen_value).expanduser().resolve() if args.screen_value else edit_dir / "screen_text.json"
        screen_value = load_json(screen_path) if screen_path.exists() else None
        if screen_value and screen_value.get("video_sig") and timeline and timeline.get("video_sig")\
                and screen_value["video_sig"] != timeline["video_sig"]:
            warnings.append('screen_text.json belongs to another video; run screen_text.py again')
            screen_value = None
    cues, already_written = drop_already_captioned(cues, screen_value)
    boundaries = [round(seg[0], 3) for seg in segs[1:]]
    out = {
        "source": str((timeline or {}).get("video") or "cut.mp4"),
        "style": style_id,
        "max_words": max_words,
        "canvas": list(canvas),
        "words_from": words_from,
        "timeline_key": timeline_key(timeline),
        "duration": (timeline or {}).get("duration"),
        "cut_boundaries": boundaries,
        "replacements": rep_report,
        "differences": differences,
        "warnings": warnings,
        "existing_captions": [{"start": c["start"], "end": c["end"],
                         "text": " ".join(w["text"] for w in c["words"])} for c in already_written],
        "created_at": now_iso(),
        "cues": cues,
    }
    opath = Path(args.output).expanduser().resolve() if args.output else edit_dir / "captions.json"
    save_json(opath, out)

    state_path = edit_dir / "state.json"
    if state_path.exists() and opath.parent == edit_dir:
        state = load_json(state_path, default={}) or {}
        state["captions"] = opath.name
        state["updated_at"] = now_iso()
        save_json(state_path, state)

    n_words = sum(len(c["words"]) for c in cues)
    avg = n_words / len(cues) if cues else 0
    extra = f', respecting {len(boundaries)} cuts' if segs else ""
    origin = {"sources": 'source transcripts', "cut": 'cut transcript',
              "sources+cut": 'source and cut transcripts'}.get(words_from, words_from)
    for r in rep_report:
        if r["count"]:
            print(f'''troca: "{r['from']}" -> "{r['to']}" ({r['count']}x)''')
    for msg in warnings:
        print(f'notice: {msg}')
    for d in differences[:MAX_DIFFERENCES]:
        print(f"""check: {fmt_time(d['time'])} sources "{d['sources']}", cut transcript "{d['cut']}" (if the cut is correct: --replace '{d['sources']}={d['cut']}')""")
    if len(differences) > MAX_DIFFERENCES:
        print(f'check: {len(differences) - MAX_DIFFERENCES} more differences (see differences in captions.json)')
    if already_written:
        seconds_value = sum(c["end"] - c["start"] for c in already_written)
        print(f'skipped {len(already_written)} caption(s) ({seconds_value:.0f}s) where the video already displays the spoken phrase (screen_text.json)')
    print(f'ok: {len(cues)} captions, {n_words} words (average {avg:.1f} per caption), style {style_id}{extra}, words: {origin} -> {opath}')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
