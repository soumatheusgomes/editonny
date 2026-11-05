'Run independent checks of a rendered cut concurrently: transcript fidelity, remaining silence, word boundaries/clicks/levels, and a junction contact sheet.\n\nUse transcribe.py, breaths.py, verify_cut.py without requesting approval, and contact_sheet.py --junctions. Print each report and compare every selected manual EDL passage with the speech actually present in cut.mp4, using short phrases and measured timestamps. Report missing and extra words rather than silently correcting the quoted intent. Save the same review in the cut-speech document.\n\nA silence finding is a review item, not proof of a tooling error. Split unintended pauses in the manual EDL or adjust measured waveform thresholds. Intervals deliberately marked dry=false remain separately identified and do not count as accidental silence.\n\nUsage: uv run --directory ENGINE python helpers/audit_cut.py EDIT --min-silence 0.7\nAn optional transcription prompt may contain names supplied by the user. Do not infer approval or delivery readiness from one check alone.'

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HELPERS = Path(__file__).resolve().parent
SKILL = HELPERS.parent
sys.path.insert(0, str(HELPERS))
from common import fmt_time, load_json  # noqa: E402
from pack_transcripts import cut_phrases, group_into_phrases, render_cut_markdown, split_long  # noqa: E402


def run(cmd: list[str]) -> tuple[int, str, float]:
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, cwd=HELPERS.parent, encoding='utf-8', errors='replace')
    return proc.returncode, (proc.stdout + proc.stderr).strip(), time.monotonic() - t0


def cmd(helper: str, *args: str) -> str:
    """A command the agent can paste from any folder (the skill's own Python, by --directory)."""
    return f"uv run --directory '{SKILL}' python helpers/{helper} " + " ".join(args)


# ------------------------------------------------------------------ cut speech, segment by segment

def _normal_words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", t)


def intervals_value(timeline: dict, edl: dict) -> list[dict]:
    """Consecutive segments with the same beat and quote are one interval of edl_manual.json (waveform_cut
    splits a range at its breaths and copies beat and quote to every piece)."""
    dry = {r.get("id"): not r.get("dry", True) for r in (edl or {}).get("ranges", [])}
    out: list[dict] = []
    for s in timeline.get("segments", []):
        key = (s.get("beat") or "", s.get("quote") or "")
        a0 = float(s.get("audio_out_start", s["out_start"]))
        if out and out[-1]["key"] == key and not dry.get(s.get("id")) and not out[-1]["dry"]:
            out[-1]["end"] = float(s["out_end"])
            out[-1]["ids"].append(s.get("id"))
            continue
        out.append({"key": key, "beat": key[0], "quote": key[1], "start": float(s["out_start"]), "audio": a0,
                    "end": float(s["out_end"]), "ids": [s.get("id")], "dry": bool(dry.get(s.get("id")))})
    return out


def words_per_interval(words: list[dict], groups_value: list[dict]) -> list[list[dict]]:
    """A word goes to the interval whose audio has started when the word starts (the J-cut lead starts the audio of
    an interval a few frames before its picture)."""
    starts = [g["audio"] for g in groups_value]
    out: list[list[dict]] = [[] for _ in groups_value]
    for w in words:
        k = 0
        for i, a in enumerate(starts):
            if float(w["start"]) >= a - 0.02:
                k = i
        out[k].append(w)
    return out


def difference(quote: str, heard_value: str) -> tuple[list[str], list[str]]:
    """(words of the quote missing in the cut, words of the cut not in the quote), accents and case ignored."""
    a, b = _normal_words(quote), _normal_words(heard_value)
    missing_value, remainder = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            missing_value += a[i1:i2]
        if op in ("insert", "replace"):
            remainder += b[j1:j2]
    return missing_value, remainder


def speech_of_cut(edit: Path, video: Path) -> list[str]:
    data = load_json(edit / "transcripts" / f'{video.stem}.json')
    if not isinstance(data, dict):
        return ['(no cut transcript; inspect the transcription error above)']
    words = sorted(data.get("words", []), key=lambda w: w["start"])
    try:
        (edit / "cut_speech.md").write_text(render_cut_markdown(data, cut_phrases(data)), encoding="utf-8")
    except OSError:
        pass
    timeline = load_json(edit / f'{video.stem}_timeline.json') or {}
    groups_value = intervals_value(timeline, load_json(edit / "edl.json") or {}) if timeline.get("segments") else []
    if not groups_value or not any(g["quote"] for g in groups_value):
        lines_value = ['(no interval quotes; showing recorded speech with phrase onset times)']
        return lines_value + [f"  {fmt_time(p['start'])}  {p['text']}" for p in cut_phrases(data)]
    lines_value, n_dif = [], 0
    for g, ws in zip(groups_value, words_per_interval(words, groups_value)):
        heard_value = " ".join(w["text"] for w in ws)
        mark_value = ""
        if g["dry"]:
            mark_value = '  dry is false; comparison skipped'
        elif g["quote"]:
            missing_value, remainder = difference(g["quote"], heard_value)
            if missing_value or remainder:
                n_dif += 1
                parts_value = ([f'''missing "{' '.join(missing_value)}"'''] if missing_value else []) +\
                         ([f'''extra "{' '.join(remainder)}"'''] if remainder else [])
                mark_value = "  ≠ check: " + ", ".join(parts_value)
        beat_label = f"{g['beat']} · " if g["beat"] else ""
        lines_value.append(f'''{fmt_time(g['start'])}  {beat_label}written: "{g['quote']}"{mark_value}''')
        cut_value = [f["words"] for f in split_long(group_into_phrases(ws, 0.3))] if ws else []
        if not cut_value:
            lines_value.append("          (nothing spoken)")
        for c in cut_value:
            lines_value.append(f"  {fmt_time(float(c[0]['start']))}  {' '.join((w['text'] for w in c))}")
    summary_value = (f'{n_dif} intervals differ from their quotes. Listen for clipped boundaries, abandoned takes, or transcription differences' if n_dif else
              "Recorded speech matches every interval's quote, ignoring accents and punctuation.")
    return lines_value + ["", summary_value]


# ------------------------------------------------------------------ breaths, minus the ranges kept whole

def breaths_of_cut(out: str, edit: Path, video: Path) -> tuple[list[str], int]:
    """Report of breaths.py --json with the silences inside a "dry": false range listed apart."""
    try:
        rep = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return [out], -1
    edl = load_json(edit / "edl.json") or {}
    dry_ids = {r.get("id") for r in edl.get("ranges", []) if not r.get("dry", True)}
    segs = [s for s in (load_json(edit / f'{video.stem}_timeline.json') or {}).get("segments", [])
            if s.get("id") in dry_ids]
    count_value, fica = [], []
    for r in rep.get("breaths", []):
        inside_value = next((s for s in segs if r["start"] >= float(s["out_start"]) - 0.05
                       and r["end"] <= float(s["out_end"]) + 0.05), None)
        (fica if inside_value else count_value).append((r, inside_value))
    th, mn = rep.get("thresholdDb"), rep.get("min_s", 0.25)
    lines_value = []
    if not count_value:
        lines_value.append(f'breaths: no silence of {mn:.2f} s or more (below {th:.0f} dB)')
    else:
        lines_value.append(f'breaths: {len(count_value)} silences of {mn:.2f} s or more (below {th:.0f} dB):')
        for r, _ in count_value:
            lines_value.append(f"  {fmt_time(r['start'])} to {fmt_time(r['end'])}  ({r['dur']:.2f} s)")
        lines_value.append('  review each detected pause against the intended pacing; adjust only authorized intervals and preserve complete words')
    for r, s in fica:
        lines_value.append(f'''  retain: {fmt_time(r['start'])} to {fmt_time(r['end'])} ({r['dur']:.2f} s) inside interval {s.get('id')} with "dry": false ("{(s.get('quote') or '')[:40]}")''')
    return lines_value, len(count_value)


BREATHS = 'Remaining audible breaths or pauses'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument('edit_dir', type=Path)
    ap.add_argument("--video", default="cut.mp4", help='cut filename within the folder (default cut.mp4)', dest='video')
    ap.add_argument("--prompt", default=None, help='proper names occurring in speech, as accepted by transcribe.py', dest='prompt')
    ap.add_argument("--min-silence", type=float, default=None, help="passed to verify_cut.py", dest='min_silence')
    args = ap.parse_args()

    edit = args.edit_dir.resolve()
    video, edl = edit / args.video, edit / "edl.json"
    for p in (video, edl):
        if not p.exists():
            print(f'error: not found {p}', file=sys.stderr)
            return 2

    h = lambda name: str(HELPERS / name)  # noqa: E731
    # State the prompt regime: this run rewrites transcripts/<cut>.json, which compose reads for captions.
    tr = [h("transcribe.py"), str(video), "--edit-dir", str(edit)] + (["--prompt", args.prompt] if args.prompt else ["--no-prompt"])
    vf = [h("verify_cut.py"), str(edl), str(video), "--without-approval", '--report-only']
    if args.min_silence is not None:
        vf += ["--min-silence", str(args.min_silence)]

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {'Cut transcription': pool.submit(run, tr),
                BREATHS: pool.submit(run, [h("breaths.py"), str(video), "--json"]),
                'Boundaries, loudness, and silence: verify_cut': pool.submit(run, vf),
                'Join contact sheet': pool.submit(run, [h("contact_sheet.py"), str(video), "--junctions"])}
        failed, breaths = False, 0
        for title, fut in jobs.items():
            code, out, dt = fut.result()
            if title == BREATHS and code in (0, 1):
                lines_value, breaths = breaths_of_cut(out, edit, video)
                failed |= breaths < 0
                breaths = max(0, breaths)
                out = "\n".join(lines_value)
                code = 0
            else:
                failed |= code != 0
            print(f"=== {title} · {dt:.0f} s{('' if code == 0 else f' · ERROR (exit {code})')}\n{out}\n")
    print('=== Cut speech by interval: expected EDL quote followed by recorded words and timing')
    print("\n".join(speech_of_cut(edit, video)) + "\n")
    steps_value = ["compare the cut speech above with each interval's quote"]
    if breaths:
        steps_value.append('review remaining unnecessary pauses (' + cmd("waveform_cut.py", f"'{edit / 'edl_manual.json'}'",
                                                            "-o", f"'{edl}'", "--threshold <3 dB higher>") + ")")
    steps_value.append('next, measure face clearance and review the cut in the studio; see references/cut.md')
    print(f'Verification completed in {time.monotonic() - t0:.0f} s. Next: ' + "; ".join(steps_value) + ".")
    return 1 if failed or breaths else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
