#!/usr/bin/env python3
"""Export a VP9 person cutout using the engine matte helper.

Run: python3 tools/person_cutout.py --edit EDIT [--start SECONDS --end SECONDS]
     [--skill ENGINE] [--output assets/person.webm]

The cut timeline resets tracking at every shot boundary. Outputs person.webm and
person.json with the covered interval and edge statistics. Existing matte caches
are reused. Review a short sample of hair, hands, and edges before a full export."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

def error(msg, code=2):
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(code)

def count_frames(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', path], capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        return int(r.stdout.strip().split(',')[0])
    except ValueError:
        return -1

def default_engine():
    """Default engine."""
    candidates = [os.environ.get('EDITONNY_ENGINE_DIR') or '', os.path.join('~', '.claude', 'skills', 'editonny', 'engine'), os.path.join('~', '.codex', 'skills', 'editonny', 'engine')]
    for c in candidates:
        if c and os.path.exists(os.path.join(os.path.expanduser(c), 'helpers', 'matte.py')):
            return os.path.expanduser(c)
    return os.path.expanduser(candidates[1])

def project_engine():
    """Project engine."""
    try:
        with open(os.path.join(PROJECT, 'project.json'), encoding='utf-8') as f:
            s = str(json.load(f).get('skill') or '').strip()
    except (OSError, ValueError, AttributeError):
        return None
    return os.path.expanduser(s) if s else None

def find_engine(explicit=None):
    """Find engine."""
    if explicit:
        return (os.path.abspath(os.path.expanduser(explicit)), '--skill')
    s = project_engine()
    if s:
        return (os.path.abspath(s), 'project.json ("skill")')
    parent = os.path.dirname(PROJECT)
    if os.path.basename(PROJECT) == 'project-template' and os.path.exists(os.path.join(parent, 'helpers', 'matte.py')):
        return (parent, 'the engine containing this template')
    return (os.path.abspath(default_engine()), 'installed engine; set skill in project.json to select it explicitly')

def main():
    ap = argparse.ArgumentParser(description='Export a transparent person cutout')
    ap.add_argument('--edit', required=True, help='Edit directory containing cut.mp4 and cut_timeline.json')
    ap.add_argument('--skill', default=None, help='Engine directory: project.json skill, template parent, EDITONNY_ENGINE_DIR, or installed editonny/engine')
    ap.add_argument('--output', default=os.path.join(PROJECT, 'assets', 'person.webm'))
    ap.add_argument('--crf', type=int, default=20)
    ap.add_argument('--start', type=float, default=None, help='Interval start in cut seconds')
    ap.add_argument('--end', type=float, default=None, help='Interval end in cut seconds')
    a = ap.parse_args()
    edit = os.path.abspath(os.path.expanduser(a.edit))
    (skill, source) = find_engine(a.skill)
    print(f'skill: {skill} ({source})', flush=True)
    cut = os.path.join(edit, 'cut.mp4')
    tl_p = os.path.join(edit, 'cut_timeline.json')
    for p in (cut, tl_p):
        if not os.path.exists(p):
            error(f'missing {p}')
    if os.path.islink(cut):
        error('cut.mp4 must be a real file in the edit directory so matte processing resets at every cut.')
    if not os.path.exists(os.path.join(skill, 'helpers', 'matte.py')):
        error(f'Engine not found at {skill} (use --skill or project.json skill)')
    with open(tl_p, encoding='utf-8') as f:
        tl = json.load(f)
    (fps, W, H) = (tl['fps'], tl['width'], tl['height'])
    print('Extracting the person (first run takes about 3 minutes per minute of video)...', flush=True)
    proc = subprocess.Popen(['uv', 'run', '--directory', skill, 'python', 'helpers/matte.py', '--edit-dir', edit, '--json'], stdout=subprocess.PIPE, text=True, bufsize=1, env=dict(os.environ, PYTHONUNBUFFERED='1'), encoding='utf-8', errors='replace')
    body = []
    for line in proc.stdout:
        if body or line.rstrip('\n') == '{':
            body.append(line)
        else:
            print('  ' + line.rstrip('\n'), flush=True)
    if proc.wait() != 0:
        error('Person cutout failed; see the preceding error', proc.returncode)
    if not body:
        error('Could not parse matte.py output')
    rep = json.loads(''.join(body))
    intervals = sorted(rep['ranges'], key=lambda x: x['start_frame'])
    for (x, y) in zip(intervals, intervals[1:]):
        if x['end_frame'] != y['start_frame']:
            error(f"Cutout intervals are not contiguous ({x['id']} ends at {x['end_frame']}, {y['id']} starts at {y['start_frame']})")
    total = intervals[-1]['end_frame'] - intervals[0]['start_frame']
    start_frame = max(0, int(round((a.start or 0) * fps)))
    end_frame = min(total, int(round(a.end * fps)) if a.end is not None else total)
    if end_frame <= start_frame:
        error('--end must be greater than --start')
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        list = os.path.join(tmp, 'mattes.txt')
        with open(list, 'w', encoding='utf-8') as f:
            for x in intervals:
                p = x['file'] if os.path.isabs(x['file']) else os.path.join(edit, x['file'])
                f.write("file '" + p.replace("'", "'\\''") + "'\n")
        trim_filter = ''
        if a.start is not None or a.end is not None:
            trim_filter = f',trim=start_frame={start_frame}:end_frame={end_frame},setpts=N/({fps}*TB)'
        filter = f'[0:v]setpts=N/({fps}*TB){trim_filter},format=rgba[c];[1:v]setpts=N/({fps}*TB){trim_filter},format=gray,scale={W}:{H}:flags=area[m];[c][m]alphamerge,format=yuva420p[v]'
        cmd = ['ffmpeg', '-v', 'error', '-y', '-i', cut, '-f', 'concat', '-safe', '0', '-i', list, '-filter_complex', filter, '-map', '[v]', '-r', str(fps), '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-b:v', '0', '-crf', str(a.crf), '-deadline', 'good', '-cpu-used', '4', '-row-mt', '1', '-auto-alt-ref', '0', '-metadata:s:v:0', 'alpha_mode=1', '-an', a.output]
        print('Composing video with transparent background...', flush=True)
        if subprocess.run(cmd).returncode != 0:
            error('FFmpeg failed to export person.webm', 1)
    expected = end_frame - start_frame
    done = count_frames(a.output)
    if abs(done - expected) > 1:
        print(f'warn: person.webm has {done} frames; cut has {expected}', file=sys.stderr)
    info = dict(video=os.path.basename(a.output), fps=fps, width=W, height=H, frames=done, start=round(start_frame / fps, 4), end=round(end_frame / fps, 4), intervals=[dict(id=x['id'], start=round(x['start_frame'] / fps, 4), end=round(x['end_frame'] / fps, 4), stats=x.get('stats') or {}) for x in intervals])
    with open(os.path.splitext(a.output)[0] + '.json', 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(f'ok: {a.output} ({done} frames, {len(intervals)} intervals)')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
