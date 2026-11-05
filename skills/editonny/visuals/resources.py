'Materialize portable resources without replacing existing project files.'
import hashlib
from pathlib import Path
import re
from media import VIDEO_EXTENSIONS, is_link, media_path, copy_new, manifest, verify


def _files(origin: Path, root: Path):
    current = origin
    while current != root:
        if is_link(current):
            raise ValueError(f'Resources must not be symlinks: {current}')
        current = current.parent
    for file in sorted(origin.rglob('*')):
        if is_link(file):
            raise ValueError(f'Resources must not be symlinks: {file}')
        if file.is_file() and not any(p.startswith('.') for p in file.relative_to(origin).parts):
            yield file


def _copy(pairs, project: Path, videos=None) -> int:
    root = project.resolve()
    pairs = list(pairs)
    # Validate every destination before copying, including symlinked directories.
    for origin, destination in pairs:
        if is_link(origin) or not destination.resolve().is_relative_to(root):
            raise ValueError(f'Resource is outside the project or is a symlink: {destination}')
        current = destination
        while True:
            if is_link(current):
                raise ValueError(f'Destination must not be a symlink: {current}')
            if current.exists() and not (current.is_file() if current == destination else current.is_dir()):
                raise ValueError(f'Invalid resource destination type: {current}')
            if current == project:
                break
            current = current.parent
    total = 0
    for origin, destination in pairs:
        if destination.is_file():
            continue  # Timestamps cannot establish ownership of project customizations.
        if videos and destination in videos:
            copy_new(origin, destination, videos[destination])
            total += 1
            continue
        digest = hashlib.sha256()
        with origin.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        copy_new(origin, destination, {'bytes': origin.stat().st_size, 'sha256': digest.hexdigest()})
        total += 1
    return total


def copy_scenes(origin: Path, project: Path) -> int:
    origin = origin.resolve()
    project = project.resolve()
    folder = origin / 'scenes'
    return _copy(((p, project / 'compositions/scenes/visuals' / p.relative_to(folder))
                    for p in _files(folder, origin)), project)


def copy_assets(origin: Path, project: Path, text: str = '') -> int:
    origin = origin.resolve()
    project = project.resolve()
    assets = origin / 'assets/visuals'
    pairs = [(p, project / 'assets/visuals' / p.relative_to(assets))
             for p in _files(assets, origin) if p.suffix.lower() not in VIDEO_EXTENSIONS]
    refs = set(re.findall(r'assets/visuals/[^\s"\'`<>]+\.(?:mp4|webm|mov|mkv|avi|m4v)\b', text, re.IGNORECASE))
    new_items = {}
    if refs:
        videos = {i['destination']: i for i in manifest(origin) if 'destination' in i}
        for ref in sorted(refs):
            if ref not in videos:
                raise ValueError(f'Video is missing from the manifest: {ref}')
            destination = media_path(project, ref)
            if destination.is_file():
                continue  # Existing project footage, including personal edits, belongs to the project.
            if destination.exists():
                raise ValueError(f'Video destination is not a file: {destination}')
            pairs.append((verify(origin, videos[ref]), destination))
            new_items[destination] = videos[ref]
    return _copy(pairs, project, new_items)
