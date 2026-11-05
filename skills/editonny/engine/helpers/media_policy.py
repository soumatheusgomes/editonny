"""Explicit image interpretation and encoder choices shared by media boundaries."""
from __future__ import annotations

import copy
from functools import lru_cache
import re
import shutil
import subprocess

COLOR_FIELDS = ('color_primaries', 'color_transfer', 'color_space', 'color_range')
COLOR_OPTIONS = {'color_primaries': '-color_primaries', 'color_transfer': '-color_trc',
                 'color_space': '-colorspace', 'color_range': '-color_range'}
PARAM_OPTIONS = {'color_primaries': 'color_primaries', 'color_transfer': 'color_trc',
                 'color_space': 'colorspace', 'color_range': 'range'}
UNKNOWN = ('', None, 'unknown', 'unspecified', 'reserved')


def color_tags(info: dict) -> list[str]:
    result = []
    for field, option in COLOR_OPTIONS.items():
        if info.get(field) not in UNKNOWN:
            result += [option, str(info[field])]
    return result


def frame_tags(info: dict) -> str:
    parts = [f'{PARAM_OPTIONS[k]}={info[k]}' for k in COLOR_FIELDS if info.get(k) not in UNKNOWN]
    return 'setparams=' + ':'.join(parts) if parts else ''


def preserve_scale(info: dict) -> str:
    options = []
    matrix = info.get('color_space')
    if matrix not in UNKNOWN and matrix not in ('rgb', 'gbr'):
        options += [f'in_color_matrix={matrix}', f'out_color_matrix={matrix}']
    color_range = info.get('color_range')
    if color_range in ('pc', 'tv'):
        options += [f'in_range={color_range}', f'out_range={color_range}']
    return ':' + ':'.join(options) if options else ''


@lru_cache(maxsize=1)
def probe_capabilities() -> dict:
    result = {'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe'), 'filters': [], 'encoders': []}
    if not result['ffmpeg']:
        return result
    version = subprocess.run([result['ffmpeg'], '-version'], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
    result['version'] = version.stdout.splitlines()[0] if version.returncode == 0 and version.stdout else 'unavailable'
    for key in ('filters', 'encoders'):
        process = subprocess.run([result['ffmpeg'], '-hide_banner', '-' + key], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        if process.returncode:
            raise ValueError(f'Could not inspect FFmpeg {key}: {process.stderr[-300:]}')
        rows = [line.split() for line in process.stdout.splitlines()]
        result[key] = [p[1] for p in rows if len(p) >= 3 and p[1] != '=' and
                       (bool(re.fullmatch(r'[AVN|]+->[AVN|]+', p[2])) if key == 'filters' else bool(re.fullmatch(r'[VAS][A-Z.]{5}', p[0])))]
    return result


def encoder_args(info: dict, available: dict | None = None, quality: str | int | None = None) -> list[str]:
    available = available if available is not None else probe_capabilities()
    pix_fmt = info.get('pix_fmt') or 'yuv420p'
    if pix_fmt == 'yuvj420p':
        pix_fmt = 'yuv420p'
    if pix_fmt not in ('yuv420p', 'yuv420p10le', 'yuv420p12le', 'yuv422p', 'yuv422p10le', 'yuv444p', 'yuv444p10le'):
        raise ValueError(f'No validated preservation encoder for {pix_fmt}; select an explicit tested conversion')
    codec = info.get('encoder') or ('libx264' if pix_fmt in ('yuv420p', 'yuv422p', 'yuv444p') else 'libx265')
    if codec not in available.get('encoders', []):
        raise ValueError(f'{codec} is required for source precision; no automatic bit-depth reduction is allowed')
    if quality is None:
        quality = 16 if info.get('is_hdr') else 18
    result = ['-c:v', codec, '-pix_fmt', pix_fmt, '-crf', str(quality)]
    if codec == 'libx265':
        result += ['-tag:v', 'hvc1', '-x265-params', 'log-level=error']
    return result + color_tags(info)


def plan_media(source_facts: list[dict], contract: dict | None, capabilities: dict | None, purpose: str) -> dict:
    if not source_facts:
        raise ValueError('Media planning requires observed video sources')
    if purpose in ('master', 'composition') and (contract or {}).get('workflow', {}).get('audio') and any(f.get('audio_tracks', 0) > 1 for f in source_facts):
        raise ValueError('Multi-track source editing is not implemented; no source audio track may be silently discarded')
    capabilities = capabilities if capabilities is not None else probe_capabilities()
    output = copy.deepcopy(source_facts[0])
    if any(f.get('sample_aspect_ratio') not in (None, '', '1:1', '0:1') for f in source_facts):
        raise ValueError('Non-square source pixels need an explicit validated geometric interpretation; do not silently stretch them')
    policy = (contract or {}).get('workflow', {}).get('color', {'mode': 'preserve', 'target': 'source'})
    interpretation = policy.get('inputOverride') or {}
    if interpretation:
        if policy.get('mode') == 'preserve' and any(f.get(k) not in UNKNOWN and f.get(k) != value for f in source_facts for k, value in interpretation.items()):
            raise ValueError('Overriding known source metadata is a requested color reinterpretation; confirm it in convert mode')
        source_facts = [{**f, **interpretation} for f in source_facts]
        output.update(interpretation)
        output['is_hdr'] = output.get('color_transfer') in ('smpte2084', 'arib-std-b67')
    profiles = {tuple(info.get(k) for k in (*COLOR_FIELDS, 'pix_fmt', 'bit_depth')) for info in source_facts}
    if len(profiles) > 1 and policy.get('mode', 'preserve') == 'preserve':
        raise ValueError('Mixed source color/precision profiles require a confirmed common target before concatenation')
    for facts in source_facts:
        if any('dovi' in str(side).lower() or 'dolby' in str(side).lower() for side in facts.get('side_data', [])):
            raise ValueError('Dynamic Dolby metadata needs an explicitly validated conversion; preservation is not available on this route')
    filters = []
    if policy.get('target', 'source') != 'source':
        if policy.get('target') != 'sdr-bt709' or policy.get('conversion') != 'hable':
            raise ValueError('The requested color conversion has no validated implementation')
        if not all(f.get('is_hdr') for f in source_facts):
            raise ValueError('Hable conversion requires explicitly identified HDR sources; do not guess the input')
        if not {'zscale', 'tonemap'} <= set(capabilities.get('filters', [])):
            raise ValueError('Requested HDR-to-SDR conversion requires zscale and tonemap; approximate fallback is disabled')
        filters.append('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p')
        output.update(color_primaries='bt709', color_transfer='bt709', color_space='bt709', color_range='tv',
                      pix_fmt='yuv420p', bit_depth=8, is_hdr=False)
    if policy.get('grade'):
        if output.get('is_hdr') or output.get('bit_depth', 8) != 8 or output.get('color_transfer') not in ('bt709', 'iec61966-2-1'):
            raise ValueError('This creative grade requires an explicitly accepted tested SDR 8-bit route')
        from grade import resolve_grade
        filters.append(resolve_grade(policy['grade']))
    encoding = (contract or {}).get('workflow', {}).get('encoding', {})
    if encoding.get('chroma', 'source') == 'source' and not output.get('pix_fmt', '').startswith(('yuv420p', 'yuvj420p', 'yuv422p', 'yuv444p')):
        raise ValueError('This source pixel format has no validated preservation encoder; confirm a supported technical conversion')
    codec = encoding.get('videoCodec', 'auto')
    if codec == 'source':
        codec = output.get('codec')
    if codec not in ('auto', 'h264', 'hevc'):
        raise ValueError('Source codec has no validated MP4 preservation encoder; choose h264 or hevc explicitly')
    if codec != 'auto':
        output['encoder'] = {'h264': 'libx264', 'hevc': 'libx265'}[codec]
    depth = encoding.get('bitDepth', 'source')
    depth = output.get('bit_depth', 8) if depth == 'source' else depth
    chroma = encoding.get('chroma', 'source')
    if chroma == 'source':
        chroma = '422' if '422' in output.get('pix_fmt', '') else '444' if '444' in output.get('pix_fmt', '') else '420'
    output['pix_fmt'] = f'yuv{chroma}p' + (f'{depth}le' if depth > 8 else '')
    output['bit_depth'] = depth
    if purpose == 'composition' and (any(f.get('is_vfr') for f in source_facts) or (contract or {}).get('workflow', {}).get('video', {}).get('timing') == 'vfr'):
        raise ValueError('Native composition is CFR; confirm a frame-rate conversion before rendering VFR effects')
    result = {k: output.get(k) for k in (*COLOR_FIELDS, 'pix_fmt', 'bit_depth', 'is_hdr', 'encoder')}
    result.update(filters=filters, inputOverride=interpretation, encoderArgs=encoder_args(output, capabilities), purpose=purpose, policyVersion=1)
    return result
