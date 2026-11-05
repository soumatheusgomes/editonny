'Place EDL boundaries in measured pauses using 10 ms RMS audio.\n\nWord timestamps can drift by 0.1–0.3 seconds. If a boundary cuts speech, choose the nearest sufficiently long pause within the allowed outward/inward reach, completing or excluding the unfinished word. If no pause is reachable, choose the quietest nearby point and flag it for listening.\n\nFor silent boundaries, approach the first/last speech with 0.08 seconds before and 0.12 seconds after, then extend through weak consonants while the waveform supports them. Use separate onset/release thresholds and hysteresis. Never cross neighboring intervals or exceed the configured outward reach.\n\nNearly contiguous intervals of the same source represent continuous speech split for editing; close their gap without searching away words at that junction. All warnings require review of the actual recording.\n\nUsage: uv run python helpers/precise_edges.py EDIT/edl.json --apply'

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

import numpy as np

from common import load_json, save_json
from speech_regions import load_mono

SR = 16000
HOP = 0.010
ON_DB, OFF_DB, WEAK_DB = 14.0, 10.0, 6.0   # above the source floor
HYST = 8                                   # 80 ms
PAD_IN, PAD_OUT = 0.08, 0.12               # silence kept before / after the speech
WALK = 12                                  # up to 0.12 s outward for weak consonants
PAUSE_MIN_S = 0.07                         # shorter dips are stop consonants or run-on speech
REACH_OUT_S = 0.25                         # an edge on speech may grow this much to finish a word
REACH_IN_S = 0.40                          # ... or shrink this much to drop the unfinished word
EDGE_PROBE_S = 0.03                        # how close to the edge sound counts as "on speech"
MIN_DYNAMIC_DB = 20.0                      # p90 - floor below this: no silence to measure against
CLICK_S = 0.03                             # a blip this short inside a pause does not split it
CONTIGUOUS_S = 0.15                        # next range of the same source starting this close: one take


RUMBLE_AF = "highpass=f=300,highpass=f=300"   # car, road, AC: rumble lives below the voice


def envelope(path: str, af: str | None = None) -> np.ndarray:
    mono, _ = load_mono(path, sr=SR, af=af)
    n = int(HOP * SR)
    frames = mono[: (len(mono) // n) * n].reshape(-1, n).astype(np.float64)
    return 10.0 * np.log10(np.mean(frames * frames, axis=1) + 1e-12)


def idx(t: float) -> int:
    return int(round(t / HOP))


def pauses(db: np.ndarray, floor: float, i0: int, i1: int) -> list[tuple[int, int]]:
    """Runs below floor+OFF_DB of at least PAUSE_MIN_S inside [i0, i1), as (first, last+1)."""
    i0, i1 = max(0, i0), min(len(db), i1)
    quiet = db[i0:i1] < floor + OFF_DB
    # a blip of CLICK_S or less inside a pause (lip click, breath) does not split it
    k, short = 0, int(round(CLICK_S / HOP))
    while k < quiet.size:
        if not quiet[k]:
            j = k
            while j < quiet.size and not quiet[j]:
                j += 1
            if j - k <= short and k > 0 and j < quiet.size:
                quiet[k:j] = True
            k = j
        else:
            k += 1
    out, k, need = [], 0, int(round(PAUSE_MIN_S / HOP))
    while k < quiet.size:
        if quiet[k]:
            j = k
            while j < quiet.size and quiet[j]:
                j += 1
            if j - k >= need:
                out.append((i0 + k, i0 + j))
            k = j
        else:
            k += 1
    return out


def on_speech(db: np.ndarray, floor: float, t: float) -> bool:
    a, b = max(0, idx(t - EDGE_PROBE_S)), min(len(db), idx(t + EDGE_PROBE_S) + 1)
    return b > a and float(db[a:b].max()) >= floor + OFF_DB


def fix_edge_on_speech(db, floor, t, side, lo, hi):
    """(new_time, how) for an edge that sits on speech."""
    out_dir = 1 if side == "end" else -1
    w0 = max(idx(lo), idx(t - (REACH_OUT_S if side == "start" else REACH_IN_S)))
    w1 = min(idx(hi), idx(t + (REACH_OUT_S if side == "end" else REACH_IN_S)))
    best = None
    for a, b in pauses(db, floor, w0, w1 + 1):
        # the edge keeps PAD_OUT / PAD_IN of the pause, or all of it when the pause is shorter:
        # stopping mid-pause left 0.04 s after the word and the 30 ms fade ate its end
        if side == "end":   # end goes where the pause begins
            new = min(a * HOP + PAD_OUT, b * HOP)
            dist = abs(a * HOP - t)
        else:               # start goes where the pause ends
            new = max(b * HOP - PAD_IN, a * HOP)
            dist = abs(b * HOP - t)
        grows = (new - t) * out_dir > 0
        if (dist <= REACH_OUT_S if grows else dist <= REACH_IN_S):
            # finishing the word wins a near tie over dropping it
            score = dist - (0.05 if grows else 0.0)
            if best is None or score < best[0]:
                best = (score, new, "finishes the word" if grows else "removes the incomplete word")
    if best:
        return min(max(best[1], lo), hi), best[2]
    a, b = max(idx(lo), idx(t - 0.08)), min(idx(hi), idx(t + 0.08)) + 1
    if b <= a:
        return t, "NO PAUSE"
    k = a + int(np.argmin(db[a:b]))
    return k * HOP, "NO PAUSE"


def tighten(db, floor, s, e, lo, hi, fix_start, fix_end):
    """Pull edges in silence toward the speech, walking out for weak consonants."""
    i0, i1 = idx(s), min(int(math.ceil(e / HOP)), len(db) - 1)
    if i1 <= i0:
        return s, e
    win = db[i0:i1 + 1]
    on = np.where(win >= floor + ON_DB)[0]
    off = np.where(win >= floor + OFF_DB)[0]
    if len(on) == 0 or len(off) == 0:
        return s, e
    ns, ne = s, e
    if fix_start:
        start_i = on[0]
        for i in on:
            if (win[i:i + HYST] >= floor + OFF_DB).mean() > 0.6:
                start_i = i
                break
        k, steps = i0 + start_i, 0
        while steps < WALK and k - 1 >= 0 and db[k - 1] >= floor + WEAK_DB:
            k, steps = k - 1, steps + 1
        ns = min((i0 + start_i) * HOP - PAD_IN, k * HOP - 0.05)
    if fix_end:
        k, steps = i0 + off[-1], 0
        while steps < WALK and k + 1 < len(db) and db[k + 1] >= floor + WEAK_DB:
            k, steps = k + 1, steps + 1
        ne = max((i0 + off[-1] + 1) * HOP + PAD_OUT, (k + 1) * HOP + 0.06)
    return max(ns, lo), min(ne, hi)


def source_levels(path: str, log=None) -> tuple[np.ndarray, float] | None:
    """(10 ms level, noise floor) of a source, or None when it has no silence to measure against.
    A recording with rumble (car, road, air conditioning) is measured again without the lows: the voice
    stands 25 to 30 dB over what is left, while the high consonants (the S) stay in."""
    for af in (None, RUMBLE_AF):
        db = envelope(path, af)
        floor, top = float(np.percentile(db, 10)), float(np.percentile(db, 90))
        if top - floor >= MIN_DYNAMIC_DB:
            if af and log:
                log(f'{Path(path).stem}: background rumble; measured voice after high-pass filtering in band {top - floor:.0f} dB)')
            return db, floor
    if log:
        log(f'{Path(path).stem}: no measurable silence; floor {floor:.0f} dB, speech {top:.0f} dB. Music or an existing edit may mask pauses. Source boundaries remain unchanged')
    return None


def refine_edl(edl: dict, envs: dict | None = None, log=print) -> list[str]:
    """Move every edge of edl["ranges"] in place. Returns the NO PAUSE alerts."""
    envs = {} if envs is None else envs
    for name, path in edl["sources"].items():
        if name in envs:
            continue
        envs[name] = source_levels(path, log)
        if envs[name]:
            db, floor = envs[name]
            log(f'{name}: floor {floor:.0f} dB, {len(db) * HOP:.1f} s')

    rs = edl["ranges"]
    removed_value, alerts, moved = 0.0, [], 0
    frozen_start, frozen_end = set(), set()
    for a, b in zip(rs, rs[1:]):
        if b.get("dry", True) and a["source"] == b["source"] and 0 <= float(b["start"]) - float(a["end"]) < CONTIGUOUS_S:
            frozen_end.add(a["id"])
            frozen_start.add(b["id"])
            b["start"] = a["end"]
    for n, r in enumerate(rs):
        env = envs.get(r["source"])
        if env is None or not r.get("dry", True):   # "dry": false keeps its boundaries as specified.
            continue
        db, floor = env
        s, e = float(r["start"]), float(r["end"])
        lo, hi = max(0.0, s - REACH_OUT_S), min(len(db) * HOP, e + REACH_OUT_S)
        prev = next((p for p in rs[:n][::-1] if p["source"] == r["source"]), None)
        nxt = next((p for p in rs[n + 1:] if p["source"] == r["source"]), None)
        if prev and prev["end"] <= s + 1e-6:   # only a neighbour that plays before this one
            lo = max(lo, float(prev["end"]) + 0.02)
        if nxt and nxt["start"] >= e - 1e-6:
            hi = min(hi, float(nxt["start"]) - 0.02)

        notes = []
        ns, ne = s, e
        keep_s, keep_e = r["id"] in frozen_start, r["id"] in frozen_end
        if keep_s:
            notes.append('start joins the preceding interval')
        if keep_e:
            notes.append('end joins the following interval')
        start_hot = not keep_s and on_speech(db, floor, s)
        end_hot = not keep_e and on_speech(db, floor, e)
        if start_hot:
            ns, how = fix_edge_on_speech(db, floor, s, "start", lo, hi)
            notes.append(f'start falls inside speech: {how}')
        if end_hot:
            ne, how = fix_edge_on_speech(db, floor, e, "end", lo, hi)
            notes.append(f'end inside speech: {how}')
        ts, te = tighten(db, floor, ns, ne, lo, hi, not (start_hot or keep_s), not (end_hot or keep_e))
        ns, ne = (s if keep_s else round(ts, 3)), (e if keep_e else round(te, 3))
        if ne - ns < 0.1:
            notes.append("became too short; retained")
            ns, ne = s, e
        if any("NO PAUSE" in x for x in notes):
            alerts.append(f"{r['id']} ({r['source']} {s:.2f}-{e:.2f}) {r.get('quote', '')[:50]}")
        d0, d1 = ns - s, ne - e
        mark = "" if abs(d0) < 0.005 and abs(d1) < 0.005 else "  <-"
        moved += bool(mark)
        removed_value += (e - s) - (ne - ns)
        log(f"{r['id']} {r['source']} {s:7.2f}->{ns:7.2f} ({d0:+.2f})  {e:7.2f}->{ne:7.2f} ({d1:+.2f}){mark}{('  · ' + '; '.join(notes) if notes else '')}")
        r["start"], r["end"] = ns, ne

    log(f'\n{moved} of {len(rs)} intervals adjusted · silence removed {removed_value:+.2f} s')
    if alerts:
        log('NO PAUSE: speech crosses this boundary; listen or move the cut to the phrase ending:')
        for a in alerts:
            log("  " + a)
    return alerts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument('edl', type=Path)
    ap.add_argument("--apply", action="store_true", help="write to edl.json (backup at edl.json.bak)", dest='apply')
    args = ap.parse_args()

    edl = load_json(args.edl)
    if not isinstance(edl, dict) or not edl.get("ranges"):
        print(f'EDL has no intervals: {args.edl}', file=sys.stderr)
        return 2
    refine_edl(edl)
    if args.apply:
        shutil.copy(args.edl, args.edl.with_name(args.edl.name + ".bak"))
        save_json(args.edl, edl)
        print(f'saved to {args.edl} (backup {args.edl.name}.bak)')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
