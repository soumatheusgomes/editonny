"""Exclusive revision ownership and validated local delivery; originals stay external."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

from common import content_hash, ensure_edit_dirs, now_iso, save_json
from project_contract import (digest, load_contract, read_json,
                              require_confirmation, settings_hash, source_identity_hash,
                              validate_confirmation, validate_contract)

_owned_locks = {}
_lock_guard = threading.Lock()


def edit_directory(project: Path, config: dict | None = None) -> Path:
    return (Path(project).resolve() / (config or load_contract(project))['edit']).resolve()


def managed_revision(project: Path) -> Path:
    """The `<job>/project/vNNN` directory owning this project; its job keeps one shared `exports/`."""
    revision = Path(project).resolve().parent
    if revision.parent.name != 'project' or not re.fullmatch(r'v\d{3,}', revision.name):
        raise ValueError('Only a managed revision can be revised or delivered')
    return revision


def selected_sources(manifest: dict, config: dict) -> dict:
    ids = config['workflow']['sourceIds']
    by_id = {s['id']: s for s in manifest.get('sources', [])}
    if any(i not in by_id for i in ids) or len(by_id) != len(manifest.get('sources', [])):
        raise ValueError('Selected sources do not match the source manifest')
    return {'sources': [by_id[i] for i in ids]}


def verify_sources(manifest: dict) -> None:
    for source in manifest.get('sources', []):
        path = Path(source['path'])
        if not path.is_file() or content_hash(path) != source['sha256']:
            raise ValueError(f'Source content changed or is unavailable: {path.name}; inspect and confirm again')


@contextmanager
def revision_lock(project: Path):
    edit = edit_directory(project)
    lock = edit / '.workflow.lock'
    owner = (os.getpid(), threading.get_ident(), lock)
    with _lock_guard:
        nested = owner in _owned_locks
        token = _owned_locks.get(owner) or {'pid': os.getpid(), 'token': uuid.uuid4().hex}
        if not nested:
            try:
                with lock.open('x', encoding='utf-8') as stream:
                    json.dump(token, stream)
            except FileExistsError as error:
                raise ValueError('Another workflow operation owns this revision; inspect status before retrying') from error
            _owned_locks[owner] = token
        elif read_json(lock) != token:
            raise ValueError('Revision ownership changed during this operation')
    try:
        yield
    finally:
        if not nested:
            with _lock_guard:
                if lock.exists() and read_json(lock) == token:
                    lock.unlink()
                _owned_locks.pop(owner, None)


def _manifest(project: Path, config: dict | None = None) -> dict:
    config = config or load_contract(project)
    return selected_sources(read_json(edit_directory(project, config) / 'source_manifest.json'), config)


@contextmanager
def managed_operation(edit: Path):
    edit = Path(edit).resolve()
    project = edit.parent / 'project'
    raw = read_json(project / 'project.json') if (project / 'project.json').is_file() else {}
    if raw.get('workflow') or (edit / 'source_manifest.json').is_file():
        with revision_lock(project):
            yield
    else:
        yield


def baseline_edl(edit: Path, manifest: dict) -> dict:
    path = Path(edit) / 'edl.json'
    if path.is_file():
        return read_json(path)
    timeline = Path(edit) / 'cut_timeline.json'
    if timeline.is_file():
        segments = read_json(timeline).get('segments', [])
        return {'sources': {str(i): s['source_path'] for i, s in enumerate(segments)}, 'ranges': [
            {'source': str(i), 'start': s.get('edl_start', s['src_start']), 'end': s.get('edl_end', s['src_end']),
             'crop': s.get('crop'), 'gain_db': s.get('gain_db', 0)} for i, s in enumerate(segments)]}
    return {'sources': {s['id']: s['path'] for s in manifest['sources']}, 'ranges': [
        {'source': s['id'], 'start': 0, 'end': s['facts']['duration']} for s in manifest['sources']]}


def record_confirmation(project: Path, confirmation: dict) -> dict:
    config = load_contract(project)
    manifest = _manifest(project, config)
    verify_sources(manifest)
    accepted = validate_confirmation(config, source_identity_hash(manifest), confirmation)
    with revision_lock(project):
        path = edit_directory(project, config) / 'state.json'
        state = read_json(path) if path.is_file() else {}
        parent = state.get('parentSettingsHash')
        if parent and accepted.get('parentSettingsHash') != parent:
            raise ValueError('Revision confirmation must identify the parent settings hash')
        state['settingsConfirmation'] = accepted
        state.pop('acceptedVariant', None)
        state.setdefault('confirmationHistory', []).append(accepted)
        state['phase'] = 'confirmed'
        state['project'] = '../project'
        save_json(path, state)
    return accepted


def create_workspace(parent: Path, title: str, inspection: dict, config: dict, confirmation: dict) -> Path:
    config = validate_contract(config)
    manifest = selected_sources(inspection, config)
    validate_confirmation(config, source_identity_hash(manifest), confirmation)
    verify_sources(manifest)
    parent = Path(parent).expanduser().resolve()
    if str(parent) != config['workflow']['delivery']['workspaceRoot']:
        raise ValueError('Workspace destination differs from the root accepted in the settings summary')
    # The caller must resolve local-vs-remote ownership before supplying this parent.
    probe = parent
    while not probe.exists():
        probe = probe.parent
    required = max(16 * 1024 * 1024, sum(Path(s['path']).stat().st_size for s in manifest['sources']) * 2)
    if shutil.disk_usage(probe).free < required:
        raise ValueError(f'Insufficient space for the proposed workspace (estimate: {required} bytes); choose storage explicitly')
    slug = re.sub(r'[^\w-]+', '-', title.lower(), flags=re.UNICODE).strip('-') or 'video'
    name = datetime.now().astimezone().strftime('%Y-%m-%d') + '-' + slug[:80]
    parent.mkdir(parents=True, exist_ok=True)
    for count in range(1, 10000):
        root = parent / (name if count == 1 else f'{name}-{count:02d}')
        try:
            root.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise ValueError('Could not reserve a unique workspace name')
    try:
        revision = root / 'project/v001'
        edit = ensure_edit_dirs(revision / 'edit')
        project = revision / 'project'
        engine = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(engine / 'project-template/build.py'),
                                 'new', str(project), '--edit', str(edit)], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or 'Project initialization failed')
        template = read_json(project / 'project.json')
        template.update(config)
        template['edit'] = '../edit'
        save_json(project / 'project.json', template)
        save_json(edit / 'source_manifest.json', manifest)
        save_json(edit / 'state.json', {'revision': 'v001', 'project': '../project', 'phase': 'awaiting_confirmation', 'artifacts': {}})
        (root / 'exports').mkdir()
        record_confirmation(project, confirmation)
        return project
    except BaseException:
        shutil.rmtree(root)
        raise


def _merge(original: dict, changes: dict) -> dict:
    result = copy.deepcopy(original)
    for key, value in changes.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def revise_workspace(project: Path, changes: dict, scope: str, variant: str | None = None) -> Path:
    project = Path(project).resolve()
    original = load_contract(project)
    manifest = _manifest(project, original)
    require_confirmation(project, source_identity_hash(manifest))
    verify_sources(manifest)
    if variant:
        from project_contract import resolve_variant
        config = resolve_variant(original, variant)
        if changes or scope != original['workflow']['editing']['scope']:
            raise ValueError('An accepted variant must be resolved without additional decision changes')
    else:
        config = _merge(original, changes)
        config['workflow']['editing']['scope'] = scope
    config = validate_contract(config)
    if config['workflow']['delivery']['workspaceRoot'] != original['workflow']['delivery']['workspaceRoot']:
        raise ValueError('Moving the workspace root requires a new confirmed initialization at that destination')
    revision = managed_revision(project)
    with revision_lock(project):
        for number in range(int(revision.name[1:]) + 1, 10000):
            destination = revision.parent / f'v{number:03d}'
            try:
                destination.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise ValueError('Could not reserve a new revision')
        try:
            transient = shutil.ignore_patterns('.workflow.lock', '.render.lock', 'preview_cache', 'renders', 'node_modules', '__pycache__')
            def ignore(directory, names):
                # Delivery records describe the parent's exports, not this revision's.
                return transient(directory, names) | ({'deliveries'} if Path(directory) == revision / 'edit' else set())
            for name in ('edit', 'project'):
                shutil.copytree(revision / name, destination / name, ignore=ignore)
            ensure_edit_dirs(destination / 'edit')
            save_json(destination / 'project/project.json', config)
            save_json(destination / 'edit/state.json', {'revision': destination.name, 'parentRevision': revision.name,
                      'parentSettingsHash': settings_hash(original), 'phase': 'awaiting_confirmation', 'project': '../project', 'artifacts': {}})
            if scope == 'intervals':
                parent_edl = baseline_edl(revision / 'edit', manifest)
                state_path = destination / 'edit/state.json'
                state = read_json(state_path)
                state.update(parentEdl=parent_edl, parentEdlHash=digest(parent_edl))
                save_json(state_path, state)
            if variant:
                state_path = destination / 'edit/state.json'
                state = read_json(state_path)
                state.update(acceptedVariant={'parentProject': str(project), 'name': variant}, phase='confirmed')
                save_json(state_path, state)
            if settings_hash(config) == settings_hash(original):
                state_path = destination / 'edit/state.json'
                inherited = read_json(state_path)
                inherited['settingsConfirmation'] = require_confirmation(project, source_identity_hash(manifest))
                inherited['phase'] = 'confirmed'
                save_json(state_path, inherited)
            for name in ('preview_approval.json', 'preview_marks.json'):
                (destination / 'edit' / name).unlink(missing_ok=True)
            return destination / 'project'
        except BaseException:
            shutil.rmtree(destination)
            raise


def read_status(project: Path) -> dict:
    config = load_contract(project)
    edit = edit_directory(project, config)
    state = read_json(edit / 'state.json')
    try:
        manifest = _manifest(project, config)
        verify_sources(manifest)
        require_confirmation(project, source_identity_hash(manifest))
        confirmation = 'confirmed'
        reason = None
    except (ValueError, OSError) as error:
        confirmation, reason = 'required', str(error)
    return {'project': str(Path(project).resolve()), 'revision': state.get('revision'), 'phase': state.get('phase'),
            'confirmation': confirmation, 'reason': reason, 'settings': config['workflow'],
            'operationLock': (edit / '.workflow.lock').exists(), 'artifacts': state.get('artifacts', {}),
            'dependencies': render_dependencies(project), 'sampleReview': state.get('sampleReview')}


def record_artifact(project: Path, kind: str, path: Path, dependencies: dict, checks: dict) -> None:
    with revision_lock(project):
        state_path = edit_directory(project) / 'state.json'
        state = read_json(state_path)
        state.setdefault('artifacts', {})[kind] = {'path': str(Path(path).resolve()), 'sha256': content_hash(path),
                                                'dependencies': dependencies, 'checks': checks, 'recordedAt': now_iso()}
        save_json(state_path, state)


def render_dependencies(project: Path) -> dict:
    config = load_contract(project)
    edit = edit_directory(project, config)
    dependencies = {'configurationHash': digest(config)}
    for name in ('cut.mp4', 'cut_timeline.json', 'edl.json', 'edit_data.json', 'source_manifest.json', 'captions.json', 'transcripts/cut.json'):
        path = edit / name
        dependencies[name] = content_hash(path) if path.is_file() else None
    engine = Path(config.get('skill', Path(__file__).resolve().parents[1]))
    dependencies['engineHash'] = digest({str(path.relative_to(engine)): content_hash(path)
        for base in (engine / 'helpers', engine / 'project-template') for path in sorted(base.rglob('*.py'))
        if '__pycache__' not in path.parts})
    from media_policy import probe_capabilities
    dependencies['ffmpeg'] = probe_capabilities().get('version')
    dependencies['renderer'] = content_hash(Path(project) / 'package.json')
    paths = [Path(project) / 'build.py', Path(project) / 'hyperframes.json']
    for directory in ('assets', 'compositions', 'tools'):
        paths += [p for p in (Path(project) / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    # External media explicitly named in the composition is an input too, not merely its path string.
    def referenced_files(value):
        if isinstance(value, dict):
            for v in value.values():
                referenced_files(v)
        elif isinstance(value, list):
            for v in value:
                referenced_files(v)
        elif isinstance(value, str) and '\x00' not in value and len(value) < 4096:
            for root in (Path(project), edit):
                path = root / value
                try:
                    if path.is_file():
                        paths.append(path.resolve())
                except OSError:
                    pass
    referenced_files(config)
    if (edit / 'edit_data.json').is_file():
        referenced_files(read_json(edit / 'edit_data.json'))
    dependencies['projectInputs'] = digest({str(p.resolve()): content_hash(p) for p in sorted(set(paths)) if p.is_file()})
    return dependencies


def record_review(project: Path, candidate: Path, evidence: dict, treatment: bool = False) -> dict:
    config = load_contract(project)
    manifest = _manifest(project, config)
    verify_sources(manifest)
    identity = source_identity_hash(manifest)
    require_confirmation(project, identity)
    reviewer = evidence.get('reviewer')
    if reviewer not in ('agent', 'user') or not evidence.get('notes') or evidence.get('findings') != []:
        raise ValueError('Review needs an identified reviewer, inspection notes and no unresolved findings')
    if config['workflow']['review']['sample'] == 'user' and reviewer != 'user':
        raise ValueError('This project requires the user to approve its sample')
    if reviewer == 'user' or treatment:
        validate_confirmation(config, identity, evidence.get('confirmation'))
    if treatment and config['workflow']['color']['mode'] == 'preserve' and not (config.get('scenes') or config.get('animations') or config['workflow']['video']['fit'] != 'preserve'):
        raise ValueError('A transformed reference cannot authorize an unrequested look change')
    candidate = Path(candidate).resolve()
    candidate_hash = content_hash(candidate)
    from common import probe_video
    import math
    duration = probe_video(candidate)['duration']
    intervals = evidence.get('intervals', [])
    if not intervals or any(not isinstance(i, list) or len(i) != 2 or any(isinstance(t, bool) or not isinstance(t, (float, int)) or not math.isfinite(t) for t in i) or not 0 <= i[0] < i[1] <= duration + 0.001 for i in intervals):
        raise ValueError('Review must identify finite inspected intervals within the actual candidate')
    timeline = read_json(edit_directory(project) / 'cut_timeline.json')
    full_duration = timeline.get('duration')
    if isinstance(full_duration, bool) or not isinstance(full_duration, (int, float)) or not math.isfinite(full_duration) or full_duration <= 0:
        raise ValueError('Review requires a measured complete cut duration')
    covered, stop = 0.0, 0.0
    for start, end in sorted(intervals):
        covered += max(0, end - max(start, stop))
        stop = max(stop, end)
    if covered < min(8, full_duration) - 0.001:
        raise ValueError('Review a representative 8–15 second sample, or the complete shorter clip')
    if evidence.get('artifactHash') != candidate_hash or evidence.get('dependencies') != render_dependencies(project):
        raise ValueError('Review is stale: candidate or composition dependencies changed')
    record = {**evidence, 'path': str(candidate), 'sha256': candidate_hash, 'recordedAt': now_iso()}
    with revision_lock(project):
        path = edit_directory(project) / 'state.json'
        state = read_json(path)
        state['approvedReference' if treatment else 'sampleReview'] = record
        save_json(path, state)
    return record


def seed_source(project: Path) -> Path:
    """Initialize a narrowly scoped job without making editorial cuts or changing stream samples."""
    config = load_contract(project)
    manifest = _manifest(project, config)
    verify_sources(manifest)
    require_confirmation(project, source_identity_hash(manifest))
    if len(manifest['sources']) != 1:
        raise ValueError('An unchanged base needs one source; multiple takes need an explicitly scoped assembly')
    source = manifest['sources'][0]
    facts = source['facts']
    edit = edit_directory(project)
    output = edit / 'cut.mp4'
    with revision_lock(project):
        if output.exists() or (edit / 'cut_timeline.json').exists():
            raise ValueError('A cut already exists; preserve it or create another revision')
        with tempfile.TemporaryDirectory(prefix='seed-', dir=edit) as tmp:
            candidate = Path(tmp) / 'cut.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-i', source['path'], '-map', '0:v:0', '-map', '0:a?',
                '-c', 'copy', '-movflags', '+faststart', str(candidate)], check=True, capture_output=True)
            verify_sources(manifest)
            os.link(candidate, output)
        segment = {'id': 'original', 'source': source['id'], 'source_path': source['path'], 'src_start': 0,
                   'src_end': facts['duration'], 'out_start': 0, 'out_end': facts['duration']}
        if config['workflow']['video']['timing'] == 'vfr':
            from render_vfr import frame_times
            segment['sourceFrameTimes'] = segment['outputFrameTimes'] = frame_times(output)
        save_json(edit / 'cut_timeline.json', {'duration': facts['duration'], 'width': facts['display_width'],
                  'height': facts['display_height'], 'fps': facts['fps'], 'fps_str': facts['fps_str'],
                  'contractHash': settings_hash(config), 'segments': [segment]})
    return output


def recover_revision(project: Path) -> dict:
    """Remove only an abandoned operation lock, never running processes or media."""
    lock = edit_directory(project) / '.workflow.lock'
    if lock.exists():
        if os.name != 'posix':
            raise ValueError('Automatic lock recovery is POSIX-only; on Windows verify the owner has stopped before manually removing .workflow.lock')
        value = read_json(lock)
        pid = value.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError('Lock owner is unknown; inspect the lock before recovery')
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            if read_json(lock) == value:
                lock.unlink()
        else:
            raise ValueError('Lock owner is still alive; recovery will not interrupt it')
    return read_status(project)


def publish_export(project: Path, candidate: Path, verification: dict, variant: str = 'final') -> dict:
    project, candidate = Path(project).resolve(), Path(candidate).resolve()
    config = load_contract(project)
    manifest = _manifest(project, config)
    verify_sources(manifest)
    identity = source_identity_hash(manifest)
    require_confirmation(project, identity)
    checks = verification.get('checks', [])
    if verification.get('status') != 'passed' or not checks or any(c.get('required', True) and c.get('status') != 'passed' for c in checks):
        raise ValueError('Only a candidate with all required checks passed can be delivered')
    expected = content_hash(candidate)
    if verification.get('artifactHash') != expected or verification.get('settingsHash') != settings_hash(config) or verification.get('sourceIdentityHash') != identity:
        raise ValueError('Verification does not identify the current candidate, sources and settings')
    if not re.fullmatch(r'[\w-]+', variant):
        raise ValueError('Variant name must be a safe identifier')
    accepted_variant = read_json(edit_directory(project) / 'state.json').get('acceptedVariant', {}).get('name')
    if variant != 'final' and variant != accepted_variant:
        raise ValueError('Resolve the accepted variant into its own revision before rendering and exporting')
    variant = accepted_variant or variant
    if candidate.suffix.lower() != '.mp4':
        raise ValueError('Confirmed MP4 delivery requires an .mp4 candidate')
    from fidelity import verify_media
    fresh = verify_media(candidate, project)
    if fresh['status'] != 'passed':
        raise ValueError('Candidate failed final delivery verification')
    owner = managed_revision(project)
    destination = owner.parent.parent / 'exports'
    # Manifests stay with the revision so exports/ holds only what the person uses.
    records = owner / 'edit/deliveries'
    for folder in (destination, records):
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError('Export destination layout changed through a symlink; restore the confirmed job directory')
        folder.mkdir(exist_ok=True)
    revision = owner.name
    stem = f"{config['workflow']['delivery']['basename']}-{revision}-{variant}"
    with revision_lock(project):
        for number in range(1, 10000):
            name = stem if number == 1 else f'{stem}-{number:02d}'
            output = destination / f'{name}{candidate.suffix}'
            sidecar = records / f'{name}.json'
            subtitle_outputs = {fmt: destination / f'{name}.{fmt}' for fmt in config['workflow']['delivery']['subtitleFormats']}
            if any(p.exists() for p in (output, sidecar, *subtitle_outputs.values())):
                continue
            staging = Path(tempfile.mkdtemp(prefix='.export-', dir=destination))
            staged = staging / ('candidate' + candidate.suffix)
            owned = []
            try:
                with staged.open('xb') as stream, candidate.open('rb') as source:
                    shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if content_hash(staged) != expected:
                    raise ValueError('Candidate changed during publication')
                bundle = [(staged, output)]
                subtitles = []
                from export_subtitles import export_subtitles
                for fmt, final in subtitle_outputs.items():
                    path = staged.with_suffix('.' + fmt)
                    export_subtitles(project, fmt, path)
                    bundle.append((path, final))
                    subtitles.append({'path': str(final), 'sha256': content_hash(path)})
                if render_dependencies(project) != fresh['dependencies']:
                    raise ValueError('Composition changed after verification; no export was published')
                verify_sources(manifest)
                report = {'schemaVersion': 1, 'revision': revision, 'artifact': str(output), 'sha256': expected,
                          'size': staged.stat().st_size, 'settingsHash': settings_hash(config), 'sourceIdentityHash': identity,
                          'variant': variant, 'subtitles': subtitles, 'verification': fresh, 'deliveredAt': now_iso()}
                staged_report = staged.with_suffix('.json')
                with staged_report.open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                bundle.append((staged_report, sidecar))
                try:
                    for source, final in bundle:
                        os.link(source, final)
                        owned.append((source, final))
                except FileExistsError:
                    for source, final in reversed(owned):
                        if final.exists() and os.path.samefile(source, final):
                            final.unlink()
                    owned = []
                    continue
                return report
            except BaseException:
                for source, final in reversed(owned):
                    if final.exists() and os.path.samefile(source, final):
                        final.unlink()
                raise
            finally:
                shutil.rmtree(staging)
    raise ValueError('Could not reserve a unique final export')
