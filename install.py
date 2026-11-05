#!/usr/bin/env python3
"""Install one editonny copy and register links for the selected agents."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent / 'skills' / 'editonny'
MARKER = '.editonny-install.json'
EXCLUDED = {'.venv', '.git', '__pycache__', 'node_modules', '.pytest_cache',
            '.ruff_cache', '.DS_Store', 'Thumbs.db', MARKER}


def excluded(name):
    return (name in EXCLUDED or name == '.env' or name.startswith('.env.') and name != '.env.example'
            or name.endswith(('.pyc', '.pyo')))


def is_link(path):
    """Symlink or Windows directory junction: both redirect a path, so ownership checks treat them alike."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == stat.IO_REPARSE_TAG_MOUNT_POINT


def make_link(link, storage):
    """Register with a symlink; Windows without symlink privilege (error 1314) gets a junction instead."""
    try:
        link.symlink_to(storage, target_is_directory=True)
    except OSError as error:
        if os.name != 'nt' or getattr(error, 'winerror', None) != 1314:
            raise
        import _winapi
        _winapi.CreateJunction(str(storage.absolute()), str(link))


def fingerprint(source):
    digest = hashlib.sha256()
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if any(excluded(part) for part in relative.parts):
            continue
        if is_link(path):
            raise ValueError(f'Symlinks are not allowed in the package: {relative}')
        if path.is_file():
            digest.update(relative.as_posix().encode())
            digest.update(b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
    return digest.hexdigest()


def locations(home, target):
    home = Path(home).expanduser().absolute()
    storage = home / '.local/share/editonny'
    links = []
    if target in ('codex', 'both'):
        links.append(home / '.agents/skills/editonny')
    if target in ('claude', 'both'):
        config = os.environ.get('CLAUDE_CONFIG_DIR') if home == Path.home() else None
        links.append((Path(config).expanduser() if config else home / '.claude') / 'skills/editonny')
    if target not in ('codex', 'claude', 'both'):
        raise ValueError('target must be codex, claude, or both')
    return storage, links


def check_links(storage, links):
    for link in links:
        if is_link(link) and link.resolve() == storage.resolve():
            continue
        if link.exists() or is_link(link):
            raise ValueError(f'The existing path does not belong to this installation: {link}. Nothing was replaced.')


def sync_engine(storage):
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('uv not found. Install uv: https://docs.astral.sh/uv/getting-started/installation/')
    subprocess.run([uv, 'sync', '--locked', '--directory', str(storage / 'engine')], check=True)


def install(source, home, target='both', sync=True, dry_run=False):
    source = Path(source).resolve()
    if not (source / 'SKILL.md').is_file():
        raise ValueError(f'Incomplete package: {source}/SKILL.md')
    storage, links = locations(home, target)
    if source == storage.resolve() or source in storage.resolve().parents or storage.resolve() in source.parents:
        raise ValueError('Source and installation must be in separate directories.')
    check_links(storage, links)
    previous = {}
    if storage.exists() or is_link(storage):
        if is_link(storage):
            raise ValueError(f'The existing installation is an unowned symlink: {storage}')
        try:
            previous = json.loads((storage / MARKER).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError(f'The existing directory is not owned by the installer: {storage}') from None
        if not isinstance(previous, dict) or previous.get('product') != 'editonny':
            raise ValueError(f'The existing directory is not owned by editonny: {storage}')
    signature = fingerprint(source)
    changed = previous.get('sha256') != signature or fingerprint(storage) != signature
    print(f'editonny → {storage}')
    for link in links:
        print(f'  registration → {link}')
    if dry_run:
        print('Dry run: no files or environments changed.')
        return storage
    if sync and not shutil.which('uv'):
        raise RuntimeError('uv not found. Install uv or use --no-sync to prepare files only.')

    stage = backup = None
    created_links = []
    replaced = False
    try:
        if changed:
            storage.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.editonny-stage-', dir=storage.parent))
            shutil.copytree(source, stage, dirs_exist_ok=True,
                            ignore=lambda _, names: [n for n in names if excluded(n)])
            # Preserve local settings separately from the distributed package.
            for relative in ('.env', 'engine/.env'):
                old = storage / relative
                if old.is_file():
                    shutil.copy2(old, stage / relative)
            (stage / MARKER).write_text(json.dumps({'product': 'editonny', 'sha256': signature,
                'version': (source / 'VERSION').read_text(encoding='utf-8').strip()}, indent=2) + '\n', encoding='utf-8')
            if storage.exists():
                backup_root = storage.parent / 'editonny-backups'
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                storage.rename(backup)
            stage.rename(storage)
            stage = None
            replaced = True
        if sync:
            sync_engine(storage)
        for link in links:
            if not is_link(link):
                link.parent.mkdir(parents=True, exist_ok=True)
                make_link(link, storage)
                created_links.append(link)
    except BaseException:
        for link in created_links:
            link.unlink()
        if replaced and storage.exists():
            shutil.rmtree(storage)
        if backup is not None and backup.exists():
            backup.rename(storage)
        raise
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
    if backup:
        print(f'Previous version preserved at {backup}')
    print('Installation ready.' if changed else 'Package is already up to date.')
    if not sync:
        print('Dependencies were not installed (--no-sync). Run again without this option before editing.')
    print('Start a new conversation. Codex: $editonny. Claude Code: /editonny.')
    return storage


def uninstall(home, target='both', dry_run=False):
    storage, links = locations(home, target)
    check_links(storage, links)
    for link in links:
        if is_link(link):
            print(f'Remove registration: {link}')
            if not dry_run:
                link.unlink()
    print(f'Files and settings preserved: {storage}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['codex', 'claude', 'both'], default='both')
    parser.add_argument('--home', type=Path, default=Path.home(), help='alternate home for an isolated installation')
    parser.add_argument('--no-sync', action='store_true', help='prepare files without installing Python dependencies')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true', help='remove agent registrations, preserve files and settings')
    args = parser.parse_args()
    try:
        if args.uninstall:
            uninstall(args.home, args.target, args.dry_run)
        else:
            install(SOURCE, args.home, args.target, not args.no_sync, args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
