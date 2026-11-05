#!/usr/bin/env python3
"""Insert generated footage as a synchronized picture-only camera, retaining original speech. Estimate delay by motion correlation and refine with frame edges; use 71 ms when correlation is weak. Save synchronization.json and optionally split EDL ranges at the requested source times. Preserve original files as .before-ai.json backups and add silent audio to the camera copy for waveform analysis. Re-render with waveform_cut.py and render_cut.py."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
import numpy as np
DEFAULT_DELAY_MS = 71.0
MIN_SEGMENT = 0.3

def info(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 'stream=width,height,r_frame_rate,avg_frame_rate,nb_read_packets', '-show_entries', 'format=duration', '-of', 'json', path], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if out.returncode != 0:
        sys.exit(f'error: could not read {path}')
    d = json.loads(out.stdout)
    s = d['streams'][0]
    (num, den) = (s.get('avg_frame_rate') or s['r_frame_rate']).split('/')
    fps = float(num) / float(den) if float(den) else 30.0
    return dict(w=int(s['width']), h=int(s['height']), fps=fps, frames=int(s.get('nb_read_packets') or 0), dur=float(d['format'].get('duration') or 0))

def small_frames(path, i):
    (w, h) = (90, 160) if i['h'] >= i['w'] else (160, 90)
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', f'scale={w}:{h},format=gray', '-f', 'rawvideo', '-'], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w).astype(np.float32)

def motion(a, fps):
    """Sample central-frame motion energy every 5 ms using adjacent-frame differences."""
    h = a.shape[1]
    (y0, y1) = (int(h * 0.15), int(h * 0.8))
    e = np.abs(np.diff(a[:, y0:y1, :], axis=0)).mean((1, 2))
    t = (np.arange(len(e)) + 0.5) / fps
    g = np.arange(0, t[-1], 0.005)
    s = np.interp(g, t, e)
    return (s - s.mean()) / (s.std() + 1e-06)

def edges(a):
    """Normalize frame edges to tolerate generated color and brightness changes."""
    g = np.abs(np.diff(a, axis=2))[:, :-1, :] + np.abs(np.diff(a, axis=1))[:, :, :-1]
    return (g - g.mean((1, 2), keepdims=True)) / (g.std((1, 2), keepdims=True) + 1e-06)

def match_frames(qe, fe, qi, fi, estimate):
    """Match generated frames near the motion estimate; return delay and valid fraction. Averaging frame-time differences estimates delay despite the generated frame-rate grid."""
    (be, bi) = (edges(qe), edges(qi))
    difs = []
    for j in range(len(bi)):
        c = int(round((j / fi - estimate) * fe))
        cand = [k for k in range(c - 3, c + 4) if 0 <= k < len(be)]
        if len(cand) < 5:
            continue
        d = np.array([np.abs(be[k] - bi[j]).mean() for k in cand])
        k = int(np.argmin(d))
        srt = np.sort(d)
        if srt[1] - srt[0] < 0.1 * srt[0]:
            continue
        difs.append(j / fi - cand[k] / fe)
    if len(difs) < max(8, 0.3 * len(bi)):
        return (None, len(difs) / max(1, len(bi)))
    d = np.sort(np.array(difs))
    cut = len(d) // 10
    d = d[cut:len(d) - cut] if len(d) - 2 * cut >= 5 else d
    return (float(d.mean()), len(difs) / len(bi))

def measure_delay(submitted, ai):
    (ie, ii) = (info(submitted), info(ai))
    (qe, qi) = (small_frames(submitted, ie), small_frames(ai, ii))
    (se, si) = (motion(qe, ie['fps']), motion(qi, ii['fps']))
    m = min(len(se), len(si))
    best = (0, -2.0)
    for lag in range(-80, 81):
        a = se[max(0, -lag):m - max(0, lag)]
        b = si[max(0, lag):m - max(0, -lag)]
        n = min(len(a), len(b))
        if n < 100:
            continue
        c = float(np.corrcoef(a[:n], b[:n])[0, 1])
        if c > best[1]:
            best = (lag, c)
    (delay, corr) = (best[0] * 0.005, best[1])
    (refined, frac) = match_frames(qe, ie['fps'], qi, ii['fps'], delay)
    mode = 'motion'
    if refined is not None and abs(refined - delay) < 0.06:
        (delay, mode) = (refined, f'image ({frac:.0%} of frames)')
    return (delay, corr, ie, ii, mode)

def source_key(edl, source):
    for (k, p) in (edl.get('sources') or {}).items():
        try:
            if os.path.exists(p) and os.path.samefile(p, source):
                return k
        except OSError:
            pass
    return None

def split_ranges(edl, primary_key, windows, gap_ok=0.02):
    """Split primary-source ranges at windows [(start, end, name)]; return ranges and report."""
    (new_items, rel) = ([], [])
    for r in edl['ranges']:
        if r.get('source') != primary_key or r.get('picture_source'):
            new_items.append(r)
            continue
        (start, end) = (float(r['start']), float(r['end']))
        points = [(start, None)]
        for (w0, w1, name) in windows:
            (o0, o1) = (max(start, w0), min(end, w1))
            if o1 - o0 >= MIN_SEGMENT:
                points.append((o0, name))
                points.append((o1, None))
        if len(points) == 1:
            new_items.append(r)
            continue
        points.sort(key=lambda p: p[0])
        seq = []
        for (k, (t, name)) in enumerate(points):
            t1 = points[k + 1][0] if k + 1 < len(points) else end
            if t1 - t > 0.0001:
                seq.append([t, t1, name])
        lim = {n: (a, b) for (a, b, n) in windows}
        for (k, s) in enumerate(seq):
            if s[2] is None and s[1] - s[0] < MIN_SEGMENT:
                neighbors = [seq[j] for j in (k - 1, k + 1) if 0 <= j < len(seq) and seq[j][2]]
                if neighbors:
                    v = neighbors[0]
                    (a, b) = lim[v[2]]
                    if s[0] >= a - gap_ok and s[1] <= b + gap_ok:
                        s[2] = v[2]
                    else:
                        rel.append(f"warning: {r['id']}: remaining {s[1] - s[0]:.2f} s without generated picture at {s[0]:.2f}-{s[1]:.2f}; the engine merges short pieces with neighbors. Adjust --start/--end or generate extra handles.")
        merged = []
        for s in seq:
            if merged and merged[-1][2] == s[2]:
                merged[-1][1] = s[1]
            else:
                merged.append(list(s))
        letters = 'abcdefghijklmnopqrstuvwxyz'
        for (k, (a, b, name)) in enumerate(merged):
            q = dict(r)
            q['id'] = f"{r['id']}{letters[k]}" if k < len(letters) else f"{r['id']}_{k + 1}"
            q['start'] = round(a, 4)
            q['end'] = round(b, 4)
            if name:
                q['picture_source'] = name
                q['reason'] = (r.get('reason') or '') + ' · generated picture'
            new_items.append(q)
        rel.append(f"{r['id']}: " + ' | '.join((f"{a:.2f}-{b:.2f} {('AI ' + n if n else 'original')}" for (a, b, n) in merged)))
    return (new_items, rel)

def main():
    ap = argparse.ArgumentParser(description='Insert generated video as a picture-only second camera (picture_source).')
    ap.add_argument('--request', required=True, help='JSON saved beside the submitted clip by clip_for_ai.py')
    ap.add_argument('--ai', required=True, help='Returned generated video')
    ap.add_argument('--name', default='AI1', help='Camera name in the EDL (default AI1); multiple pieces become AI1_p1, AI1_p2...')
    ap.add_argument('--edit', help='Edit directory containing synchronization.json')
    ap.add_argument('--edl', help='edl_manual.json to update sources and split ranges; preserves a backup')
    ap.add_argument('--source-name', help='Source key in the EDL (default: resolve by source file path)')
    ap.add_argument('--start', type=float, help='Original-source time where generated picture begins')
    ap.add_argument('--end', type=float, help='Original-source time where generated picture ends')
    ap.add_argument('--delay-ms', type=float, help='Use this delay instead of the measured value')
    ap.add_argument('--measure-only', action='store_true', help='Measure and report delay and source coverage without writing')
    a = ap.parse_args()
    request_data = json.load(open(a.request, encoding='utf-8'))
    submitted = request_data['file']
    if not os.path.exists(submitted):
        submitted = os.path.join(os.path.dirname(os.path.abspath(a.request)), os.path.basename(request_data['file']))
    (delay, corr, ie, ii, mode) = measure_delay(submitted, a.ai)
    print(f"submitted {ie['frames']} frames at {ie['fps']:.2f} fps ({ie['dur']:.2f} s) · AI {ii['frames']} frames at {ii['fps']:.2f} fps ({ii['dur']:.2f} s) · measured delay {delay * 1000:+.0f} ms (using {mode}; motion correlation {corr:.2f})")
    if a.delay_ms is not None:
        delay = a.delay_ms / 1000
        print(f'  using supplied delay: {a.delay_ms:+.0f} ms')
    elif corr < 0.5:
        delay = DEFAULT_DELAY_MS / 1000
        print(f'  warning: weak correlation (little movement or changed gesture). Using {DEFAULT_DELAY_MS:.0f} ms observed with Kling. Check lip sync; use --delay-ms for manual adjustment.')
    if ii['dur'] < ie['dur'] - 0.05:
        print(f"  generated video is {ie['dur'] - ii['dur']:.2f} s shorter: the source ending has no generated picture")
    pieces = request_data['pieces']
    names = []
    windows = []
    ai_sources = {}
    for (k, p) in enumerate(pieces):
        name = a.name if len(pieces) == 1 else f'{a.name}_p{k + 1}'
        C = p['sourceStart']
        offset = C - p['start'] + delay
        dur_p = p['frames'] / request_data['sourceFps']
        t0 = max(p['start'], p['start'] - C - delay) + 0.04
        t1 = min(p['start'] + dur_p, ii['dur'] - offset) - 0.04
        lo = a.start if a.start is not None else -1000000000.0
        hi = a.end if a.end is not None else 1000000000.0
        (w0, w1) = (max(t0, lo), min(t1, hi))
        print(f'  {name}: generated footage covers source from {t0:.3f} to {t1:.3f} s (offset {offset:+.4f})' + (f' · used from {w0:.3f} to {w1:.3f}' if a.start is not None or a.end is not None else ''))
        if a.start is not None and a.start < t0 - 0.001:
            print(f'  warning: --start {a.start} precedes generated coverage ({t0:.3f}): switching at {t0:.3f}')
        if w1 - w0 >= MIN_SEGMENT:
            windows.append((w0, w1, name))
        ai_sources[name] = {'file': os.path.abspath(a.ai), 'offset': round(offset, 6), 'scale': 1.0, 'adjustment_s': 0.0, 'confidence': 'good', 'has_video': True, 'sourceCoverage': [round(t0, 4), round(t1, 4)], 'delayMs': round(delay * 1000, 1), 'correlation': round(corr, 3), 'notes': 'Generated picture only (ai_in_cut.py); voice remains from the original source'}
        names.append(name)
    if a.measure_only:
        return
    if not a.edit:
        sys.exit('error: supply --edit <EDIT> to write synchronization.json, or use --measure-only')
    # Validate everything before the first write: a rejected run leaves files and project untouched.
    sync_path = os.path.join(a.edit, 'synchronization.json')
    primary_key = a.source_name
    edl = None
    if a.edl:
        edl = json.load(open(a.edl, encoding='utf-8'))
        # Resolve the file the renderer will read (render_cut.resolve_source rules).
        ref = os.path.expanduser(edl.get('synchronization') or 'synchronization.json')
        base = os.path.dirname(os.path.abspath(a.edl))
        ref = ref if os.path.isabs(ref) else next((c for c in (os.path.join(base, ref), os.path.join(os.path.dirname(base), ref)) if os.path.exists(c)), os.path.join(base, ref))
        if os.path.realpath(ref) != os.path.realpath(sync_path):
            sys.exit(f"error: the EDL renders with {ref}, but this script writes {sync_path}; use the EDL's edit folder or point the EDL at that file")
        primary_key = primary_key or source_key(edl, request_data['source'])
        if not primary_key:
            sys.exit(f"error: source {request_data['source']} is missing from sources in {a.edl}; supply --source-name")
    primary_key = primary_key or os.path.splitext(os.path.basename(request_data['source']))[0]
    doc = json.load(open(sync_path, encoding='utf-8')) if os.path.exists(sync_path) else {}
    sources = doc.get('sources', {})
    # Reuse only the copy registered for these camera names; never overwrite any other file, such as the
    # original footage or another camera (a rebuild from backups gets a new name).
    real = lambda path: os.path.realpath(str(path))
    files = [(n, v.get('file')) for (n, v) in sources.items() if isinstance(v, dict) and v.get('file')]
    files += list((edl or {}).get('sources', {}).items())
    owned = {real(f) for (n, f) in files if n in names}
    others = {real(f) for (n, f) in files if n not in names} | {real(request_data['source']), real(a.ai)}
    stem = os.path.splitext(os.path.abspath(a.ai))[0] + '_cam'
    (cam, k) = (stem + '.mp4', 2)
    while real(cam) in others or (os.path.exists(cam) and real(cam) not in owned):
        (cam, k) = (f'{stem}_{k}.mp4', k + 1)
    for f in ai_sources.values():
        f['file'] = cam
    if primary_key in sources and doc.get('primary') != primary_key and any(abs(float(sources[primary_key].get(k) or d) - d) > 1e-09 for (k, d) in (('offset', 0.0), ('adjustment_s', 0.0), ('scale', 1.0))):
        sys.exit(f"error: synchronization.json already has a different primary camera ({doc.get('primary')}); compose the offsets manually")
    for name in names:
        if name == primary_key:
            sys.exit(f'error: camera name {name} is the primary source; choose another --name')
        old = sources.get(name)
        if isinstance(old, dict) and old.get('file') != cam:
            sys.exit(f"error: camera {name} already uses {old.get('file')}; choose another --name for this clip")
        if edl is not None and edl.get('sources', {}).get(name) not in (None, cam):
            sys.exit(f"error: the EDL already maps {name} to {edl['sources'][name]}; choose another --name for this clip")
    r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.ai, '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k', '-t', f"{ii['dur']:.4f}", '-movflags', '+faststart', cam])
    if r.returncode != 0:
        sys.exit('error: could not write the camera copy with silent audio')
    print(f'ok {os.path.basename(cam)} (generated picture + silent audio: this file enters the EDL)')
    if os.path.exists(sync_path) and not os.path.exists(sync_path[:-5] + '.before-ai.json'):
        shutil.copy2(sync_path, sync_path[:-5] + '.before-ai.json')
    doc.setdefault('version', 1)
    doc.setdefault('primary', primary_key)
    doc['rule'] = 'source time = offset + adjustment_s + scale * primary time'
    doc['created_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    sources = doc.setdefault('sources', {})
    if primary_key not in sources:
        sources[primary_key] = {'file': request_data['source'], 'offset': 0.0, 'scale': 1.0, 'adjustment_s': 0.0, 'confidence': 'primary', 'has_video': True}
    for (name, f) in ai_sources.items():
        if isinstance(sources.get(name), dict):
            f['adjustment_s'] = float(sources[name].get('adjustment_s') or 0.0)
        sources[name] = f
    with open(sync_path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    print(f"ok {sync_path}: {', '.join(names)} (primary {primary_key})")
    if edl is not None:
        backup =os.path.splitext(a.edl)[0] + '.before-ai.json'
        if not os.path.exists(backup):
            shutil.copy2(a.edl, backup)
        for name in names:
            edl.setdefault('sources', {})[name] = cam
        (edl['ranges'], rel) = split_ranges(edl, primary_key, windows)
        with open(a.edl, 'w', encoding='utf-8') as fh:
            json.dump(edl, fh, ensure_ascii=False, indent=2)
        for l in rel:
            print('  ' + l)
        if not any(('AI' in l for l in rel)):
            print('  warning: no EDL range overlaps generated coverage; check --start/--end and source')
        print(f'ok {a.edl} (original backup: {os.path.basename(backup)}; restore it and rerun this script for each clip to rebuild). Now rerun waveform_cut.py and render_cut.py.')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
