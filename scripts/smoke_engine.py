#!/usr/bin/env python3
"""Local synthetic render check; artifacts stay in .work, no paid services."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'skills/editonny/engine'
VISUALS = ROOT / 'skills/editonny/visuals'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hyperframes', help='Exact version to test in the temporary project only')
    args = parser.parse_args()
    if args.hyperframes and not re.fullmatch(r'\d+\.\d+\.\d+', args.hyperframes):
        parser.error('--hyperframes requires an exact version such as 0.8.171')
    work = ROOT / '.work'
    work.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='engine smoke ', dir=work))
    env = {k: v for k, v in os.environ.items()
           if not re.search(r'(KEY|TOKEN|SECRET|CREDENTIAL)', k, re.I)}

    def run(*args, cwd=ROOT):
        return subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                              check=True, text=True, capture_output=True, timeout=600, encoding='utf-8', errors='replace')

    edit = output / 'edit'
    edit.mkdir()
    (edit / 'transcripts').mkdir()
    run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=360x640:rate=24', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000', '-t', '3', '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', edit / 'cut.mp4')
    (edit / 'cut_timeline.json').write_text(json.dumps({
        'duration': 3, 'width': 360, 'height': 640, 'fps': 24,
        'segments': [{'out_start': 0}]}), encoding='utf-8')
    (edit / 'transcripts/cut.json').write_text(json.dumps({'words': [
        {'text': 'Editonny', 'start': 0.2, 'end': 1.1},
        {'text': 'works', 'start': 1.2, 'end': 2.6}]}), encoding='utf-8')
    project = output / 'project with spaces'
    run(sys.executable, ENGINE / 'project-template/build.py', 'new', project, '--edit', edit)
    if args.hyperframes:
        package = project / 'package.json'
        package.write_text(re.sub(r'hyperframes@\d+\.\d+\.\d+',
                                 'hyperframes@' + args.hyperframes, package.read_text(encoding='utf-8')), encoding='utf-8')
    run(sys.executable, VISUALS / 'apply.py', '--project', project,
        '--use', 'transition-glow', '--cut', '1.5')
    cfg = json.loads((project / 'project.json').read_text(encoding='utf-8'))
    assert cfg['scenes'], 'Scene application did not register its composition'
    run(sys.executable, 'build.py', 'prepare', cwd=project)
    run(sys.executable, 'build.py', 'compose', cwd=project)
    assert (project / 'index.html').is_file()
    for command in (['lint'], ['check'],
                    ['snapshot', '--at', '1,1.5,2', '--no-end', '--describe', 'false',
                     '--output', 'snapshots'], ['render', '--quality', 'draft',
                    '--fps', '24', '--output', 'renders/smoke.mp4']):
        print('HyperFrames:', ' '.join(command), flush=True)
        result = run(sys.executable, 'build.py', 'hf', *command, cwd=project)
        (output / (command[0] + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
    assert len(list((project / 'snapshots').glob('frame-*.png'))) == 3
    video = project / 'renders/smoke.mp4'
    probe = json.loads(run('ffprobe', '-v', 'error', '-show_streams',
                          '-show_format', '-of', 'json', video).stdout)
    stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert stream['width'] == 1080 and stream['height'] == 1920, stream
    assert 2.9 <= float(probe['format']['duration']) <= 3.2, probe['format']
    assert any(s['codec_type'] == 'audio' for s in probe['streams']), 'Audio missing'
    assert video.stat().st_size > 10000
    for second in (1, 1.5, 2):
        run('ffmpeg', '-v', 'error', '-ss', str(second), '-i', video,
            '-frames:v', '1', output / f'frame-{second}.png')
    (output / 'probe.json').write_text(json.dumps(probe, indent=2), encoding='utf-8')
    print(f'PASS: {video}\nFrame: {output / "frame-1.png"}')


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout, error.stderr, file=sys.stderr)
        raise
