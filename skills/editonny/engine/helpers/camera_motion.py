"""Plan sentence-level camera movement in project.json.

Read the cut transcript, optional waveform word timings, timeline, and audio levels. Alternate
fullscreen base zoom between 1.0 and 1.12 and emphasize suitable words with short face push-ins.
Preserve manually configured blocks and unrelated project data. Keep the camera still beneath
scenes marked overPerson, resolving repeated speech anchors with startAfter and endAfter.
Generated settings carry auto="camera_motion"; reruns replace only those settings and --undo
removes them. Rebuild the project to inspect the result before rendering.

Usage: python helpers/camera_motion.py PROJECT [--undo] [--min-gap 2.5] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

MARK = "camera_motion"
ZOOM_A, ZOOM_B = 1.0, 1.12
PUNCH_A, PUNCH_B = 1.25, 1.3
MIN_GAP_S = 2.5
LONG_S = 4.5          # a sentence longer than this is split in two
SHORT_S = 1.2          # a sentence shorter than this joins the next one
PUNCH_HOLD_S = 0.6     # how long the punch-in stays after the word ends
SNAP_S = 0.25          # a sentence start this close to a cut moves to the cut

NEGATIONS = {"nao", "nunca", "nem", "nada", "ninguem", "jamais", "nenhum", "nenhuma", "sem", "zero"}
NUMBERS = {"um", "uma", "dois", "duas", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez", "onze",
           "doze", "treze", "catorze", "quatorze", "quinze", "vinte", "trinta", "quarenta", "cinquenta", "sessenta",
           "setenta", "oitenta", "noventa", "cem", "cento", "duzentos", "trezentos", "quinhentos", "mil", "milhao",
           "milhoes", "bilhao", "bilhoes", "dobro", "triplo", "metade", "primeiro", "primeira", "segundo", "segunda",
           "terceiro", "terceira"}
ARTICLES_NUM = {"um", "uma"}       # "um" and "uma" are almost always the article: they only count with energy
WEAK = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "que", "em", "no", "na", "nos", "nas", "um",
          "uma", "por", "pra", "para", "com", "se", "eu", "ele", "ela", "voce", "me", "te", "foi", "era", "isso",
          "esse", "essa", "este", "esta", "meu", "minha", "seu", "sua", "la", "ja", "mas", "ou", "como", "so", "ai",
          "ne", "tem", "ter", "vai", "vou", "ao", "aos", "quando", "porque", "quem", "tudo", "cada", "mesma", "mesmo"}


# These lexicons describe recorded speech, including Portuguese and English.
NEGATIONS |= {"no", "not", "never", "none", "nothing", "nobody", "without"}
NUMBERS |= {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "hundred", "thousand", "million", "first", "second", "third"}
WEAK |= {"the", "a", "an", "of", "to", "in", "on", "and", "or", "with", "for", "is", "are", "was", "it", "this", "that"}

class CameraError(Exception):
    pass


def normalize_word(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    return re.sub(r"[^a-z0-9%$]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def read_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def words_of_cut(edit: Path) -> list[dict]:
    """Words of the cut with the start measured on the wave when waveform_words.json matches the transcript."""
    p = edit / "transcripts" / "cut.json"
    if not p.is_file():
        raise CameraError(f'missing {p}: finish and transcribe the cut first')
    ws = [dict(text=w["text"].strip(), start=float(w["start"]), end=float(w["end"]))
          for w in read_json(p)["words"] if w["text"].strip()]
    po = edit / "waveform_words.json"
    if po.is_file():
        try:
            listing = read_json(po).get("words") or []
        except (ValueError, AttributeError):
            listing = []
        by_time: dict[float, list[dict]] = {}
        for r in listing:
            try:
                by_time.setdefault(round(float(r["whisper"]), 2), []).append(r)
            except (KeyError, TypeError, ValueError):
                continue
        for w in ws:
            for r in by_time.get(round(w["start"], 2), []):
                if normalize_word(r.get("text", "")) == normalize_word(w["text"]) and abs(float(r["start"]) - w["start"]) <= 0.45:
                    w["start"] = round(float(r["start"]), 3)
                    w["end"] = round(max(w["end"], w["start"] + 0.05), 3)
                    break
    return ws


def phrases(ws: list[dict], cuts_value: list[float]) -> list[list[int]]:
    """Indexes of the words of each sentence: split on . ! ? and a long sentence split again at a comma or a cut."""
    out, current_value = [], []
    for i, w in enumerate(ws):
        current_value.append(i)
        if re.search(r"[.!?…]$", w["text"]):
            out.append(current_value)
            current_value = []
    if current_value:
        out.append(current_value)

    def dur(f):
        return ws[f[-1]]["end"] - ws[f[0]]["start"]

    split_phrases = []
    for f in out:
        queue = [f]
        while queue:
            g = queue.pop(0)
            if dur(g) <= LONG_S or len(g) < 4:
                split_phrases.append(g)
                continue
            middle = (ws[g[0]]["start"] + ws[g[-1]]["end"]) / 2
            cand = [k for k in range(1, len(g) - 1) if ws[g[k - 1]]["text"].endswith((",", ";", ":"))]
            cand += [k for k in range(1, len(g) - 1)
                     if any(abs(c - ws[g[k]]["start"]) <= SNAP_S for c in cuts_value)]
            cand = [k for k in cand if ws[g[k]]["start"] - ws[g[0]]["start"] >= SHORT_S
                    and ws[g[-1]]["end"] - ws[g[k]]["start"] >= SHORT_S]
            if not cand:
                split_phrases.append(g)
                continue
            k = min(cand, key=lambda k: abs(ws[g[k]]["start"] - middle))
            queue = [g[:k], g[k:]] + queue
    together: list[list[int]] = []
    for f in split_phrases:                       # a very short sentence rides with the one before it
        if together and (dur(f) < SHORT_S or dur(together[-1]) < SHORT_S) and dur(together[-1]) + dur(f) <= LONG_S:
            together[-1] = together[-1] + f
        else:
            together.append(f)
    return together


def word_energy(db, w: dict) -> float:
    """Loudness of a word: the 90th percentile of the wave (10 ms windows) between its start and its end."""
    if db is None:
        return 0.0
    import numpy as np  # noqa: PLC0415
    a, b = int(w["start"] * 100), max(int(w["end"] * 100), int(w["start"] * 100) + 1)
    seg = db[a:b]
    return float(np.percentile(seg, 90)) if len(seg) else -99.0


def emphasis(ws: list[dict], f: list[int], db) -> tuple[int, str] | None:
    """(index, reason) of the emphasis word of a sentence: number, negation, or the loudest content word."""
    en = {i: word_energy(db, ws[i]) for i in f}
    nums = [i for i in f if re.search(r"\d", ws[i]["text"]) or "%" in ws[i]["text"] or "$" in ws[i]["text"]
            or (normalize_word(ws[i]["text"]) in NUMBERS and normalize_word(ws[i]["text"]) not in ARTICLES_NUM)]
    if nums:
        return max(nums, key=lambda i: en[i]), 'number'
    negs = [i for i in f if normalize_word(ws[i]["text"]) in NEGATIONS]
    if negs:
        return max(negs, key=lambda i: en[i]), 'negation'
    content_words = [i for i in f if normalize_word(ws[i]["text"]) not in WEAK and len(normalize_word(ws[i]["text"])) >= 3]
    if not content_words:
        return None
    return max(content_words, key=lambda i: en[i]), "strongest word"


def anchor_time(v, ws: list[dict], after_value: float = 0.0) -> float | None:
    """Seconds of a "start"/"end" of a hand-written block: a number, or the first time the word(s) are said."""
    if isinstance(v, (int, float)):
        return float(v)
    target_value = [normalize_word(x) for x in str(v).split() if normalize_word(x)]
    if not target_value:
        return None
    for i in range(len(ws) - len(target_value) + 1):
        if ws[i]["start"] >= after_value - 1e-3 and [normalize_word(w["text"]) for w in ws[i:i + len(target_value)]] == target_value:
            return ws[i]["start"]
    return None


def intervals_over_a_person(cfg: dict, ws: list[dict], vdur: float) -> list[tuple[float, float]]:
    """Merge overPerson scene intervals; startAfter/endAfter select repeated word anchors."""
    out = []
    for i, c in enumerate(cfg.get("scenes") or []):
        if not isinstance(c, dict) or c.get("overPerson") is not True:
            continue
        name_value = c.get("file") or f'custom scene {i + 1}'
        start_value = anchor_time(c.get("start", 0.0), ws, float(c.get("startAfter", c.get("after", 0.0)) or 0.0))
        if start_value is None:
            raise CameraError(f'''{name_value}: word not found: "{c.get('start')}" for start in the recording; supply seconds''')
        end_value = vdur
        if c.get("end") is not None:
            end_value = anchor_time(c["end"], ws, max(start_value, float(c.get("endAfter", 0.0) or 0.0)))
            if end_value is None:
                raise CameraError(f'''{name_value}: word not found: "{c['end']}" for end in the recording; supply seconds''')
        start_value, end_value = max(0.0, start_value), min(vdur, end_value)
        if end_value > start_value + 0.05:
            out.append((start_value, end_value))
    out.sort()
    together: list[tuple[float, float]] = []
    for start_value, end_value in out:
        if together and start_value <= together[-1][1] + 0.05:
            together[-1] = (together[-1][0], max(together[-1][1], end_value))
        else:
            together.append((start_value, end_value))
    return together


def plan_camera(cfg: dict, ws: list[dict], cuts_value: list[float], vdur: float, db=None, min_gap: float = MIN_GAP_S) -> dict:
    """The new "blocks" of the project and a short report. Pure: reads nothing from disk."""
    manual_value, plain_blocks = [], []
    for b in cfg.get("blocks") or []:
        if not isinstance(b, dict) or b.get("auto") == MARK:
            continue
        b = dict(b)
        if isinstance(b.get("zooms"), list):          # punch-ins from a previous run inside a hand-written block
            b["zooms"] = [z for z in b["zooms"] if not (isinstance(z, dict) and z.get("auto") == MARK)]
            if not b["zooms"]:
                del b["zooms"]
                if b.pop("_zoom_auto", None):
                    b.pop("zoom", None)
        start_value = anchor_time(b.get("start", 0.0), ws)
        end_value = anchor_time(b["end"], ws, start_value or 0.0) if "end" in b else vdur
        if start_value is None or end_value is None:
            raise CameraError(f"block {b.get('scene')}: start/end words not found in speech; provide seconds")
        plain = (b.get("scene") == "fullscreen" and not b.get("zooms") and not b.get("followFace")
                 and b.get("camera_motion", True) is not False and not b.get("blur"))
        (plain_blocks if plain else manual_value).append((start_value, min(end_value, vdur), b))
    manual_value.sort(key=lambda x: x[0])

    # the stretches that are free for the live camera: everything outside the hand-written blocks that stay
    free_value, t = [], 0.0
    for start_value, end_value, _ in manual_value:
        if start_value > t + 0.05:
            free_value.append((t, start_value))
        t = max(t, end_value)
    if t < vdur - 0.05:
        free_value.append((t, vdur))

    # Text above the head must remain clear; a punch-in would move the head beneath it.
    occupied: list[tuple[float, float]] = []
    for an in cfg.get("animations") or []:
        if not isinstance(an, dict) or an.get("type") not in ("lettering", "label", "stamp"):
            continue
        start_time = anchor_time(an["at"], ws, float(an.get("after") or 0.0)) if an.get("at") is not None else None
        if start_time is None:
            continue
        end_value = anchor_time(an["exit"], ws, start_time) if an.get("exit") is not None else None
        occupied.append((start_time - 0.1, end_value if end_value is not None else start_time + 2.5))

    fs = phrases(ws, cuts_value)
    limits_value = []                                   # where each sentence starts on screen
    for f in fs:
        s = ws[f[0]]["start"]
        c = min(cuts_value, key=lambda c: abs(c - s), default=None)
        limits_value.append(round(c if c is not None and abs(c - s) <= SNAP_S else max(0.0, s - 0.04), 3))
    if limits_value:
        limits_value[0] = 0.0

    over_value = intervals_over_a_person(cfg, ws, vdur)  # the camera stays at 1.0 and does not come closer here
    warnings_value = []
    for start_value, end_value, b in manual_value:
        for sd, sa in over_value:
            if b.get("scene") == "fullscreen" and start_value < sa - 0.05 and end_value > sd + 0.05:
                warnings_value.append(f'explicit fullscreen block from {start_value:.1f} to {end_value:.1f} s overlaps the over-person scene from {sd:.1f} to {sa:.1f} seconds: remove zoom here to keep the head clear of the card')

    def pieces_value(la: float, lb: float) -> list[tuple[float, float, bool]]:
        """Split free intervals at over-person boundaries: (start, end, under_card)."""
        out, t = [], la
        for sd, sa in over_value:
            lo, hi = max(sd, la), min(sa, lb)
            if hi - lo <= 0.05:
                continue
            if lo - t > 0.05:
                out.append((t, lo, False))
                t = lo
            out.append((t, hi, True))
            t = hi
        if lb - t > 0.05:
            out.append((t, lb, False))
        elif out:
            out[-1] = (out[-1][0], lb, out[-1][2])
        return out

    autos, punches, last_base, last_punch = [], [], ZOOM_B, -99.0
    for la, lb in free_value:
        for pa, pb, under in pieces_value(la, lb):
            points_value = [pa] + ([] if under else [x for x in limits_value if pa + SHORT_S * 0.8 <= x <= pb - SHORT_S * 0.8]) + [pb]
            for a, b in zip(points_value, points_value[1:]):
                base = ZOOM_A if under or last_base == ZOOM_B else ZOOM_B
                last_base = base
                block_value = dict(scene="fullscreen", start=round(a, 3), end=round(b, 3), zoom=base, auto=MARK)
                inside_value = [f for f in fs if a - 0.05 <= ws[f[0]]["start"] < b - 0.05]
                cands = [] if under else [e for e in (emphasis(ws, f, db) for f in inside_value) if e]
                order_value = {'number': 0, 'negation': 1, "strongest word": 2}
                cands.sort(key=lambda e: (order_value[e[1]], -word_energy(db, ws[e[0]])))
                for i, reason_value in cands:
                    t0 = ws[i]["start"]
                    if t0 - last_punch < min_gap or t0 < a + 0.45 or b - t0 < PUNCH_HOLD_S + 0.25:
                        continue
                    t1 = round(min(ws[i]["end"] + PUNCH_HOLD_S, b - 0.05), 3)
                    if any(x < t1 and t0 - 0.1 < y for x, y in occupied):
                        continue
                    block_value["zooms"] = [dict(at=round(t0, 3), end=t1, scale=PUNCH_A if base == ZOOM_A else PUNCH_B,
                                           sound=False, auto=MARK)]
                    punches.append(dict(at=round(t0, 2), word=ws[i]["text"], reason=reason_value))
                    last_punch = t0
                    break
                autos.append((a, block_value))
    if autos and autos[-1][1]["end"] >= vdur - 0.05:
        del autos[-1][1]["end"]                    # the last block goes to the end (and holds on the still end)

    blocks_value = [b for _, _, b in manual_value]
    all_value = sorted([(start_value, b) for start_value, _, b in manual_value] + autos, key=lambda x: x[0])
    # A card entering or leaving an over-person scene also changes the picture.
    cards = {round(x, 2) for d, a in over_value for x in (d, a) if 0.05 < x < vdur - 0.05}
    switches = sorted({round(start_value, 2) for start_value, _ in all_value} | {p["at"] for p in punches} | cards)
    # Keep the camera still during over-person cards; main() controls their duration.
    inside_value = lambda x, y: any(x >= sd - 0.01 and y <= sa + 0.01 for sd, sa in over_value)  # noqa: E731
    largest = max((y - x for x, y in zip([0.0] + switches, switches + [vdur]) if not inside_value(x, y)), default=0.0)
    return dict(blocks=[b for _, b in all_value], phrases=len(fs), automatic_blocks=len(autos), punches=punches,
                retained=len(blocks_value), replaced_plain_blocks=len(plain_blocks), largest_interval=round(largest, 2),
                overPerson=[[round(d, 2), round(a, 2)] for d, a in over_value], warnings=warnings_value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Write phrase-based zoom changes and face push-ins on emphasized words into project.json.')
    ap.add_argument('project', type=Path, help='generated project folder')
    ap.add_argument("--undo", action="store_true", help='remove camera-motion-generated settings for a calm presentation', dest='undo')
    ap.add_argument("--min-gap", type=float, default=MIN_GAP_S, help='seconds between push-ins (default 2.5)', dest='min_gap')
    ap.add_argument("--json", action="store_true", dest='json')
    a = ap.parse_args(argv)
    try:
        pj = a.project / "project.json"
        if not pj.is_file():
            raise CameraError(f'could not find {pj}: create the project with build.py new')
        cfg = read_json(pj)
        if a.undo:
            before_value = cfg.get("blocks") or []
            cfg["blocks"] = [b for b in before_value if not (isinstance(b, dict) and b.get("auto") == MARK)]
            for b in cfg["blocks"]:
                if isinstance(b, dict) and isinstance(b.get("zooms"), list):
                    b["zooms"] = [z for z in b["zooms"] if not (isinstance(z, dict) and z.get("auto") == MARK)]
            pj.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"camera motion: removed {len(before_value) - len(cfg['blocks'])} blocks. Rebuild the project.")
            return 0
        edit = (a.project / cfg.get("edit", "../edit")).resolve()
        tl = edit / "cut_timeline.json"
        if not tl.is_file():
            raise CameraError(f'missing {tl}: finish the cut first')
        tl = read_json(tl)
        vdur = float(tl["duration"])
        cuts_value = sorted(float(s["out_start"]) for s in tl["segments"] if s["out_start"] > 0.01)
        ws = words_of_cut(edit)
        db = None
        video = edit / cfg.get("video", "cut.mp4")
        if video.is_file():
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                import waveform_cut as waveform  # noqa: E402, PLC0415
                db = waveform.envelope(video, None)
            except Exception as e:  # noqa: BLE001  (without the wave the emphasis is number, negation, first strong word)
                print(f'notice: waveform measurement unavailable ({e}); emphasis is inferred from numbers and negation', file=sys.stderr)
        r = plan_camera(cfg, ws, cuts_value, vdur, db, a.min_gap)
        cfg["blocks"] = r["blocks"]
        pj.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except CameraError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({k: v for k, v in r.items() if k != "blocks"}, ensure_ascii=False))
        return 0
    print(f"camera motion: {r['automatic_blocks']} fullscreen intervals alternating {ZOOM_A:g} and {ZOOM_B:g}, {len(r['punches'])} face push-ins; {r['retained']} explicit blocks retained.")
    for p in r["punches"]:
        print(f'''  {p['at']:6.2f} s  "{p['word']}"  ({p['reason']})''')
    for d, a in r["overPerson"]:
        print(f'  {d:6.2f} to {a:.2f} seconds over the speaker: zoom remains 1.0 without push-ins so the head stays clear of the card'
              + (f'; this leaves {a - d:.1f} seconds with a static camera; consider meaningful card-state changes every two to four seconds' if a - d > 4.0 else ""))
    for notice in r["warnings"]:
        print(f'  warning: {notice}')
    print(f"  longest interval without camera changes: {r['largest_interval']:.1f} s"
          + ('  longer than four seconds: consider a meaningful visual change if appropriate' if r["largest_interval"] > 4.0 else ""))
    print('  rebuild the sample or complete project to inspect it in the studio')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
