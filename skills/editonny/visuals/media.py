'Verify and restore bundled original videos without downloading files.'
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile

VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v'}


def is_link(path) -> bool:
    """Symlink or Windows directory junction: both redirect a path, so containment checks treat them alike."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == stat.IO_REPARSE_TAG_MOUNT_POINT


def media_path(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
            or any(p in {'', '.', '..'} for p in relative.split('/')) or PurePosixPath(relative).is_absolute()):
        raise ValueError(f'Invalid media path: {relative!r}')
    current = Path(root).expanduser().absolute()
    for part in ('', *PurePosixPath(relative).parts):
        current = current / part
        if is_link(current):
            raise ValueError(f'Media must not be a symlink: {current}')
    return current


def manifest(origin: Path) -> list:
    data = json.loads((origin / 'media.json').read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('videos'), list):
        raise ValueError('Invalid media manifest or unsupported version')
    files, destinations = set(), set()
    for item in data['videos']:
        if not isinstance(item, dict):
            raise ValueError('Invalid media entry')
        file = item.get('file')
        # Validate path syntax even when the package is incomplete.
        media_path(origin, file)
        if (file in files or Path(file).suffix.lower() not in VIDEO_EXTENSIONS
                or type(item.get('bytes')) is not int or item['bytes'] <= 0
                or not isinstance(item.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError(f'Invalid media entry: {file}')
        files.add(file)
        destination = item.get('destination')
        if destination is not None:
            media_path(origin, destination)
            if (not destination.startswith('assets/visuals/') or destination in destinations
                    or Path(destination).suffix.lower() not in VIDEO_EXTENSIONS):
                raise ValueError(f'Invalid media destination: {destination}')
            destinations.add(destination)
    return data['videos']


def verify(root: Path, item: dict) -> Path:
    path = media_path(root, item['file'])
    if not path.is_file():
        raise ValueError(f'Missing media: {path}. Reinstall the complete package or run editonny.py media import FOLDER to restore originals.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if path.stat().st_size != item['bytes'] or digest.hexdigest() != item['sha256']:
        raise ValueError(f'Media integrity mismatch: {path}. Preserve this file and reinstall the complete package in a different folder.')
    return path


def copy_new(source: Path, target: Path, item: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.import-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary)
        verify(temporary.parent, {**item, 'file': temporary.name})
        # Publish atomically without replacing a file created during the copy.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def import_media(origin: Path, source_root: Path) -> int:
    destination = origin
    pairs = []
    for item in manifest(origin):
        source = verify(source_root, item)
        target = media_path(destination, item['file'])
        if target.exists():
            verify(destination, item)
        else:
            pairs.append((source, target, item))
    for source, target, item in pairs:
        copy_new(source, target, item)
    return len(pairs)


def status(origin: Path) -> dict:
    items = manifest(origin)
    root = origin
    report = dict(root=str(root), total=len(items), bytes=sum(i['bytes'] for i in items), available=0, missing=[], invalid=[])
    for item in items:
        try:
            path = media_path(root, item['file'])
            if not path.exists():
                report['missing'].append(item['file'])
                continue
            verify(root, item)
            report['available'] += 1
        except (OSError, ValueError):
            report['invalid'].append(item['file'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    status_parser = sub.add_parser('status', help='check presence and SHA-256 hashes of bundled videos')
    status_parser.add_argument('--json', action='store_true')
    load = sub.add_parser('import', help='restore missing videos from a complete Visuals folder or intact backup')
    load.add_argument('source', type=Path)
    args = parser.parse_args()
    origin = Path(__file__).resolve().parent
    try:
        if args.command == 'import':
            count = import_media(origin, args.source.expanduser().absolute())
            print(f'{count} videos restored; originals preserved. Skill media: {origin}')
            return 0
        report = status(origin)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"Skill media: {report['root']}\n{report['available']}/{report['total']} valid videos; "
                  f"{len(report['missing'])} missing; {len(report['invalid'])} invalid.")
            if report['missing'] or report['invalid']:
                print('Run media status --json for paths. Reinstall the complete package or restore missing files with media import FOLDER.')
        return 0 if report['available'] == report['total'] else 1
    except (OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
