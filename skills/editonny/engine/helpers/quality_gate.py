"""Quality gate for a rendered video: the edit is only delivered when this prints "PASS".

Checks the exported file (not the studio):
  1. black frames (0.1 s or more anywhere, the very first 0.1 s excepted);
  2. loudness: integrated -14 LUFS with 1.5 of margin, true peak under -1 dBTP;
  3. breaths: no silence of 0.25 s or more (helpers/breaths.py). The silence after the last spoken word does not
     count when the project has a still end (`endHold`) or a closing card (`endCard`): it is the end of the
     video, not a breath;
  4. text outside the Reels safe zone (x 140-940, y 230-1530 on 1080x1920) or touching the edge of the frame,
     read with the same reader as audit_data.py (Apple Vision on the Mac, Tesseract elsewhere). The stretches
     of `evidence`, `contain`, `compare` and `greenScreen` blocks are not read: the text there is on the person's own
     footage (a screen recording), not text of the edit. Without a reader the check says so and does not block;
  5. a sheet of frames with the safe zone box drawn on it, saved next to the report.

Usage:
    uv run python helpers/quality_gate.py <final.mp4> --project <PROJ> [--fps 3] [--sheet <png>] [--adjust-volume] [--json]

`--adjust-volume`: when only the loudness is off, rewrites the audio to -14 LUFS (two passes, the picture is not
re-encoded), keeps the original as `<name>_before_volume.mp4` and measures again. No new render needed.
Exit code 0 when everything passes, 1 when something failed, 2 on error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_data as cd  # noqa: E402
import waveform_cut as waveform  # noqa: E402
import breaths  # noqa: E402
from common import fmt_time, probe_video, require_binary  # noqa: E402

TARGET_LUFS, MARGIN_LUFS, PEAK_MAX_DB_VALUE = -14.0, 1.5, -1.0
BLACK_MIN_S = 0.1
BLACK_START_S = 0.1       # black this short at the very start (the first frame) is the video opening
FOOTAGE_SCENES = ("evidence", "contain", "compare", "greenScreen")   # footage of the person: its text is not ours
SAFE_ZONE = (140, 230, 940, 1530)          # x0, y0, x1, y1 on 1080x1920
EDGE_PX = 24                         # text closer than this to the edge of the frame is "touching"
CONF_MIN = 0.5
PADDING_PX = 6                          # the reader's box is not exact


def safe_zone_for(w: int, h: int) -> tuple[int, int, int, int] | None:
    """The safe zone scaled to the video size; only defined for vertical 9:16 (other formats have wider margins)."""
    if abs(w / h - 9 / 16) > 0.02:
        return None
    k = w / 1080
    return tuple(round(v * k) for v in SAFE_ZONE)  # type: ignore[return-value]


def black_intervals(video: Path, dur: float) -> list[tuple[float, float]]:
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(video), "-vf",
                        f'blackdetect=d={BLACK_MIN_S - 0.02:.2f}:pix_th=0.06', "-an", "-f", "null", "-"],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = []
    if r.returncode:
        raise RuntimeError('Could not decode video for black-frame inspection: ' + r.stderr[-300:])
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)", r.stderr):
        a, b = float(m[1]), float(m[2])
        if a < 0.05 and b - a <= BLACK_START_S + 0.05:
            continue
        out.append((a, b))
    return out


def volume(video: Path, complete: bool = False, target_lufs: float = -14, processing_peak: float = -1.5) -> dict:
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(video), "-vn", "-af",
                        f'loudnorm=I={target_lufs}:TP={processing_peak}:LRA=11:print_format=json', "-f", "null", "-"],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if r.returncode or not m:
        raise RuntimeError('Could not measure loudness. Does the file have audio?')
    d = json.loads(m.group(0))
    out = {"lufs": float(d["input_i"]), 'peak_db': float(d["input_tp"])}
    if not all(math.isfinite(value) for value in out.values()):
        raise RuntimeError('Loudness is not a finite voice measurement; silence requires an explicit audio policy')
    if complete:
        out["_loudnorm"] = d
    return out


def adjust_volume(video: Path, target_lufs: float = -14, processing_peak: float = -1.5, peak_limit: float = -1) -> Path:
    """Two-pass loudnorm to -14 LUFS (true peak -1.5), picture copied. The original stays next to it."""
    before_value = video.with_name(video.stem + "_before_volume" + video.suffix)
    if os.path.lexists(before_value):
        raise RuntimeError(f'Backup already exists: {before_value}. Choose another output name before adjusting volume.')
    d = volume(video, complete=True, target_lufs=target_lufs, processing_peak=processing_peak)["_loudnorm"]
    af = (f"loudnorm=I={target_lufs}:TP={processing_peak}:LRA=11:linear=true:measured_I={d['input_i']}:measured_TP={d['input_tp']}:measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}:offset={d['target_offset']}")
    with tempfile.NamedTemporaryFile(prefix=f'.{video.stem}-volume-', suffix=video.suffix,
                                     dir=video.parent, delete=False) as temporary:
        new_value = Path(temporary.name)
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-map", "0:v", "-map", "0:a",
                            "-c:v", "copy", "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", str(new_value)], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if r.returncode != 0 or not new_value.stat().st_size:
            raise RuntimeError('Could not adjust loudness: ' + r.stderr[-300:])
        measured = volume(new_value)
        if measured['peak_db'] > peak_limit or abs(measured['lufs'] - target_lufs) > 1.5:
            raise RuntimeError('Encoded audio did not meet the confirmed loudness/peak targets; original candidate preserved')
        with before_value.open('xb') as backup, video.open('rb') as source:
            shutil.copyfileobj(source, backup)
        shutil.copystat(video, before_value)
        os.replace(new_value, video)
    finally:
        new_value.unlink(missing_ok=True)
    return before_value


def text_outside(video: Path, w: int, h: int, fps: float, ignore: list[tuple[float, float]] | None = None) -> dict:
    """Lines of text outside the safe zone or touching the edge. {'reader': ..., 'findings': [...]}."""
    safe_zone = safe_zone_for(w, h)
    engine_value = cd.reader_value()
    if engine_value is None:
        return {"reader": None, "findings": [], "safe_zone": safe_zone, "ignored": ignore or []}
    info = probe_video(video)
    fps_video = float(info.get("fps") or 30.0)
    dur = float(info["duration"])
    n_total = int(dur * fps - 1e-6) + 1
    findings: list[dict] = []
    recognized = 0
    with tempfile.TemporaryDirectory(prefix="editonny-quality-") as tmp:
        directory = Path(tmp)
        n = cd.extract_frames(video, directory, 0, n_total, fps, fps_video, True, "")
        raw_value = cd.read_directory(engine_value, directory) if n else {}
    recognized = sum(len(lines) for lines in raw_value.values())
    ignore = ignore or []
    for k in range(n):
        t = k / fps
        if any(x - 0.05 <= t <= y + 0.05 for x, y in ignore):
            continue
        for it in raw_value.get(f'f_{k + 1:05d}.jpg', []):
            if float(it.get("c", 1)) < CONF_MIN or len(re.sub(r"\W", "", str(it.get("t", "")))) < 2:
                continue
            x0, y1 = float(it["x"]) * w, (1 - float(it["y"]) - float(it["h"])) * h
            x1, y2 = x0 + float(it["w"]) * w, y1 + float(it["h"]) * h
            reason_value = None
            if x0 < EDGE_PX or y1 < EDGE_PX or x1 > w - EDGE_PX or y2 > h - EDGE_PX:
                reason_value = "touching the frame edge"
            elif safe_zone and (x0 < safe_zone[0] - PADDING_PX or x1 > safe_zone[2] + PADDING_PX
                           or y1 < safe_zone[1] - PADDING_PX or y2 > safe_zone[3] + PADDING_PX):
                reason_value = "outside the safe zone"
            if reason_value:
                findings.append({"t": round(t, 2), "text": str(it["t"]).strip(), "reason": reason_value,
                                "box": [round(x0), round(y1), round(x1 - x0), round(y2 - y1)]})
    # the same line seen in a row of samples is one finding
    together: list[dict] = []
    for a in sorted(findings, key=lambda a: (a["text"], a["t"])):
        if together and together[-1]["text"] == a["text"] and a["t"] - together[-1]["end"] <= 1.5 / fps + 1e-6:
            together[-1]["end"] = a["t"]
        else:
            together.append(dict(a, end=a["t"]))
    return {"reader": engine_value[0], "findings": sorted(together, key=lambda a: a["t"]), "safe_zone": safe_zone, "ignored": ignore, 'recognized': recognized}


def footage_intervals(cfg: dict, project: Path | None, dur: float) -> list[tuple[float, float]]:
    """Time ranges (s) of the blocks that show the person's own footage. A time written as a word is looked up in
    the words of the cut; without the transcript only the blocks written in seconds are found."""
    import camera_motion as cv  # noqa: PLC0415
    ws: list[dict] = []
    vdur = dur
    if project:
        try:
            edit = (project / cfg.get("edit", "../edit")).resolve()
            ws = cv.words_of_cut(edit)
            vdur = float(json.loads((edit / "cut_timeline.json").read_text(encoding="utf-8"))["duration"])
        except Exception:  # noqa: BLE001
            ws = []
    out = []
    for b in cfg.get("blocks") or []:
        if not isinstance(b, dict) or b.get("scene") not in FOOTAGE_SCENES:
            continue
        start_value = cv.anchor_time(b.get("start", 0.0), ws, float(b.get("startAfter", b.get("after", 0.0)) or 0.0))
        end_value = cv.anchor_time(b["end"], ws, max(start_value or 0.0, float(b.get("endAfter", 0.0) or 0.0))) if "end" in b else vdur
        if start_value is not None and end_value is not None:
            out.append((start_value, end_value))
    return out


def scoped_quiet(cfg: dict, project: Path | None, quiet: list) -> list:
    """Quiet intervals this revision may cut: never inside a protected source interval and, for an interval
    revision, only inside its confirmed source intervals (mapped to output time through the cut timeline)."""
    editing = cfg.get('workflow', {}).get('editing', {})
    if not project or not (editing.get('protectedIntervals') or editing.get('scope') == 'intervals'):
        return quiet
    from workspace import selected_sources  # noqa: PLC0415
    edit = project / cfg.get('edit', '../edit')
    manifest = json.loads((edit / 'source_manifest.json').read_text(encoding='utf-8'))
    paths = {s['id']: str(Path(s['path']).resolve()) for s in selected_sources(manifest, cfg)['sources']}
    segments = json.loads((edit / 'cut_timeline.json').read_text(encoding='utf-8'))['segments']

    def output(intervals):
        return [(s['out_start'] + max(i['start'], s['src_start']) - s['src_start'],
                 s['out_start'] + min(i['end'], s['src_end']) - s['src_start'])
                for i in intervals for s in segments
                if str(Path(s['source_path']).resolve()) == paths.get(i['sourceId'])
                and min(i['end'], s['src_end']) > max(i['start'], s['src_start'])]

    for a, b in output(editing.get('protectedIntervals', [])):     # Keep only the unprotected parts.
        quiet = [p for x, y in quiet for p in ((x, min(y, a)), (max(x, b), y)) if p[1] > p[0]]
    if editing.get('scope') == 'intervals':                       # And only the parts this revision may cut.
        windows = []
        for a, b in sorted(output(editing['intervals'])):          # Segments that meet across a cut form one window.
            if windows and a <= windows[-1][1] + 1e-6:
                windows[-1] = (windows[-1][0], max(windows[-1][1], b))
            else:
                windows.append((a, b))
        quiet = [(max(x, a), min(y, b)) for x, y in quiet for a, b in windows if min(y, b) > max(x, a)]
    return [r for r in quiet if r[1] - r[0] >= breaths.MIN_S]


def end_of_speech(cfg: dict, dur: float) -> float | None:
    """Where the voice ends in the final video: the duration minus the still end and the closing card. None when
    the project has neither (then the last silence counts like any other)."""
    hold_value = float(cfg.get("endHold") or 0)
    cf = cfg.get("endCard") or {}
    card_value = float(cf.get("seconds", 1.8)) if cf.get("enabled") else 0.0
    return dur - hold_value - card_value if (hold_value > 0 or card_value > 0) else None


def sheet_value(video: Path, w: int, h: int, times_value: list[float], output_value: Path) -> Path:
    from PIL import ImageDraw  # noqa: PLC0415
    import contact_sheet as cs  # noqa: PLC0415
    tw = 270
    th = round(tw * h / w)
    frames_value = cs.extract_frames(video, times_value, width=tw, height=th)
    safe_zone = safe_zone_for(w, h)
    for im in frames_value:
        d = ImageDraw.Draw(im)
        if safe_zone:
            k = tw / w
            d.rectangle([round(v * k) for v in safe_zone], outline=(255, 60, 60), width=2)
    labels_value = [fmt_time(t) for t in times_value]
    cs.build_sheet(frames_value, labels_value, 6, f'{video.name} · red box = safe zone').save(output_value)
    return output_value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Exported-video quality gate: deliver only after a PASS result.')
    ap.add_argument('video', type=Path)
    ap.add_argument("--project", type=Path, help='project folder (reads endHold and saves the contact sheet in renders/)', dest='project')
    ap.add_argument("--fps", type=float, default=3.0, help='text-recognition frames per second (default 3)', dest='fps')
    ap.add_argument("--sheet", type=Path, help='contact-sheet PNG (default: <project>/renders/quality_gate.png)', dest='sheet_value')
    ap.add_argument("--adjust-volume", action="store_true",
                    help='if only loudness fails, remux audio at -14 LUFS without rendering video again, then remeasure', dest='adjust_volume')
    ap.add_argument("--json", action="store_true", dest='json')
    a = ap.parse_args(argv)
    if not a.video.is_file():
        print(f'ERROR: not found {a.video}', file=sys.stderr)
        return 2
    try:
        require_binary("ffmpeg")
        info = probe_video(a.video)
        W, H = int(info.get("display_width") or info["width"]), int(info.get("display_height") or info["height"])
        dur = float(info["duration"])
        cfg = {}
        if a.project and (a.project / "project.json").is_file():
            cfg = json.loads((a.project / "project.json").read_text(encoding="utf-8"))
        blacks_value = black_intervals(a.video, dur)
        audio_policy = cfg.get('workflow', {}).get('audio', {})
        target_lufs = audio_policy.get('targetLufs', TARGET_LUFS)
        peak_max = audio_policy.get('truePeakLimit', PEAK_MAX_DB_VALUE)
        normalize = audio_policy.get('mode', 'normalize') == 'normalize'
        vol = volume(a.video) if info.get('has_audio', True) and normalize else None
        volume_adjusted = None
        cleanup = cfg.get('workflow', {}).get('editing', {}).get('scope', 'full') in ('full', 'intervals')
        db = waveform.envelope(a.video, None) if info.get('has_audio', True) and cleanup else None
        th = waveform.threshold(db) if db is not None else None
        quiet_intervals = scoped_quiet(cfg, a.project, breaths.find(db, th, breaths.MIN_S) if db is not None else [])
        end_voice = end_of_speech(cfg, dur)
        ending_silence = []
        if end_voice is not None:                 # silence after the last word is the still end / the card, not a breath
            ending_silence = [r for r in quiet_intervals if r[0] >= end_voice - 0.3]
            quiet_intervals = [r for r in quiet_intervals if r not in ending_silence]
        txt = text_outside(a.video, W, H, a.fps, footage_intervals(cfg, a.project, dur))
    except (RuntimeError, OSError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2

    failures: list[str] = []
    blocked: list[str] = []
    if blacks_value:
        failures.append("black frame: " + ", ".join(f'{fmt_time(x)} to {fmt_time(y)}' for x, y in blacks_value))
    if vol and abs(vol["lufs"] - target_lufs) > MARGIN_LUFS:
        failures.append(f"volume {vol['lufs']:.1f} LUFS (target: {target_lufs:g}, margin {MARGIN_LUFS:g}); adjust output gain and check again")
    if vol and vol['peak_db'] > peak_max:
        failures.append(f"peak {vol['peak_db']:.1f} dBTP (limit: {peak_max:g}): lower the gain or apply the limiter")
    if quiet_intervals:
        failures.append("quiet interval: " + ", ".join(f'{fmt_time(x)} ({y - x:.2f} s)' for x, y in quiet_intervals))
    for f in txt["findings"]:
        failures.append(f'''text {f['reason']}: "{f['text'][:40]}" at {fmt_time(f['t'])}'''
                      + (f" to {fmt_time(f['end'])}" if f["end"] > f["t"] else ""))
    if txt['reader'] is None and cfg.get('captions', {}).get('enabled', True):
        blocked.append('text check not performed: install a local reader or provide an explicitly reviewed artifact')
    if a.adjust_volume and failures and all(f.startswith(('volume ', 'peak ')) for f in failures):
        if cfg.get('workflow') and (not normalize or target_lufs != -14 or peak_max != -1):
            print('ERROR: Automatic adjustment uses a different target; render with the confirmed audio policy.', file=sys.stderr)
            return 2
        volume_adjusted = adjust_volume(a.video)
        vol = volume(a.video)
        failures = []
        if abs(vol['lufs'] - TARGET_LUFS) > MARGIN_LUFS:
            failures.append('volume remains outside the required target after adjustment')
        if vol['peak_db'] > PEAK_MAX_DB_VALUE:
            failures.append('peak remains above the required encoded limit after adjustment')
    output_value = a.sheet_value or ((a.project / "renders" / "quality_gate.png") if a.project else a.video.with_name("quality_gate.png"))
    times_value = sorted({round(min(dur - 0.05, x), 2) for x in
                     [0.1] + [f["t"] for f in txt["findings"]][:6] + [i * dur / 11 for i in range(1, 12)]})[:18]
    try:
        output_value.parent.mkdir(parents=True, exist_ok=True)
        sheet_value(a.video, W, H, times_value, output_value)
    except Exception as e:  # noqa: BLE001  (the sheet is a help to the eye, it never fails the gate)
        output_value = None
        print(f'Warning: could not build the contact sheet ({e})', file=sys.stderr)

    media = None
    if cfg.get('workflow'):
        from fidelity import verify_media
        try:
            media = verify_media(a.video, a.project)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            media = {'status': 'blocked', 'detail': str(error)}
        if media['status'] == 'failed':
            failures.append('media contract or source appearance did not pass; inspect mediaChecks')
        elif media['status'] != 'passed':
            blocked.append('required media checks could not complete; inspect mediaChecks')
    status = 'failed' if failures else 'blocked' if blocked else 'passed'
    exit_code = {'passed': 0, 'failed': 1, 'blocked': 2}[status]
    report = {'status': status, "passed": status == 'passed', "failures": failures, 'blocked': blocked, "duration": round(dur, 2), "volume": vol,
           'mediaChecks': media,
           "quiet_threshold_db": th, 'text_reader': txt["reader"], "sheet": str(output_value) if output_value else None}
    if a.json:
        print(json.dumps(report, ensure_ascii=False))
        return exit_code
    print(f'quality gate · {a.video.name} · {dur:.1f} s · {W}x{H}')
    print(f"  black frame: {('none' if not blacks_value else len(blacks_value))}")
    if vol:
        print(f"  volume: {vol['lufs']:.1f} LUFS, peak {vol['peak_db']:.1f} dBTP"
              + (f' (adjusted; original saved at {volume_adjusted.name})' if volume_adjusted else ""))
    else:
        print('  volume: source audio preserved or no audio present; normalization check not applicable')
    print(f"  quiet intervals lasting at least 0.25 s: {('none' if not quiet_intervals else len(quiet_intervals))}"
          + (f' (ending silence of {ending_silence[-1][1] - ending_silence[0][0]:.1f} seconds belongs to an intentional hold or end card and is excluded)' if ending_silence else ""))
    if txt["reader"] is None:
        print('  on-screen text was NOT checked because no text reader is available; inspect the contact sheet')
    elif txt["safe_zone"] is None:
        print(f'  on-screen text: only the boundary was checked (format {W}x{H}; safe-area rules apply to 9:16)')
    else:
        print(f"  text outside the safe zone or on the edge: {('none' if not txt['findings'] else len(txt['findings']))} (reader {txt['reader']})"
              + (f"; embedded screen text was not recognized in {', '.join((fmt_time(x) + '-' + fmt_time(y) for (x, y) in txt['ignored']))}"
                 if txt.get("ignored") else ""))
    if output_value:
        print(f'  contact sheet: {output_value}')
    if failures:
        print("FAIL:")
        for f in failures:
            print(f'  - {f}')
        print('Fix the findings above, render again, and rerun the quality gate before delivery.')
        return exit_code
    if blocked:
        print('BLOCKED:')
        for reason in blocked:
            print(f'  - {reason}')
        return exit_code
    print("PASS")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
