#!/usr/bin/env python3
"""editonny: local video editing and visual scenes, guided by conversation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
import webbrowser

SKILL = Path(__file__).resolve().parents[1]
ENGINE = SKILL / 'engine'
VISUALS = SKILL / 'visuals'


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c))


def catalog(query=''):
    scenes = json.loads((VISUALS / 'catalog.json').read_text(encoding='utf-8'))['scenes']
    words = normalize(query).split()
    return [scene for scene in scenes if all(word in normalize(json.dumps(scene, ensure_ascii=False)) for word in words)]


def doctor(as_json=False, probe_media=False):
    checks = []
    def check(name, ok, detail, required=True):
        checks.append(dict(name=name, ok=bool(ok), detail=detail, required=required))
    check('Launcher Python', sys.version_info >= (3, 9), sys.version.split()[0])
    for name in ('uv', 'ffmpeg', 'ffprobe', 'node', 'npx'):
        path = shutil.which(name)
        check(name, path, path or f'{name} not found on PATH')
    python = ENGINE / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if python.exists():
        try:
            result = subprocess.run([str(python), '-c',
                'import numpy, PIL, cv2, onnxruntime; import sys; print(sys.version.split()[0])'],
                capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            check('Python engine', result.returncode == 0, result.stdout.strip() or 'Dependencies are incomplete; run install.py again.')
        except (OSError, subprocess.TimeoutExpired) as error:
            check('Python engine', False, str(error))
    else:
        check('Python engine', False, 'Environment missing. Run uv sync --locked in the skill engine directory.')
    try:
        scenes = catalog()
        missing = [c['id'] for c in scenes if not (VISUALS / 'scenes' / (c['id'] + '.html')).is_file()]
        check('Visual catalog', not missing, f'{len(scenes)} scenes; {len(missing)} missing files')
    except (OSError, ValueError, KeyError) as error:
        check('Visual catalog', False, str(error))
    check('HyperFrames', True, 'Pinned in engine/project-template/package.json; the first render may download the package.', False)
    sys.path.insert(0, str(ENGINE / 'helpers'))
    from media_policy import probe_capabilities
    import hashlib
    fingerprint = hashlib.sha256()
    for base in (SKILL / 'scripts', ENGINE / 'helpers', ENGINE / 'preview', ENGINE / 'project-template'):
        for path in sorted(base.rglob('*')):
            if path.is_file() and not any(part in ('__pycache__', 'node_modules', '.venv') for part in path.parts) and path.suffix in ('.py', '.mjs', '.js', '.json', '.html', '.md'):
                fingerprint.update(path.relative_to(SKILL).as_posix().encode())
                fingerprint.update(path.read_bytes())
    marker = SKILL / '.editonny-install.json'
    identity = {'packageRoot': str(SKILL), 'engineRoot': str(ENGINE), 'codeFingerprint': fingerprint.hexdigest(),
                'installedFingerprint': json.loads(marker.read_text(encoding='utf-8')).get('sha256') if marker.is_file() else None,
                'layout': 'engine/visuals', 'mediaIntegrity': 'Run media status to verify bundled media'}
    capabilities = probe_capabilities()
    report = {'product': 'editonny', 'version': (SKILL / 'VERSION').read_text(encoding='utf-8').strip(), 'identity': identity,
              'mediaCapabilities': capabilities,
              'ok': all(c['ok'] for c in checks if c['required']), 'checks': checks}
    if probe_media:
        from fidelity import probe_media_route
        report['mediaProbe'] = probe_media_route()
        report['ok'] = report['ok'] and report['mediaProbe']['status'] == 'passed'
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"editonny {report['version']} — local diagnostic")
        print(f"Package: {SKILL} · code {identity['codeFingerprint'][:12]}")
        for c in checks:
            print(f"{'OK' if c['ok'] else 'MISSING'} · {c['name']}: {c['detail']}")
        print('This diagnostic does not replace a render test or final video review.')
    return 0 if report['ok'] else 1


def guide(port, open_browser):
    # The guide is the Next.js app in web/ of the full checkout; the standalone skill does not include it.
    root = SKILL.parent.parent
    if not (root / 'web' / 'package.json').is_file():
        raise ValueError('The guide is not bundled with the standalone skill. Open the full project and run python3 editonny.py guide.')
    npm = shutil.which('npm')
    if not npm or not (root / 'node_modules' / 'next').is_dir():
        raise ValueError('Install the guide dependencies first: run npm ci in the project folder (Node.js 22.13+ or 24+).')
    # Loopback only. Next.js 16.4 treats its locale rewrites as external when bound to 127.0.0.1 and redirects in a loop.
    command = [npm, 'run', 'dev', '-w', '@editonny/web', '--', '--hostname', 'localhost', '--port', str(port)]
    print('editonny · starting the guide · Ctrl+C to stop', flush=True)
    process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    url = None
    try:
        for line in process.stdout:
            print(line, end='', flush=True)
            found = re.search(r'http://localhost:\d+', line)
            url = url or (found.group(0) if found else None)
            # Open only once the server is ready, at the port it reported (Next picks one for --port 0).
            if open_browser and url and 'Ready' in line:
                webbrowser.open(url)
                open_browser = False
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 0


def run(command, cwd=None):
    return subprocess.call([str(c) for c in command], cwd=cwd,
        env={**os.environ, 'EDITONNY_ENGINE_DIR': str(ENGINE)})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('doctor', help='check the environment and files without installing tools')
    p.add_argument('--json', action='store_true')
    p.add_argument('--probe-media', action='store_true', help='explicit short local media conformance probe; never installs tools')
    p = sub.add_parser('inspect', help='inspect selected footage and propose settings; do not start an edit')
    p.add_argument('sources', nargs='+', type=Path)
    p.add_argument('--requested', type=Path, help='explicit requested choices as JSON')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('workspace', help='confirmed revision ownership and validated local delivery')
    p.add_argument('action', choices=['init', 'status', 'seed', 'revise', 'variant', 'confirm', 'review', 'reference', 'recover', 'export'])
    p.add_argument('--project', type=Path)
    p.add_argument('--inspection', type=Path)
    p.add_argument('--settings', type=Path)
    p.add_argument('--confirmation', type=Path)
    p.add_argument('--changes', type=Path)
    p.add_argument('--scope', choices=['full', 'captions', 'intervals', 'audio', 'visual'])
    p.add_argument('--directory', type=Path)
    p.add_argument('--execution-context', choices=['local', 'remote'])
    p.add_argument('--candidate', type=Path)
    p.add_argument('--evidence', type=Path)
    p.add_argument('--variant', default='final')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('catalog', help='search visual scenes by name, category, or use')
    p.add_argument('query', nargs='*')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('new', help='create an editing project from a cut')
    p.add_argument('project', type=Path)
    p.add_argument('--edit', required=True, type=Path)
    p.add_argument('--style', default='paper', choices=['amber', 'paper', 'night'])
    sub.add_parser('scene', help='apply visual scenes; use scene --help for options')
    sub.add_parser('media', help='bundled media: media status [--json] | media import FOLDER')
    sub.add_parser('helper', help='run an engine helper: helper NAME [arguments]')
    p = sub.add_parser('guide', help='open the local web guide')
    p.add_argument('--port', type=int, default=4173)
    p.add_argument('--no-open', action='store_true')
    try:
        if argv and argv[0] == 'media':
            return run([sys.executable, VISUALS / 'media.py', *argv[1:]])
        if argv and argv[0] == 'scene' and len(argv) > 1 and argv[1] in ('-h', '--help'):
            return run([sys.executable, VISUALS / 'apply.py', '--help'])
        if argv and argv[0] == 'scene':
            return run([sys.executable, VISUALS / 'apply.py', *argv[1:]])
        if argv and argv[0] == 'helper':
            if len(argv) > 1 and argv[1] in ('-h', '--help'):
                names = sorted(p.stem for p in (ENGINE / 'helpers').glob('*.py') if '__main__' in p.read_text(encoding='utf-8'))
                print('usage: editonny.py helper NAME [arguments]\nhelpers: ' + ', '.join(names))
                return 0
            if len(argv) < 2:
                raise ValueError('Use helper NAME [arguments]. Run helper --help for available names.')
            name = argv[1].removesuffix('.py')
            if not re.fullmatch(r'[a-z][a-z0-9_]*', name) or not (ENGINE / 'helpers' / f'{name}.py').is_file():
                raise ValueError(f'Unknown helper: {name}')
            uv = shutil.which('uv')
            if not uv:
                raise ValueError('uv not found. Install uv before running the engine.')
            # Preserve the caller directory so relative media paths remain valid.
            return run([uv, 'run', '--locked', '--project', ENGINE, 'python', ENGINE / 'helpers' / f'{name}.py', *argv[2:]])
        args = parser.parse_args(argv)
        if args.command == 'doctor':
            return doctor(args.json, args.probe_media)
        if args.command in ('inspect', 'workspace'):
            engine_python = ENGINE / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            if engine_python.is_file() and Path(sys.prefix).resolve() != (ENGINE / '.venv').resolve():
                return run([engine_python, Path(__file__).resolve(), *argv])
            sys.path.insert(0, str(ENGINE / 'helpers'))
            from project_contract import read_json
            if args.command == 'inspect':
                from intake import inspect_sources, propose_settings
                report = inspect_sources(args.sources)
                try:
                    report['proposal'] = propose_settings(report, read_json(args.requested) if args.requested else {})
                except ValueError as error:
                    report['proposalBlocked'] = str(error)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            from workspace import (create_workspace, read_status, revise_workspace, record_confirmation,
                                   record_review, recover_revision, publish_export, seed_source)
            if args.action == 'init':
                if not all((args.inspection, args.settings, args.confirmation, args.execution_context)):
                    raise ValueError('init requires --inspection, --settings, --confirmation and explicit --execution-context local|remote')
                if args.execution_context == 'remote' and not args.directory:
                    raise ValueError('Remote Downloads is not the user computer; provide a confirmed accessible --directory')
                settings = read_json(args.settings)
                settings = settings.get('proposal', settings)
                settings = settings.get('config', settings)
                if settings['workflow']['delivery']['executionContext'] != args.execution_context:
                    raise ValueError('Execution context differs from the confirmed settings summary')
                project = create_workspace(args.directory or Path(settings['workflow']['delivery']['workspaceRoot']), settings.get('title', 'My video'),
                                           read_json(args.inspection), settings, read_json(args.confirmation))
                result = {'project': str(project), 'confirmation': 'confirmed'}
            else:
                if not args.project:
                    raise ValueError(f'{args.action} requires --project')
                if args.action == 'status':
                    result = read_status(args.project)
                elif args.action == 'recover':
                    result = recover_revision(args.project)
                elif args.action == 'seed':
                    result = {'cut': str(seed_source(args.project))}
                elif args.action in ('review', 'reference'):
                    if not args.candidate or not args.evidence:
                        raise ValueError('review/reference requires --candidate and --evidence')
                    result = record_review(args.project, args.candidate, read_json(args.evidence), args.action == 'reference')
                elif args.action == 'variant':
                    from project_contract import load_contract
                    original = load_contract(args.project)
                    project = revise_workspace(args.project, {}, original['workflow']['editing']['scope'], args.variant)
                    result = read_status(project)
                elif args.action == 'confirm':
                    if not args.confirmation:
                        raise ValueError('confirm requires --confirmation identifying the actual summary and reply')
                    result = record_confirmation(args.project, read_json(args.confirmation))
                elif args.action == 'revise':
                    if not args.changes or not args.scope:
                        raise ValueError('revise requires --changes and --scope')
                    project = revise_workspace(args.project, read_json(args.changes), args.scope)
                    if args.confirmation:
                        record_confirmation(project, read_json(args.confirmation))
                    result = read_status(project)
                else:
                    if not args.candidate:
                        raise ValueError('export requires --candidate')
                    from fidelity import verify_media
                    result = publish_export(args.project, args.candidate, verify_media(args.candidate, args.project), args.variant)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'catalog':
            scenes = catalog(' '.join(args.query))
            if args.json:
                print(json.dumps(scenes, ensure_ascii=False, indent=2))
            else:
                for scene in scenes:
                    print(f"{scene['id']} · {scene.get('category', 'Scene')} · {scene.get('name', scene.get('title', ''))}")
                print(f'{len(scenes)} scenes found.')
            return 0
        if args.command == 'new':
            return run([sys.executable, ENGINE / 'project-template/build.py', 'new', args.project.expanduser().resolve(),
                        '--edit', args.edit.expanduser().resolve(), '--style', args.style])
        if args.command == 'guide':
            return guide(args.port, not args.no_open)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
