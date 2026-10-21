"""Check actual delivery bytes and corresponding source pixels, never metadata alone."""
from __future__ import annotations

from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import tempfile

from common import content_hash, probe_video
from media_policy import COLOR_FIELDS, UNKNOWN, plan_media
from project_contract import (load_contract, read_json, require_confirmation,
                              settings_hash, source_identity_hash)


def _shown_vfr_time(path: Path, at: float) -> float:
    """Return the presentation time, relative to the stream start, of the frame on screen at `at`."""
    process = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-read_intervals',
                              f'{max(0, at - 2):.6f}%{at + 0.001:.6f}', '-show_entries',
                              'format=start_time:frame=best_effort_timestamp_time', '-of', 'json', str(path)],
                             capture_output=True, text=True, timeout=60, check=True)
    data = json.loads(process.stdout)
    start = float(data.get('format', {}).get('start_time') or 0)
    shown = [float(f['best_effort_timestamp_time']) - start for f in data.get('frames', [])
             if f.get('best_effort_timestamp_time') not in (None, 'N/A')]
    shown = [t for t in shown if t <= at + 1e-6]
    return max(shown) if shown else at


def _frame(path: Path, at: float, width: int = 192, interpretation: dict | None = None):
    import numpy as np
    facts = probe_video(path)
    # Seek the frame on screen, not the following frame (important for short clips and cut edges).
    rate = float(Fraction(facts['fps_str']))
    if not facts.get('is_vfr'):
        at = math.floor(max(0, at) * rate + 1e-7) / rate
    else:
        # -ss returns the first frame at or after `at`; snap to the last presented timestamp instead.
        at = _shown_vfr_time(path, max(0, at))
    at = max(0, at - min(1e-6, float(Fraction(facts['time_base'])) / 4))
    height = max(2, round(width * facts['display_height'] / facts['display_width']))
    from media_policy import frame_tags
    filters = ([frame_tags(interpretation)] if interpretation else []) + [f'scale={width}:{height}:flags=area,format=rgb48le']
    process = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', str(path),
                              '-frames:v', '1', '-vf', ','.join(filters),
                              '-f', 'rawvideo', '-'], capture_output=True, timeout=60)
    if process.returncode or len(process.stdout) != width * height * 6:
        raise ValueError(f'Could not decode reference frame: {path.name} at {at:g}s')
    return np.frombuffer(process.stdout, dtype='<u2').reshape(height, width, 3).astype(float) / 65535


def _linear(rgb, transfer: str):
    import numpy as np
    if transfer == 'smpte2084':
        m1, m2, c1, c2, c3 = 2610 / 16384, 2523 / 32, 3424 / 4096, 2413 / 128, 2392 / 128
        v = np.maximum(rgb, 0) ** (1 / m2)
        return 10000 * (np.maximum(v - c1, 0) / np.maximum(c2 - c3 * v, 1e-12)) ** (1 / m1)
    if transfer == 'arib-std-b67':
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        scene = np.where(rgb <= 0.5, rgb * rgb / 3, (np.exp((rgb - c) / a) + b) / 12)
        luminance = scene @ np.array([0.2627, 0.6780, 0.0593])
        # Declared HLG reference: 1000-nit display, zero black, system gamma 1.2.
        return 1000 * scene * np.maximum(luminance[..., None], 0) ** 0.2
    if transfer == 'iec61966-2-1':
        return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return np.where(rgb < 0.081, rgb / 4.5, ((rgb + 0.099) / 1.099) ** (1 / 0.45))


def _lab(linear, primaries='bt709'):
    import numpy as np
    matrices = {'bt709': [[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]],
                'smpte170m': [[0.3935209, 0.3652581, 0.1916769], [0.2123764, 0.7010599, 0.0865638], [0.0187391, 0.1119339, 0.9583847]],
                'bt470bg': [[0.430619, 0.3415419, 0.1783091], [0.2220379, 0.7066384, 0.0713236], [0.0201853, 0.1295504, 0.9390944]],
                'bt2020': [[0.636958, 0.1446169, 0.168881], [0.2627002, 0.6779981, 0.0593017], [0, 0.0280727, 1.0609851]]}
    if primaries not in matrices:
        raise ValueError('Unknown/unsupported primaries; fidelity cannot assume BT.709')
    xyz = linear @ np.array(matrices[primaries]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])), axis=-1)


def delta_e_2000(first, second):
    """CIEDE2000 for reference-patch tests and declared BT.709/sRGB SDR comparisons."""
    import numpy as np
    l1, a1, b1 = np.moveaxis(first, -1, 0)
    l2, a2, b2 = np.moveaxis(second, -1, 0)
    c = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(c ** 7 / (c ** 7 + 25 ** 7)))
    a1, a2 = (1 + g) * a1, (1 + g) * a2
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    h1, h2 = np.mod(np.arctan2(b1, a1), 2 * np.pi), np.mod(np.arctan2(b2, a2), 2 * np.pi)
    dl, dc = l2 - l1, c2 - c1
    dh = h2 - h1
    dh = np.where(dh > np.pi, dh - 2 * np.pi, np.where(dh < -np.pi, dh + 2 * np.pi, dh))
    dh = np.where(c1 * c2 == 0, 0, dh)
    d_h = 2 * np.sqrt(c1 * c2) * np.sin(dh / 2)
    mean_l, mean_c = (l1 + l2) / 2, (c1 + c2) / 2
    mean_h = (h1 + h2) / 2
    mean_h = np.where(np.abs(h1 - h2) > np.pi, mean_h + np.where(h1 + h2 < 2 * np.pi, np.pi, -np.pi), mean_h)
    mean_h = np.where(c1 * c2 == 0, h1 + h2, mean_h)
    t = 1 - 0.17 * np.cos(mean_h - np.pi / 6) + 0.24 * np.cos(2 * mean_h) + 0.32 * np.cos(3 * mean_h + np.pi / 30) - 0.20 * np.cos(4 * mean_h - 63 * np.pi / 180)
    sl = 1 + 0.015 * (mean_l - 50) ** 2 / np.sqrt(20 + (mean_l - 50) ** 2)
    sc, sh = 1 + 0.045 * mean_c, 1 + 0.015 * mean_c * t
    rc = 2 * np.sqrt(mean_c ** 7 / (mean_c ** 7 + 25 ** 7))
    rt = -rc * np.sin(np.pi / 3 * np.exp(-((mean_h * 180 / np.pi - 275) / 25) ** 2))
    return np.sqrt(np.maximum(0, (dl / sl) ** 2 + (dc / sc) ** 2 + (d_h / sh) ** 2 + rt * (dc / sc) * (d_h / sh)))


def _itp(linear_nits):
    import numpy as np
    lms = linear_nits @ (np.array([[1688, 683, 99], [2146, 2951, 309], [262, 462, 3688]]) / 4096)
    value = np.maximum(lms / 10000, 0) ** (2610 / 16384)
    pq = ((3424 / 4096 + 2413 / 128 * value) / (1 + 2392 / 128 * value)) ** (2523 / 32)
    return pq @ (np.array([[2048, 6610, 17933], [2048, -13613, -17390], [0, 7003, -543]]) / 4096)


def measure_fidelity(reference: dict, candidate: Path, intervals: list[dict]) -> dict:
    import numpy as np
    samples = []
    for interval in intervals:
        source = Path(interval['source'])
        expected = _frame(source, interval['sourceTime'], interpretation=reference.get('inputOverride'))
        actual = _frame(candidate, interval['outputTime'])
        if expected.shape != actual.shape:
            raise ValueError('Image geometry changed; a declared geometric reference is required')
        mask = np.ones(expected.shape[:2], dtype=bool)
        active = [m['box'] for m in reference.get('overlayMasks', []) if m['start'] <= interval['outputTime'] < m['end']]
        for x0, y0, x1, y1 in reference.get('masks', []) + active:
            h, w = mask.shape
            mask[round(y0 * h):round(y1 * h), round(x0 * w):round(x1 * w)] = False
        if not mask.any():
            raise ValueError('No unaffected image area remains to compare')
        transfer = reference.get('transfer')
        if transfer not in ('bt709', 'iec61966-2-1', 'smpte170m', 'bt2020-10', 'bt2020-12', 'smpte2084', 'arib-std-b67'):
            raise ValueError('Unknown/unsupported transfer: preserve bytes or confirm an explicit source interpretation; fidelity cannot be certified by guessing')
        linear_a, linear_b = _linear(expected, transfer), _linear(actual, transfer)
        hdr = transfer in ('smpte2084', 'arib-std-b67')
        weights = np.array([0.2627, 0.6780, 0.0593]) if hdr else np.array([0.2126, 0.7152, 0.0722])
        ya, yb = linear_a @ weights, linear_b @ weights
        if hdr:
            difference = _itp(linear_a) - _itp(linear_b)
            error = 720 * np.sqrt(difference[..., 0] ** 2 + 0.25 * difference[..., 1] ** 2 + difference[..., 2] ** 2)
            allowed = np.where(ya >= 1, ya * 0.02, 0.02)
            luminance_ok = float(np.mean(np.abs(ya[mask] - yb[mask]) > allowed[mask])) <= 0.05
        else:
            primaries = reference.get('primaries', 'bt709')
            error = delta_e_2000(_lab(linear_a, primaries), _lab(linear_b, primaries))
            luminance_ok = float(np.percentile(np.abs(ya[mask] - yb[mask]), 95)) <= 0.01
        median, p95 = float(np.median(error[mask])), float(np.percentile(error[mask], 95))
        samples.append({'sourceTime': interval['sourceTime'], 'outputTime': interval['outputTime'],
                        'metric': 'deltaEITP' if hdr else 'deltaE00', 'median': median, 'p95': p95,
                        'luminancePassed': luminance_ok, 'passed': median <= 1 and p95 <= (3 if hdr else 2) and luminance_ok})
    return {'status': 'passed' if samples and all(s['passed'] for s in samples) else 'failed', 'samples': samples,
            'scope': 'Sampled corresponding frames outside declared overlay masks; not universal coverage'}


def verify_media(candidate: Path, project: Path, reference: dict | None = None) -> dict:
    candidate, project = Path(candidate), Path(project)
    config = load_contract(project)
    edit = (project / config['edit']).resolve()
    manifest = read_json(edit / 'source_manifest.json')
    from workspace import selected_sources, verify_sources
    manifest = selected_sources(manifest, config)
    verify_sources(manifest)
    identity = source_identity_hash(manifest)
    require_confirmation(project, identity)
    original_hash = content_hash(candidate)
    from workspace import render_dependencies
    dependencies = render_dependencies(project)
    state = read_json(edit / 'state.json')
    checks = []
    def check(name, passed, detail, required=True):
        checks.append({'name': name, 'status': 'passed' if passed else 'failed', 'required': required, 'detail': detail})
    review = state.get('sampleReview', {})
    review_path = Path(review.get('path', ''))
    check('sampleReview', bool(review.get('findings') == [] and review.get('dependencies') == dependencies and
          review_path.is_file() and content_hash(review_path) == review.get('sha256')), 'A reviewed sample must match the current composition and bytes')
    facts = probe_video(candidate)
    audio_policy = config['workflow']['audio']
    expected_audio = any(s['facts'].get('has_audio') for s in manifest['sources']) or audio_policy['music'] or audio_policy['effects']
    check('audioPresence', bool(facts.get('has_audio')) == expected_audio,
          {'expected': expected_audio, 'actual': facts.get('has_audio')})
    if expected_audio and facts.get('has_audio') and audio_policy['mode'] == 'preserve' and not audio_policy['music'] and not audio_policy['effects'] and audio_policy['denoise'] == 'off':
        base = edit / 'cut.mp4'
        base = base if base.is_file() else Path(manifest['sources'][0]['path'])
        base_facts = probe_video(base)
        def audio_hash(path):
            process = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a', '-c:a', 'copy',
                                      '-f', 'hash', '-hash', 'sha256', '-'], capture_output=True, text=True, timeout=600)
            if process.returncode:
                raise ValueError('Could not verify preserved audio stream bytes: ' + process.stderr[-300:])
            return process.stdout.strip()
        check('preservedAudio', audio_hash(base) == audio_hash(candidate) and
              facts['audio_tracks'] == base_facts['audio_tracks'] and facts['audio_channels'] == base_facts['audio_channels'] and
              facts['sample_rate'] == base_facts['sample_rate'] and
              abs(facts['audio_start_time'] - base_facts['audio_start_time']) <= 1 / max(1, base_facts['sample_rate']),
              'Unchanged audio stream payloads, tracks, rate, channels and start time must match the approved cut')
    video = config['workflow']['video']
    check('geometry', (facts.get('display_width'), facts.get('display_height')) == (video['width'], video['height']),
          {'actual': [facts.get('display_width'), facts.get('display_height')], 'expected': [video['width'], video['height']]})
    if video['timing'] == 'cfr':
        check('frameRate', Fraction(facts['fps_str']) == Fraction(video['frameRate']), facts['fps_str'])
    decode = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(candidate), '-map', '0:v:0', '-map', '0:a?',
                             '-f', 'null', '-'], capture_output=True, text=True, timeout=600)
    check('completeDecode', decode.returncode == 0, decode.stderr[-500:])
    expected = plan_media([s['facts'] for s in manifest['sources']], config, None, 'delivery')
    check('colorMetadata', all(expected.get(k) in UNKNOWN or facts.get(k) == expected[k] for k in COLOR_FIELDS),
          {k: {'expected': expected.get(k), 'actual': facts.get(k)} for k in COLOR_FIELDS})
    check('precision', facts.get('bit_depth', 0) >= expected.get('bit_depth', 8), facts.get('pix_fmt'))
    def chroma(info):
        return next((c for c in ('420', '422', '444') if c in info.get('pix_fmt', '')), None)
    check('chromaPrecision', chroma(facts) is not None and chroma(facts) == chroma(expected),
          {'expected': expected.get('pix_fmt'), 'actual': facts.get('pix_fmt')})
    codec = config['workflow']['encoding']['videoCodec']
    wanted_codec = manifest['sources'][0]['facts'].get('codec') if codec == 'source' else codec
    if wanted_codec == 'auto':
        wanted_codec = 'h264' if expected.get('bit_depth', 8) == 8 else 'hevc'
    check('encoding', facts.get('codec') in ('h264', 'hevc') if codec == 'auto' else facts.get('codec') == wanted_codec,
          {'expected': 'automatic H.264/HEVC' if codec == 'auto' else wanted_codec, 'actual': facts.get('codec')})
    timeline_path = edit / 'cut_timeline.json'
    timeline = read_json(timeline_path) if timeline_path.is_file() else {}
    try:
        from render_vfr import frame_times
        actual_times = frame_times(candidate)
        hold = float(config.get('endHold', 0)) + (float(config.get('endCard', {}).get('seconds', 0)) if config.get('endCard', {}).get('enabled') else 0)
        if not timeline.get('duration'):
            raise ValueError('A current timeline is required to verify presentation timestamps')
        if video['timing'] == 'vfr':
            if hold:
                raise ValueError('VFR end holds need an explicitly validated timing route')
            if not timeline.get('segments') or any(not s.get('outputFrameTimes') for s in timeline['segments']):
                raise ValueError('VFR verification requires observed source-to-output frame timestamps')
            expected_times = [float(t) for s in timeline['segments'] for t in s['outputFrameTimes']]
        else:
            rate = Fraction(video['frameRate'])
            expected_times = [float(i / rate) for i in range(round((float(timeline['duration']) + hold) * rate))]
        tolerance = float(Fraction(facts['time_base'])) + 1e-9
        passed = len(actual_times) == len(expected_times) and all(abs(a - b) <= tolerance for a, b in zip(actual_times, expected_times))
        check('presentationTiming', passed, {'actualFrames': len(actual_times), 'expectedFrames': len(expected_times), 'toleranceSeconds': tolerance})
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        checks.append({'name': 'presentationTiming', 'status': 'blocked', 'required': True, 'detail': str(error)})
    if timeline.get('duration'):
        hold = float(config.get('endHold', 0)) + (float(config.get('endCard', {}).get('seconds', 0)) if config.get('endCard', {}).get('enabled') else 0)
        check('duration', abs(facts['duration'] - float(timeline['duration']) - hold) <= max(0.05, 1 / float(Fraction(facts['fps_str']))), facts['duration'])
    policy = config['workflow']['color']
    try:
        approved = state.get('approvedReference', {})
        if reference is None and approved:
            path = Path(approved['path'])
            if approved.get('dependencies') != dependencies or content_hash(path) != approved['sha256']:
                raise ValueError('Approved treatment reference is stale')
            reference = {'transfer': probe_video(path).get('color_transfer'), 'intervals': [
                {'source': str(path), 'sourceTime': facts['duration'] * f, 'outputTime': facts['duration'] * f}
                for f in (0.1, 0.25, 0.5, 0.75, 0.9)]}
        if policy['mode'] != 'preserve' and reference is None:
            raise ValueError('Requested image treatment needs a measured approved transformed reference')
        if config.get('scenes') or config.get('animations') or any(b.get('zoom', 1) != 1 or b.get('scene', 'fullscreen') != 'fullscreen' for b in config.get('blocks', [])):
            if reference is None:
                raise ValueError('Requested geometry/scenes need explicit corresponding reference intervals and masks')
        segments = timeline.get('segments', [])
        selected_paths = {str(Path(s['path']).resolve()) for s in manifest['sources']}
        for segment in segments:
            paths = [segment.get('source_path'), segment.get('picture_path') or segment.get('source_path')]
            if any(not path or str(Path(path).resolve()) not in selected_paths for path in paths):
                raise ValueError('Timeline references footage outside the confirmed selection')
        intervals = []
        for segment in segments[:8]:
            midpoint = (float(segment['out_start']) + float(segment['out_end'])) / 2
            source = segment.get('picture_path') or segment.get('source_path')
            if not source:
                raise ValueError('Timeline does not identify source-picture correspondence')
            source_time = float(segment.get('picture_src_start', segment.get('src_start', 0))) + midpoint - float(segment['out_start'])
            if segment.get('sourceFrameTimes'):
                index = len(segment['sourceFrameTimes']) // 2
                source_time, midpoint = segment['sourceFrameTimes'][index], segment['outputFrameTimes'][index]
            intervals.append({'source': source, 'sourceTime': source_time, 'outputTime': midpoint})
        if not intervals and reference is None:
            raise ValueError('A current source-to-output timeline is required for appearance verification')
        options = reference or {'transfer': expected.get('color_transfer'), 'primaries': expected.get('color_primaries'),
                                'inputOverride': expected.get('inputOverride'), 'masks': []}
        if reference is None and config.get('captions', {}).get('enabled'):
            evidence = state.get('artifacts', {}).get('render:' + original_hash, {})
            if evidence.get('dependencies') != render_dependencies(project) or evidence.get('sha256') != original_hash:
                raise ValueError('Captions need current render evidence with measured overlay bounds; no guessed area may be excluded')
            options['overlayMasks'] = evidence.get('checks', {}).get('overlayMasks', [])
            if not options['overlayMasks']:
                raise ValueError('Enabled captions have no measured on-screen bounds')
        metrics = measure_fidelity(options, candidate, options.get('intervals', intervals))
        check('appearance', metrics['status'] == 'passed', metrics)
    except (ValueError, RuntimeError, subprocess.SubprocessError) as error:
        checks.append({'name': 'appearance', 'status': 'blocked', 'required': True, 'detail': str(error)})
    if config.get('captions', {}).get('enabled'):
        try:
            from project_contract import validate_caption_language
            validate_caption_language(config, read_json(edit / 'transcripts/cut.json'))
            from quality_gate import text_outside
            text = text_outside(candidate, video['width'], video['height'], 3)
            if text['reader'] is None:
                checks.append({'name': 'captionLayout', 'status': 'not_checked', 'required': True, 'detail': 'No local text reader is available'})
            else:
                check('captionLayout', not text['findings'], text)
                check('captionTextPresent', text.get('recognized', 0) > 0, 'Actual rendered text was recognized locally')
        except (ValueError, RuntimeError) as error:
            checks.append({'name': 'captionLayout', 'status': 'blocked', 'required': True, 'detail': str(error)})
    if config['workflow']['audio']['mode'] == 'normalize' and any(s['facts'].get('has_audio') for s in manifest['sources']):
        try:
            from quality_gate import volume
            measured = volume(candidate)
            audio = config['workflow']['audio']
            check('audio', abs(measured['lufs'] - audio['targetLufs']) <= 1.5 and measured['peak_db'] <= audio['truePeakLimit'], measured)
        except (ValueError, RuntimeError) as error:
            checks.append({'name': 'audio', 'status': 'blocked', 'required': True, 'detail': str(error)})
    check('stableCandidate', content_hash(candidate) == original_hash, 'Candidate bytes unchanged during verification')
    verify_sources(manifest)
    check('stableDependencies', render_dependencies(project) == dependencies, 'Composition dependencies unchanged during verification')
    failed = any(c['status'] == 'failed' for c in checks if c['required'])
    blocked = any(c['status'] in ('blocked', 'not_checked') for c in checks if c['required'])
    return {'status': 'failed' if failed else 'blocked' if blocked else 'passed', 'checks': checks,
            'artifactHash': original_hash, 'settingsHash': settings_hash(config), 'sourceIdentityHash': identity,
            'actual': facts, 'dependencies': dependencies}


def probe_media_route() -> dict:
    """Explicit short local probe; basic doctor never invokes this."""
    with tempfile.TemporaryDirectory(prefix='editonny-media-probe-') as tmp:
        video = Path(tmp) / 'probe.mp4'
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:s=64x64:r=24:d=0.1',
                                 '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-color_range', 'tv', '-colorspace', 'bt709', str(video)],
                                capture_output=True, text=True, timeout=30)
        facts = probe_video(video) if result.returncode == 0 else {}
        return {'status': 'passed' if facts.get('color_space') == 'bt709' and facts.get('color_range') == 'tv' else 'failed',
                'scope': 'Short SDR encode/probe only; not HDR, overlay, playback or universal fidelity certification', 'actual': facts}
