'Refine selected EDL intervals against the actual waveform.\n\nStart from rough, complete sentence intervals chosen for take quality and continuity; the last take is not automatically best. Measure each source with cached 10 ms mono RMS windows. Trim toward sustained speech with configured padding, preserve weak consonants, and split only pauses at least PAUSE_MIN_S long. Shorter pauses remain because cutting micro-pauses can clip speech.\n\nExtend final word tails only within TAIL_MAX_S while signal remains above the decay threshold. Stop before a new rise indicating a click, breath, or next word. Where connected speech has no pause, use the valley after the final consonant and flag shallow valleys for review. Drop very short isolated click/breath fragments using the duration and peak thresholds.\n\nIntervals marked dry=false remain whole with their original boundaries. Never extend into the next interval of the same source. Derive each source threshold from noise floor and speech percentiles; an explicit --threshold overrides it. A rough boundary already on speech is reported and should first be repaired with precise_edges.py or moved to a real pause.\n\nContiguous camera-switch intervals use one continuous voice measurement, then split at the picture switch. A switch within speech keeps adjoining audio intervals touching; very short picture flashes merge into a neighbor. Report the switch without treating it as an accidental speech cut.\n\nUsage: uv run python helpers/waveform_cut.py EDIT/edl_manual.json -o EDIT/edl.json --threshold -38\nThen run render_cut.py and listen to the resulting boundaries. Cleanup defaults remain subordinate to explicit scope and intentional pauses.'

from __future__ import annotations

import argparse
import copy
import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ffprobe_json, load_json, require_binary, save_json  # noqa: E402
from icloud import CloudOnly, ensure_local  # noqa: E402

HOP_S = 0.01
PAUSE_MIN_S = 0.22
PAD_S = 0.06
TAIL_MAX_S = 0.20
FLOOR_TO_TH_DB = 14.0
SPEECH_TO_TH_DB = 20.0
SPEECH_TO_TH_MAX_DB = 28.0
TAIL_FLOOR_DB = 6.0
TAIL_UNDER_TH_DB = 12.0
TAIL_RISE_DB = 3.0
MIN_PIECE_S = 0.08
WEAK_PIECE_S = 0.35
PEAK_OVER_TH_DB = 13.0
ONSET_S = 0.04
JOIN_S = 0.02
MIN_SWITCH_S = 0.30
VALLEY_DB = 6.0      # the valley between two glued words is at least this far under both of them


def envelope(path: Path, cache: Path | None) -> np.ndarray:
    """dB of the mono mix in 10 ms windows, index 0 = time 0 of the file."""
    if cache:  # The name carries the source identity, so a remapped source key never reuses another take.
        st = path.stat()
        key = hashlib.sha1(f'{path.resolve()}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()[:12]
        cache = cache.with_name(f'{cache.stem}_{key}{cache.suffix}')
    if cache and cache.is_file():
        return np.load(cache)
    ffmpeg = require_binary("ffmpeg")
    raw = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000",
                          "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    w = int(48000 * HOP_S)
    n = len(x) // w
    if n == 0:
        raise SystemExit(f'ERROR: {path.name} has no measurable audio')
    db = 10 * np.log10((x[: n * w].reshape(n, w) ** 2).mean(axis=1) + 1e-12)
    offset = audio_offset(path)
    if offset > 0:   # audio starts after the video: the first windows are silence
        db = np.concatenate([np.full(int(round(offset / HOP_S)), -120.0), db])
    db = db.astype(np.float32)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, db)
    return db


def audio_offset(path: Path) -> float:
    info = ffprobe_json(path)
    starts = {s.get("codec_type"): float(s.get("start_time") or 0) for s in info.get("streams", [])
              if s.get("codec_type") in ("audio", "video")}
    return max(0.0, starts.get("audio", 0.0) - starts.get("video", 0.0))


def threshold(db: np.ndarray) -> float:
    floor = float(np.percentile(db, 10))
    loud = float(np.percentile(db, 90))
    # a phone with noise suppression leaves the gaps near digital silence (-72 dB) and the breaths at -45:
    # the floor alone would count a breath as speech, so the threshold never goes lower than loud - 28
    return round(min(max(floor + FLOOR_TO_TH_DB, loud - SPEECH_TO_TH_MAX_DB), loud - SPEECH_TO_TH_DB), 1)


class Source:
    def __init__(self, db: np.ndarray, th: float):
        self.db, self.th = db, th
        self.floor = float(np.percentile(db, 10))
        # a weak consonant still sounds above this; below it is the room (or the decay of a noise gate)
        self.weak = max(self.floor + TAIL_FLOOR_DB, th - TAIL_UNDER_TH_DB)

    def at(self, t: float) -> float:
        # the epsilon: 11.45 + 4 hops is 11.489999..., which read the window before and walked one hop too far
        return float(self.db[min(len(self.db) - 1, max(0, int(t / HOP_S + 1e-6)))])

    def pauses(self, a: float, b: float) -> list[tuple[float, float]]:
        out, run = [], None
        i0, i1 = int(round(a / HOP_S)), min(len(self.db), int(round(b / HOP_S)))
        for i in range(max(0, i0), i1):
            quiet = self.db[i] < self.th
            if quiet and run is None:
                run = i
            if not quiet and run is not None:
                if (i - run) * HOP_S >= PAUSE_MIN_S:
                    out.append((run * HOP_S, i * HOP_S))
                run = None
        if run is not None and (i1 - run) * HOP_S >= PAUSE_MIN_S:
            out.append((run * HOP_S, i1 * HOP_S))
        return out

    def first_sustained(self, a: float, b: float) -> float | None:
        need = int(round(ONSET_S / HOP_S))
        above = self.db[max(0, int(round(a / HOP_S))): min(len(self.db), int(round(b / HOP_S)))] >= self.th
        run = 0
        for k, up in enumerate(above):
            run = run + 1 if up else 0
            if run >= need:
                return (int(round(a / HOP_S)) + k - need + 1) * HOP_S
        return None

    def last_sustained_end(self, a: float, b: float) -> float | None:
        """end of the last sound that lasts ONSET_S inside [a, b] (None: no sound)"""
        need = int(round(ONSET_S / HOP_S))
        i0, i1 = max(0, int(round(a / HOP_S))), min(len(self.db), int(round(b / HOP_S)))
        run = 0
        for i in range(i1 - 1, i0 - 1, -1):
            run = run + 1 if self.db[i] >= self.th else 0
            if run >= need:
                return (i + need) * HOP_S
        return None

    def extend_head(self, x0: float, limit: float) -> float:
        """weak first consonant (the S of "Sem"): walk back while it still sounds, never into a click"""
        x, ref = x0, self.at(x0)
        while x - HOP_S > max(limit, x0 - TAIL_MAX_S):
            v = self.at(x - HOP_S)
            if v <= self.weak or v > ref + TAIL_RISE_DB:
                break
            ref = min(ref, v)
            x -= HOP_S
        return round(x, 3)

    def extend_tail(self, x1: float, limit: float) -> float:
        return self.extend_tail_why(x1, limit)[0]

    def extend_tail_why(self, x1: float, limit: float) -> tuple[float, str]:
        """(where the walk stopped, why): "fraco" (the room), "subiu" (a click, a breath or the next word),
        "limite" (TAIL_MAX_S or the next range)"""
        x, ref = x1, self.at(x1)
        while x < min(limit, x1 + TAIL_MAX_S):
            v = self.at(x)
            if v <= self.weak:
                return round(x, 3), "fraco"
            if v > ref + TAIL_RISE_DB:
                return round(x, 3), "subiu"
            ref = min(ref, v)
            x += HOP_S
        return round(x, 3), "limite"

    def sustained_from(self, t: float, within: float = 0.03) -> bool:
        """a sound of ONSET_S or more (a word, not a click) starts within `within` s after t"""
        onset = self.first_sustained(t, t + within + ONSET_S + HOP_S)
        return onset is not None and onset <= t + within + 1e-6

    def valley(self, a: float, b: float) -> tuple[float, float] | None:
        """(end time, depth in dB) of the lowest 10 ms of [a, b): the end goes right after that window. The depth is
        how far it is under the louder of the two sides (0.1 s before and 0.1 s after), taking the quieter side."""
        i0 = max(1, int(a / HOP_S + 1e-6))
        i1 = min(len(self.db) - 1, int(b / HOP_S + 1e-6))
        if i1 <= i0:
            i0 = max(1, i1 - 1)
        if i1 <= i0:
            return None
        seg = self.db[i0:i1]
        k = i0 + int(len(seg) - 1 - np.argmin(seg[::-1]))     # the lowest; on a tie, the later one
        left = float(self.db[max(0, k - 10):k].max())
        right = float(self.db[k + 1:k + 11].max()) if k + 1 < len(self.db) else float(self.db[k])
        return round((k + 1) * HOP_S, 3), round(min(left, right) - float(self.db[k]), 1)

    def peak(self, a: float, b: float) -> float:
        seg = self.db[max(0, int(a / HOP_S)): int(b / HOP_S)]
        return float(seg.max()) if len(seg) else -120.0


def next_in_source(ranges: list[dict], i: int) -> float | None:
    """Earliest start of another range of the same source that begins at or after the end of range i."""
    r = ranges[i]
    end = float(r["end"])
    starts = [float(o["start"]) for k, o in enumerate(ranges)
              if k != i and o.get("source") == r.get("source") and float(o["start"]) >= end - 0.05]
    return min(starts) if starts else None


def split_range(r: dict, src: Source, limit: float | None = None, notes: list | None = None) -> list[dict]:
    """limit: the tail never goes past it (the start of the next range of the same source). notes: the end glued to
    the next word is reported there."""
    a, b = float(r["start"]), float(r["end"])
    onset = src.first_sustained(a, b)
    if onset is not None and onset - PAD_S > a:
        a = src.extend_head(round(onset - PAD_S, 3), a)
    cur, pieces = a, []
    for s0, s1 in src.pauses(a, b):
        if s0 <= a + 0.02:            # silence at the start: trim it
            cur = max(cur, s1 - PAD_S)
            continue
        if s1 >= b - 0.02:            # silence at the end: trim it
            b = min(b, s0 + PAD_S)
            break
        pieces.append((cur, s0 + PAD_S))
        cur = s1 - PAD_S
    last = src.last_sustained_end(cur, b)   # a trailing silence shorter than a pause is trimmed too
    if last is not None and b - last > PAD_S + 0.02:
        b = round(last + PAD_S, 3)
    pieces.append((cur, b))
    out = []
    for k, (x0, x1) in enumerate(pieces):
        last_piece = k + 1 == len(pieces)
        nxt = pieces[k + 1][0] if not last_piece else x1 + TAIL_MAX_S
        if limit is not None and last_piece:
            nxt = min(nxt, max(x1, limit))
        walked, why = src.extend_tail_why(x1, nxt)
        walked = min(walked, max(x1, round(nxt, 3)))                # the 10 ms walk may step past the limit
        glued = None
        if last_piece and why == "subiu" and src.sustained_from(walked):
            # the sentence ends glued to the next word: stop in the valley between them, after the final consonant
            # (the walked decay only goes down, so its lowest point is past the consonant and before the next word)
            v = src.valley(x1, walked)
            if v is not None and x0 + MIN_PIECE_S < v[0] <= walked + 1e-6:
                walked, glued = v[0], v[1]
        x1 = walked
        if x1 - x0 < MIN_PIECE_S:
            continue
        if x1 - x0 < WEAK_PIECE_S and src.peak(x0, x1) < src.th + PEAK_OVER_TH_DB:
            continue
        n = copy.deepcopy(r)
        n.update(start=round(x0, 3), end=round(x1, 3), reason=(r.get("reason") or "").strip() + " + waveform cutting")
        if glued is not None:
            n['ends_in_valley'] = True
            quem = f"{r.get('id')} ({(r.get('quote') or '')[:40]!r})"
            if notes is not None and glued >= VALLEY_DB:
                notes.append(f"{quem}: the ending touched the next word; stopped in their separating valley at {x1:.2f} s ({glued:.0f} dB below them), after the final consonant. verify_cut may still flag EDGE-IN-SPEECH because the next word starts immediately. Listen across the render's 30 ms fade")
            elif notes is not None:
                notes.append(f"{quem}: the ending touches the next word without a valley; only {glued:.0f} dB): retained at {x1:.2f} seconds at the quietest point may still include the next word's onset. Use another take or extend by one complete word")
        out.append(n)
    return out


def picture_of(r: dict) -> str:
    return r.get("picture_source") or r.get("source")


def switch_groups(ranges: list[dict]) -> list[list[int]]:
    """Indices of the ranges, grouped where a camera switch continues the same speech (same source, touching,
    picture changes, none of them "dry": false)."""
    groups: list[list[int]] = []
    for i, r in enumerate(ranges):
        if groups:
            prev = ranges[groups[-1][-1]]
            if (prev.get("source") == r.get("source") and prev.get("dry", True) and r.get("dry", True)
                    and abs(float(r["start"]) - float(prev["end"])) <= JOIN_S and picture_of(prev) != picture_of(r)):
                groups[-1].append(i)
                continue
        groups.append([i])
    return groups


def split_at_switches(pieces: list[dict], group: list[dict]) -> list[dict]:
    """Pieces of a merged range, back to the range each part belongs to, split at the switch times."""
    seams = [float(r["start"]) for r in group[1:]]
    out = []
    for piece in pieces:
        x0, x1 = float(piece["start"]), float(piece["end"])
        owner = sum(1 for t in seams if t <= x0 + 1e-9)
        parts = []
        a = x0
        for k, t in enumerate(seams):
            if x0 < t < x1:
                parts.append([a, t, owner])
                a, owner = t, k + 1
        parts.append([a, x1, owner])
        while len(parts) > 1:
            short = min(range(len(parts)), key=lambda j: parts[j][1] - parts[j][0])
            if parts[short][1] - parts[short][0] >= MIN_SWITCH_S:
                break
            if short == 0:
                parts[1][0] = parts[0][0]
            elif short == len(parts) - 1:
                parts[-2][1] = parts[-1][1]
            else:
                left, right = parts[short - 1], parts[short + 1]
                if left[1] - left[0] >= right[1] - right[0]:
                    left[1] = parts[short][1]
                else:
                    right[0] = parts[short][0]
            del parts[short]
        for x, y, k in parts:
            n = copy.deepcopy(group[k])
            n.update(start=round(x, 3), end=round(y, 3),
                     reason=(group[k].get("reason") or "").strip() + " + waveform cutting")
            out.append(n)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refine waveform cuts and split removable internal pauses and breaths.")
    ap.add_argument('edl_manual', type=Path, help='EDL with one interval per complete selected phrase')
    ap.add_argument("-o", "--output", type=Path, help='trimmed EDL (default: edl.json in the same folder)', dest='output_value')
    ap.add_argument("--threshold", type=float, help='global pause threshold in dB; measured by default', dest='threshold_value')
    args = ap.parse_args(argv)

    edl = load_json(args.edl_manual)
    if not edl or not edl.get("ranges"):
        print(f'ERROR: {args.edl_manual} has no intervals', file=sys.stderr)
        return 2
    output_value = args.output_value or args.edl_manual.with_name("edl.json")
    if output_value.resolve() == args.edl_manual.resolve():
        print('ERROR: output must not overwrite edl_manual.json', file=sys.stderr)
        return 2
    edit = args.edl_manual.resolve().parent
    try:
        ensure_local([Path(p) for p in edl.get("sources", {}).values()])
    except CloudOnly as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    sources: dict[str, Source] = {}
    for name, p in edl.get("sources", {}).items():
        path = Path(p)
        if not path.is_file():
            print(f'ERROR: source {name} does not exist: {path}', file=sys.stderr)
            return 2
        db = envelope(path, edit / "regions" / f'waveform_{name}.npy')
        th = args.threshold_value if args.threshold_value is not None else threshold(db)
        sources[name] = Source(db, th)
        print(f'{name}: floor {sources[name].floor:.0f} dB, loud speech {np.percentile(db, 90):.0f} dB, pause below {th:.0f} dB')

    new, warnings_value, integers_value, switches, valleys = [], [], [], [], []
    for r in edl["ranges"]:
        if r.get("source") not in sources:
            print(f"ERROR: interval {r.get('id')} uses source {r.get('source')!r}, absent from sources",
                  file=sys.stderr)
            return 2
        if r.get("picture_source") and r.get("picture_source") not in sources:
            print(f"ERROR: interval {r.get('id')} takes its picture from {r.get('picture_source')!r}, absent from sources",
                  file=sys.stderr)
            return 2
    for group in switch_groups(edl["ranges"]):
        rs = [edl["ranges"][i] for i in group]
        src = sources[rs[0]["source"]]
        for k, r in enumerate(rs):
            sides = (('beginning', r["start"], k > 0), ("end", r["end"], k < len(rs) - 1))
            for side_value, t, switch in sides:
                if switch:
                    if side_value == 'beginning' and src.at(t) >= src.th:
                        switches.append(f"{rs[k - 1].get('id')} -> {r.get('id')} at {float(t):.2f} s: the picture switches from {picture_of(rs[k - 1])} to {picture_of(r)} mid-speech; the voice continues")
                    continue
                if src.at(t) >= src.th + 6:
                    warnings_value.append((r.get("id"), side_value, f"{r.get('id')} ({r.get('quote', '')[:40]!r}): the {side_value} at {t:.2f} seconds falls within speech and may clip a word"))
        r = rs[0]
        if not r.get("dry", True):
            new.append(copy.deepcopy(r))
            integers_value.append(f"""{r.get('id')} ("dry": false): {float(r['start']):.2f} to {float(r['end']):.2f} seconds; boundaries retained as specified""")
            continue
        limit = next_in_source(edl["ranges"], group[-1])
        if len(rs) == 1:
            new.extend(split_range(r, src, limit, valleys))
            continue
        merged = dict(copy.deepcopy(r), end=rs[-1]["end"])
        new.extend(split_at_switches(split_range(merged, src, limit, valleys), rs))

    in_valley = {n.get("id") for n in new if n.get('ends_in_valley')}    # the valley note says it better
    warnings_value = [txt for rid, side_value, txt in warnings_value if not (side_value == "end" and rid in in_valley)]
    for i, n in enumerate(new):
        n["id"] = f'r{i + 1:03d}'
    edl = dict(edl, ranges=new)
    save_json(output_value, edl)
    total = sum(n["end"] - n["start"] for n in new)
    print(f'{len(new)} ranges, {total:.2f} s → {output_value}')
    for t in integers_value:
        print("  inteiro: " + t)
    for t in switches:
        print('  camera switch: ' + t)
    for a in warnings_value + valleys:
        print('  warning: ' + a)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
