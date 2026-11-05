"""Draft a cut from measured speech blocks and identify likely retakes.

Split sources at measured pauses, compare nearby phrase openings, and flag likely false starts,
repetitions, breaths, and hesitation-only blocks. The heuristic prefers a later matching block;
that is a review candidate, not proof it is the best take. Review automatic-cut.md against the
recording, retain the best complete delivery, and preserve intentional pauses and requested scope.
Refine boundaries with precise_edges.refine_edl and balance measured source voice levels.
Write edl.json with a backup of an existing EDL and automatic-cut.md with dropped-block reasons.

Usage: python helpers/assemble_cut.py --edit-dir EDIT --sources a.mov b.mov --pause 0.35
Use --dry-run to inspect the proposed cut before writing it.
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np

from precise_edges import HOP, OFF_DB, refine_edl, source_levels
from common import load_json, save_json

PAUSE_S = 0.35          # a pause this long splits two blocks
MIN_ISLAND_S = 0.03       # speech islands shorter than this are clicks and join the silence
                        # (0.08 swallowed short words: "ser", "o que", "É")
WINDOW_BLOCKS = 4       # a retake is looked for this many blocks ahead ...
WINDOW_S_VALUE = 25.0         # ... and within this many seconds
SIMILAR = 0.6          # share of the block's words found, in order, at the start of a near later block
SIMILAR_FAR = 0.75   # ... of a later block further away or in a later source (retake of a whole part)
FAR_BLOCKS = 12       # how far ahead, in blocks across sources, a whole-part retake is looked for
MIN_FAR = 5           # blocks shorter than this are never matched far away
NEAR_EQUAL = 0.9       # a block of MIN_LONGE+ words found this complete anywhere later is a retake
SHORT = 3               # a block this short is a false start when all its words open the next block
VOICE_WITHOUT_WORD_S = 0.25   # a block without transcribed words this long ...
VOICE_PEAK_DB = 20.0         # ... and this loud over the floor is a word Whisper misplaced, not a breath
HESITATION = {"é", "e", "ah", "ahn", "hã", "hum", "hm", "uhm", "eh", "éé", "tá", "né", "então", "aí"}
GAIN_DB = 4.0          # ranges this far from the voice reference get their gain_db (verify uses 4 too)
FORMATS_VALUE = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}


def normal_value(word: str) -> str:
    w = unicodedata.normalize("NFD", word.lower())
    w = "".join(c for c in w if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w]", "", w)


def blocks_of(db: np.ndarray, floor: float, pause_value: float) -> list[tuple[float, float]]:
    """Speech blocks (start, end) in seconds, split at quiet runs of at least `pause`."""
    loud = db >= floor + OFF_DB
    # clicks: loud runs shorter than MIN_ILHA_S become silence
    k, n, short = 0, loud.size, int(round(MIN_ISLAND_S / HOP))
    while k < n:
        if loud[k]:
            j = k
            while j < n and loud[j]:
                j += 1
            if j - k < short:
                loud[k:j] = False
            k = j
        else:
            k += 1
    out, k, gap = [], 0, int(round(pause_value / HOP))
    while k < n:
        if not loud[k]:
            k += 1
            continue
        a = k
        quiet = 0
        while k < n and quiet < gap:
            quiet = 0 if loud[k] else quiet + 1
            k += 1
        out.append((a * HOP, (k - quiet) * HOP))
    return out


def words_in(words: list[dict], a: float, b: float) -> list[dict]:
    return [w for w in words if a <= (float(w["start"]) + float(w["end"])) / 2 < b]


def coverage(first: list[str], later: list[str]) -> tuple[float, int]:
    """(share of `first` found in order in `later` in runs of 2+ words, where that match starts)."""
    if not first or not later:
        return 0.0, 0
    sm = difflib.SequenceMatcher(None, first, later, autojunk=False)
    runs = [m for m in sm.get_matching_blocks() if m.size >= 2]
    if not runs:
        return 0.0, 0
    return sum(m.size for m in runs) / len(first), runs[0].b


def retake_of(bl: dict, nxt: list[dict], near: bool) -> tuple[float, int]:
    """(how clearly `bl` is an earlier take of what `nxt` says, which of `nxt` the take starts in);
    (0, 0) if it is not a retake."""
    toks = bl["toks"]
    later = [t for b in nxt for t in b["toks"]]
    if near and len(toks) <= SHORT:
        opening = nxt[0]["toks"][:8]
        hit = opening and all(t in opening for t in toks) and len(opening) > len(toks)
        return (1.0, 0) if hit else (0.0, 0)
    cov, at = coverage(toks, later[: len(toks) + 12])
    if at > 3:
        # the later take must begin with it, not quote it in the middle ... unless it repeats a
        # long stretch almost word for word (a whole sentence said again inside a longer take)
        cov, at = coverage(toks, later)
        if not (len(toks) >= MIN_FAR and cov >= NEAR_EQUAL):
            return 0.0, 0
    which, acc = 0, 0
    for k, b in enumerate(nxt):
        if at < acc + len(b["toks"]):
            which = k
            break
        acc += len(b["toks"])
    if near:
        return (cov, which) if cov >= SIMILAR and cov * len(toks) >= 3 else (0.0, 0)
    return (cov, which) if len(toks) >= MIN_FAR and cov >= SIMILAR_FAR else (0.0, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--edit-dir", type=Path, required=True, dest='edit_dir')
    ap.add_argument("--sources", nargs="*", help='videos in script order (default: transcribed sources sorted by name)', dest='sources')
    ap.add_argument("--pause", type=float, default=PAUSE_S, dest='pause_value')
    ap.add_argument("--format", default=None, choices=sorted(FORMATS_VALUE), dest='format_value')
    ap.add_argument("--dry-run", action="store_true", help='show the plan without writing edl.json', dest='dry_run')
    args = ap.parse_args()
    edit = args.edit_dir
    from project_contract import managed_config, settings_hash
    contract = managed_config(edit, 'cut')

    trs = {}
    for p in sorted((edit / "transcripts").glob("*.json")):
        if p.stem == "cut":
            continue
        d = load_json(p)
        if isinstance(d, dict) and d.get("words") is not None and d.get("source"):
            trs[p.stem] = d
    if args.sources:
        order = [Path(f).stem for f in args.sources]
        missing = [s for s in order if s not in trs]
        if missing:
            print(f"no transcript: {', '.join(missing)} (run transcribe.py first)", file=sys.stderr)
            return 2
    else:
        order = list(trs)
    if not order:
        print(f"no transcript in {edit / 'transcripts'}", file=sys.stderr)
        return 2

    sources, ranges, dropped, envs = {}, [], [], {}
    allb: list[dict] = []          # every block of every source, in script order
    n = 0
    for stem in order:
        tr = trs[stem]
        src = str(tr["source"])
        lv = source_levels(src, print)
        envs[stem] = lv
        sources[stem] = src
        if lv is None:
            print(f'{stem}: kept whole; review it.')
            allb.append({"stem": stem, "a": 0.0, "b": float(tr.get("duration") or 0), "text": "", "toks": ["?"],
                         "whole": True})
            continue
        db, floor = lv
        words = [w for w in tr["words"] if w.get("start") is not None]
        for a, b in blocks_of(db, floor, args.pause_value):
            ws = words_in(words, a, b)
            text = " ".join(str(w.get("text", "")).strip() for w in ws)
            ws = [w for w in ws if normal_value(str(w.get("text", "")))]   # one token per word, aligned
            peak = float(db[int(a / HOP):max(int(a / HOP) + 1, int(b / HOP))].max()) - floor
            allb.append({"stem": stem, "a": a, "b": b, "text": text, "words": ws, "peak": peak,
                         "toks": [normal_value(str(w.get("text", ""))) for w in ws],
                         "cut_off": text.rstrip().endswith(("...", "…", "-"))})

    for i, bl in enumerate(allb):
        bl["keep"], bl["why"] = True, ""
        if bl.get("whole"):
            continue
        if not bl["toks"]:
            if bl["b"] - bl["a"] >= VOICE_WITHOUT_WORD_S and bl.get("peak", 0.0) >= VOICE_PEAK_DB:
                # Whisper put the word somewhere else ("Eu" 1 s early): loud enough to be a word, so it stays
                bl["text"] = '(untranscribed speech: review required)'
                continue
            bl["keep"], bl["why"] = False, 'no speech: breath or noise'
            continue
        if all(t in HESITATION for t in bl["toks"]):
            bl["keep"], bl["why"] = False, f'''hesitation only ("{bl['text']}")'''
            continue
        for j in range(i + 1, min(len(allb), i + 1 + FAR_BLOCKS)):
            later = allb[j]
            if not later["toks"] or later.get("whole"):
                continue
            near = j - i <= WINDOW_BLOCKS and (later["stem"] != bl["stem"] or later["a"] - bl["b"] <= WINDOW_S_VALUE)
            if len(bl["toks"]) <= SHORT and j > i + 1 and near:
                continue      # a false start is redone right after it, not three blocks later ("Não!" ... "que não")
            r, k = retake_of(bl, allb[j:j + 3], near)
            if r:
                later = allb[j + k]
                onde = later["a"] if later["stem"] == bl["stem"] else f"{later['stem']} {later['a']:.2f}"
                bl["keep"] = False
                bl["why"] = (f'''repeated take ({r:.0%} from its beginning at {(onde if isinstance(onde, str) else f'{onde:.2f}')}s: "{later['text'][:60]}")''')
                break

    # a take abandoned at the end of a block with no pause before the retake: "... da Shopee e um
    # mecanismo..." + "E um mecanismo com inteligência artificial". The block ends before it.
    kept = [b for b in allb if b["keep"] and not b.get("whole")]
    for bl, nb in zip(kept, kept[1:]):
        if not bl.get("cut_off") or len(bl["toks"]) < 3 or not nb["toks"]:
            continue
        for k in range(min(6, len(bl["toks"]) - 1), 1, -1):
            tail, head = bl["toks"][-k:], nb["toks"][: k + 2]
            cov, at = coverage(tail, head)
            if cov >= 0.99 and at <= 1:
                t0 = float(bl["words"][-k]["start"])
                if t0 - bl["a"] > 0.3:
                    dropped.append((bl["stem"], {"a": t0, "b": bl["b"], "text": " ".join(
                        str(w.get("text", "")).strip() for w in bl["words"][-k:])},
                        f"end of an abandoned take, redone at {nb['a']:.2f}s"))
                    bl["b"] = t0
                    bl["text"] = " ".join(str(w.get("text", "")).strip() for w in bl["words"][:-k])
                break

    for bl in allb:
        if bl["keep"]:
            n += 1
            ranges.append({"id": f'r{n:02d}', "source": bl["stem"], "start": round(bl["a"], 3),
                           "end": round(bl["b"], 3), "beat": "", "quote": bl["text"][:90],
                           "reason": 'source has no measurable pauses' if bl.get("whole")
                           else 'assemble_cut: final complete take of the phrase', "gain_db": 0})
        else:
            dropped.append((bl["stem"], bl, bl["why"]))

    edl = {"version": 1, "sources": sources,
           "output": ({"format": args.format_value, "width": FORMATS_VALUE[args.format_value][0],
                      "height": FORMATS_VALUE[args.format_value][1]} if args.format_value else {}),
           "grade": "", "jcut": {"lead_frames": 4, "tail_trim_frames": 2}, "ranges": ranges}
    if contract:
        video = contract['workflow']['video']
        edl.update(contractHash=settings_hash(contract), output={'width': video['width'], 'height': video['height'], 'fps': video['frameRate']})
        protected = contract['workflow']['editing'].get('protectedIntervals', [])
        if protected:
            raise ValueError('Protected intervals require a measured manual EDL; automatic take selection cannot guarantee them')
    log_lines: list[str] = []
    alerts = refine_edl(edl, envs, log=log_lines.append)

    speech_value = sum(r["end"] - r["start"] for r in ranges)
    raw_value = sum(float(t.get("duration") or 0) for s, t in trs.items() if s in order)
    md = [f'# Automatic cut · {len(ranges)} ranges, {speech_value:.1f} s of {raw_value:.1f} s raw', "",
          f'## Removed ({len(dropped)} blocks); review whether useful speech was misidentified as repetition', ""]
    for stem, bl, reason in dropped:
        md.append(f'''- {stem} [{bl['a']:.2f}-{bl['b']:.2f}] "{bl['text'][:80]}" · {reason}''')
    if alerts:
        md += ["", '## NO PAUSE: listen to these boundaries', ""] + [f'- {a}' for a in alerts]
    md += ["", "## Ranges", ""] + [f"- {r['id']} {r['source']} [{r['start']:.2f}-{r['end']:.2f}] {r['quote']}"
                                    for r in ranges]
    report = "\n".join(md) + "\n"
    print(report)
    if not args.dry_run:
        edl_path = edit / "edl.json"
        if edl_path.exists():
            shutil.copy(edl_path, edit / "edl.json.bak")
        save_json(edl_path, edl)
        # voice level per range now, so the first render does not come back with LOW-LEVEL flags
        vl = subprocess.run([sys.executable, str(Path(__file__).with_name("voice_levels.py")), "--edl", str(edl_path),
                             "--drop-db", str(GAIN_DB)], capture_output=True, text=True, encoding='utf-8', errors='replace')
        levels = load_json(edit / "verify" / "voice_levels.json") if vl.returncode == 0 else None
        gains_value = {r["id"]: float(r["gain_db"]) for r in (levels or {}).get("ranges", [])
                  if (r.get("low") or r.get("high")) and r.get("gain_db") is not None}
        if gains_value:
            for r in edl["ranges"]:
                if r["id"] in gains_value:
                    r["gain_db"] = gains_value[r["id"]]
            save_json(edl_path, edl)
            print("adjusted volume: " + ", ".join(f'{k} {v:+.1f} dB' for k, v in gains_value.items()))
        (edit / "automatic-cut.md").write_text(report, encoding="utf-8")
        print(f"saved: {edl_path} and {edit / 'automatic-cut.md'}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
