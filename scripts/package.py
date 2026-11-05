#!/usr/bin/env python3
"""Build the local plugin archive without caches, private files, or the web app (deployed separately)."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', 'venv', '.pytest_cache', '.ruff_cache', 'node_modules', '__pycache__', '.DS_Store', '.work',
            '.editonny-install.json', 'archive'}


def allowed(path):
    return path.suffix.lower() != '.pdf' and not any(p in EXCLUDED or p.startswith('.env') and p != '.env.example'
                   or p.lower().endswith(('.pyc', '.pyo', '.pem', '.key', '.p12', '.log')) for p in path.parts)


def is_link(path) -> bool:
    """Symlink or Windows directory junction: both redirect a path, so containment checks treat them alike."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == stat.IO_REPARSE_TAG_MOUNT_POINT


def reject_symlinks(path, root):
    for current in (path, *path.parents):
        if is_link(current):
            raise ValueError(f'Unexpected symlink in package: {current.relative_to(root)}')
        if current == root:
            return


def build(root, output, kind):
    root, output = Path(root), Path(output)
    reject_symlinks(root / 'skills/editonny/VERSION', root)
    version = (root / 'skills/editonny/VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION must use major.minor.patch format')
    if kind == 'plugin':
        base = root
        paths = [root / p for p in ('install.py', 'editonny.py', 'README.md', 'README.pt-BR.md', 'README.es.md', 'LICENSE.md')]
        paths += [root / name for name in ('PRODUCT.md', 'DESIGN.md') if (root / name).exists()]
        for folder in ('skills', '.codex-plugin', '.claude-plugin', '.agents/plugins', 'docs/assets/readme'):
            reject_symlinks(root / folder, root)
            paths.extend((root / folder).rglob('*'))
        paths += [root / 'docs' / p for p in ('architecture.md', 'design.md', 'installation.md', 'integrations.md', 'setup.md', 'validation.md')
                  if (root / 'docs' / p).exists()]
    else:
        raise ValueError('Package kind must be plugin')
    files = []
    for p in sorted(paths):
        relative = p.relative_to(base)
        if not allowed(relative):
            continue
        reject_symlinks(p, root)
        if p.is_file():
            files.append((p, relative.as_posix()))
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'editonny-{kind}-{version}.zip'
    temporary = destination.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for p, name in files:
                archive.write(p, name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with destination.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    destination.with_suffix('.zip.sha256').write_text(f'{digest.hexdigest()}  {destination.name}\n', encoding='utf-8')
    print(f'{destination.name}: {len(files)} files, {destination.stat().st_size / 1_000_000:.1f} MB')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['plugin'], default='plugin')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build(ROOT, args.output, args.kind)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
