#!/usr/bin/env python3
"""Local voice effects within measured speech intervals.

List effects: python3 voice.py --list
Process an interval: python3 voice.py --project PATH --effect old-phone --start 3.2 --end 5
Process a separate export: python3 voice.py --file INPUT.mp4 --output OUTPUT.mp4 --effect echo --start 12 --end 15

Project mode rebuilds cut_voice.mp4 from the unchanged source and every registered
voiceEffects interval. Repeating a request replaces the matching interval rather
than stacking effects. Use --remove N, --clear, or --rebuild to manage the project.

Times are seconds of the edited cut. Strength is 0–1; lower values mix the dry
voice and can sound doubled for pitch effects. Ramps are 30–60 ms, default 40.
The processor restores interval loudness, compensates filter latency, preserves
video frames and audio timing, and leaves audio outside the intervals unchanged
apart from the final AAC encoding. Process standalone files before adding music.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = '2026-10-13e'          # Version participates in output signatures, invalidating stale processing.
PRESETS_FILE = os.path.join(HERE, 'voice-presets.json')
SR = 48000
QB = 8                       # Bytes per frame: stereo, float32
POST_ROLL = 0.15              # Post-roll for filter alignment and tempo processing.
DEFAULT_RAMP = 40.0          # ms
CEILING = '-1dB'                # Processed-interval peak ceiling.
MIN_SEGMENT = 0.15            # s
LOUDNESS_TOLERANCE = 0.4          # Maximum loudness deviation from the original interval.


class VoiceError(Exception):
    pass


def warning(msg):
    print(f'  warning: {msg}', flush=True)


def fmt(v, digits=2):
    return f'%.{digits}f' % v


def num(content, name):
    try:
        value = float(str(content).replace(',', '.'))
    except ValueError:
        raise VoiceError(f'{name} must be a number (received "{content}")')
    if not math.isfinite(value):
        raise VoiceError(f'{name} must be a finite number')
    return value


# ------------------------------------------------------------------------------------------------ presets
def load_presets():
    if not os.path.exists(PRESETS_FILE):
        raise VoiceError(f'Cannot find {PRESETS_FILE} (expected beside voice.py)')
    with open(PRESETS_FILE, encoding='utf-8') as f:
        data = json.load(f)
    return {p['id']: p for p in data['presets']}


def preset_from(presets, name):
    name = str(name).strip().lower().replace('_', '-').replace(' ', '-')
    if name in presets:
        return presets[name]
    for p in presets.values():                      # Accept the effect ID or its English display name.
        for alt in (p.get('name'),):
            if alt and strip_accents(alt).lower().replace(' ', '-') == strip_accents(name):
                return p
    raise VoiceError(f'Unknown effect "{name}". Choose one of: ' + ', '.join(presets) + ' (python3 voice.py --list)')


def strip_accents(s):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def presets_signature(presets, ids):
    h = hashlib.sha1()
    for i in sorted(set(ids)):
        p = presets[i]
        h.update(json.dumps({k: p.get(k) for k in ('filterChain', 'channels', 'inputLufs', 'delayMs', 'auxiliaryFiles')},
                            sort_keys=True).encode())
        for a in p.get('auxiliaryFiles', []):
            path = os.path.join(HERE, a['file'])
            if os.path.exists(path):
                h.update(signature(path).encode())
    h.update(VERSION.encode())
    return h.hexdigest()[:16]


def list_presets(presets):
    print('Voice effects (python3 voice.py --project P --effect <id> --start <s> --end <s>):\n')
    for p in presets.values():
        markers = []
        if p.get('changesPitch'):
            markers.append('changes pitch')
        if p.get('channels') == 2:
            markers.append('stereo')
        if p.get('approximation'):
            markers.append('approximation')
        m = f' ({", ".join(markers)})' if markers else ''
        print(f'  {p["id"]:<18} {p["name"]}{m}')
        print(f'  {"":<18} {p["whenToUse"]}')
        print(f'  {"":<18} processing: {p["description"]}')
        if p.get('caveat'):
            print(f'  {"":<18} note: {p["caveat"]}')
        print()


# ------------------------------------------------------------------------------------------------ ffmpeg
def require_ffmpeg():
    for prog in ('ffmpeg', 'ffprobe'):
        if not shutil.which(prog):
            raise VoiceError(f'Cannot find {prog}. Install FFmpeg (macOS: brew install ffmpeg) and retry.')


def run(cmd, what, output_text=False):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise VoiceError(f'FFmpeg failed while trying to {what}: ' + ' | '.join(err[-4:]))
    return p.stderr.decode('utf-8', 'replace') if output_text else p


FF = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y']
RAW = ['-f', 'f32le', '-ar', str(SR), '-ac', '2']


def probe(media_path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'stream=index,codec_type,codec_name,start_time,duration,nb_frames,channels,sample_rate:stream_disposition='
                        'attached_pic:format=duration,start_time', '-of', 'json', media_path], capture_output=True)
    if p.returncode != 0:
        raise VoiceError(f'Cannot read {media_path}')
    d = json.loads(p.stdout.decode('utf-8', 'replace') or '{}')
    fmt_ = d.get('format') or {}
    info = {'video': None, 'audio': None, 'duration': float(fmt_.get('duration') or 0),
            'start': float(fmt_.get('start_time') or 0)}
    for s in d.get('streams', []):
        t = s.get('codec_type')
        if t == 'video' and (s.get('disposition') or {}).get('attached_pic'):
            continue                                 # Embedded audio cover art is not video.
        if t in ('video', 'audio') and info[t] is None:
            info[t] = s
    if info['audio'] is None:
        raise VoiceError(f'{media_path} has no audio. Export with audio before applying a voice effect.')
    return info


def audio_delay(info):
    """Seconds from the player origin to the first audio sample; zero for audio-only files."""
    if not info['video']:
        return 0.0
    a0 = float(info['audio'].get('start_time') or 0)
    v0 = float(info['video'].get('start_time') or 0)
    t0 = a0 - min(info['start'], a0, v0)
    return t0 if t0 > 0.0005 else 0.0


def decode(media_path, raw, channels):
    duplicate = ['-af', 'pan=stereo|c0=c0|c1=c0'] if int(channels or 2) == 1 else []
    run(FF + ['-i', media_path, '-map', '0:a:0', '-vn'] + duplicate + RAW + [raw], f'decode audio from {media_path}')


def loudness(raw, start_index, n):
    """Integrated gated EBU R128 LUFS for raw frames [start_index, start_index + n). Return None for silence; intervals under 0.4 seconds use approximate RMS loudness."""
    if n >= int(0.4 * SR):
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-nostats'] + RAW + ['-i', raw, '-af',
               f'atrim=start_sample={start_index}:end_sample={start_index + n},ebur128', '-f', 'null', '-']
        p = subprocess.run(cmd, capture_output=True)
        content = p.stderr.decode('utf-8', 'replace')
        summary = content[content.rfind('Summary:'):] if 'Summary:' in content else ''
        m = re.search(r'I:\s+(-?[\d.]+|-inf) LUFS', summary)
        if m:
            v = float(m.group(1)) if m.group(1) != '-inf' else -200.0
            return None if v <= -69.5 else v
    with open(raw, 'rb') as f:
        f.seek(start_index * QB)
        a = array('f')
        a.frombytes(f.read(n * QB))
    if sys.byteorder == 'big':
        a.byteswap()
    if not a:
        return None
    ms = sum(x * x for x in a) / len(a)
    return None if ms <= 1e-9 else 10 * math.log10(ms) - 0.691 + 3.01


def supported_filter_options(graph, err):
    """Omit options unavailable in the installed FFmpeg version."""
    new = graph
    if "'irnorm'" in err or "'irgain'" in err:
        new = new.replace('irnorm=-1:irgain=1', 'gtype=none')
    if "'latency'" in err:
        new = new.replace(':latency=1', '')
    if "'normalize'" in err:
        new = new.replace(':normalize=0', '')
    return None if new == graph else new


def run_graph(inputs, graph, output, what, limit_seconds=None):
    for _ in range(4):
        cmd = list(FF)
        for e in inputs:
            cmd += e
        cmd += ['-filter_complex', graph, '-map', '[result]']
        if limit_seconds:
            cmd += ['-t', '%.3f' % limit_seconds]
        cmd += RAW + [output]
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode == 0:
            return
        err = p.stderr.decode('utf-8', 'replace')
        g2 = supported_filter_options(graph, err)
        if not g2:
            raise VoiceError(f'FFmpeg failed while trying to {what}: ' + ' | '.join(err.strip().splitlines()[-4:]))
        graph = g2


def read_frames(raw, start_index, n):
    with open(raw, 'rb') as f:
        f.seek(start_index * QB)
        return f.read(n * QB)


def write(media_path, data):
    with open(media_path, 'wb') as f:
        f.write(data)


def process_segment(raw, N, a, b, p, strength, tmp, k):
    """Render aligned, loudness-restored and peak-limited frames for the interval [a, b)."""
    n = b - a
    delay = int(round(float(p.get('delayMs', 0)) * SR / 1000))
    # Pitch filters receive a small pre-roll so the processed onset is already aligned.
    pre = min(a, -delay + int(0.02 * SR)) if delay < 0 else 0
    wa, wb = a - pre, min(N, b + int(POST_ROLL * SR) + max(0, delay))
    W = wb - wa
    input_start = os.path.join(tmp, f'{k}_input.f32')
    write(input_start, read_frames(raw, wa, W))
    L0 = loudness(input_start, pre, n)
    if L0 is None:
        warning(f'Interval {fmt(a / SR)} to {fmt(b / SR)} seconds is silent; apply the effect without loudness matching')
    # Normalize input so the calibrated effect is independent of recording level.
    input_gain = 0.0 if L0 is None else max(-24.0, min(24.0, float(p.get('inputLufs', -20)) - (L0 - 3.01)))
    graph = p['filterChain'] if '[out]' in p['filterChain'] else f"[0:a]{p['filterChain']}[out]"
    graph = graph.replace('[0:a]', '[voice]')
    rising = 'pan=stereo|c0=c0|c1=c0' if int(p.get('channels', 1)) == 1 else 'anull'
    graph = (f'[0:a]pan=mono|c0=0.5*c0+0.5*c1,volume={input_gain:.2f}dB[voice];{graph};'
             f'[out]{rising},aformat=sample_fmts=flt:sample_rates={SR}:channel_layouts=stereo[result]')
    inputs = [RAW + ['-i', input_start]]
    for auxiliary in p.get('auxiliaryFiles', []):
        path = os.path.join(HERE, auxiliary['file'])
        if not os.path.exists(path):
            raise VoiceError(f'Missing auxiliary file for effect {p["id"]}: {path}')
        inputs.append((['-stream_loop', '-1'] if auxiliary.get('loop') else []) + ['-i', path])
    proc = os.path.join(tmp, f'{k}_processed.f32')
    run_graph(inputs, graph, proc, f'apply effect {p["id"]}', limit_seconds=W / SR + 1.0)
    with open(proc, 'rb') as f:
        data = f.read()
    offset = pre + delay
    if offset > 0:
        data = data[offset * QB:]
    elif offset < 0:
        data = bytes(-offset * QB) + data
    missing = n * QB - len(data)
    if missing > int(0.01 * SR) * QB:
        warning(f'Effect {p["id"]} returned {missing // QB} fewer samples in interval {fmt(a / SR)} seconds; padded with silence')
    if missing > 0:
        data += bytes(missing)
    al = os.path.join(tmp, f'{k}_aligned.f32')
    write(al, data[:n * QB])
    L1 = loudness(al, 0, n)
    g = 0.0 if (L0 is None or L1 is None) else max(-30.0, min(30.0, L0 - L1))
    fin = os.path.join(tmp, f'{k}_fin.f32')
    lim = f'alimiter=limit={CEILING}:level=0:attack=2:release=60:latency=1'
    if strength >= 0.999:
        run_graph([RAW + ['-i', al]], f'[0:a]volume={g:.3f}dB,{lim}[result]', fin, 'restore loudness')
    else:
        w = strength * 10 ** (g / 20)
        run_graph([RAW + ['-i', input_start], RAW + ['-i', al]],
                    f'[0:a]atrim=start_sample={pre}:end_sample={pre + n},asetpts=PTS-STARTPTS,volume={1 - strength:.5f}[d];[1:a]volume={w:.6f}[w];'
                    f'[d][w]amix=inputs=2:normalize=0:duration=longest,{lim}[result]', fin, 'mix with the original voice')
    L2 = loudness(fin, 0, n)
    for step in range(3):
        if L0 is None or L2 is None or abs(L0 - L2) <= LOUDNESS_TOLERANCE:
            break
        fin2 = os.path.join(tmp, f'{k}_fin{step + 2}.f32')
        run_graph([RAW + ['-i', fin]], f'[0:a]volume={L0 - L2:.3f}dB,{lim}[result]', fin2, 'restore loudness')
        fin, L2 = fin2, loudness(fin2, 0, n)
    size = os.path.getsize(fin) // QB
    if size < n:
        with open(fin, 'ab') as f:
            f.write(bytes((n - size) * QB))
    return fin, {'lufsBefore': L0, 'lufsAfter': L2}


def ramp(r):
    return [0.5 - 0.5 * math.cos(math.pi * (i + 0.5) / r) for i in range(r)]


def read_array(media_path, start_index, n):
    a = array('f')
    with open(media_path, 'rb') as f:
        f.seek(start_index * QB)
        a.frombytes(f.read(n * QB))
    if sys.byteorder == 'big':
        a.byteswap()
    if len(a) < 2 * n:
        a.extend([0.0] * (2 * n - len(a)))
    return a


def write_array(media_path, a):
    if sys.byteorder == 'big':
        a = array('f', a)
        a.byteswap()
    with open(media_path, 'wb') as f:
        f.write(a.tobytes())


def mix_edges(o, e, ri, ro, gain=1.0):
    """Blend the processed signal and dry voice through cosine ramps."""
    m = array('f', e) if gain == 1.0 else array('f', [x * gain for x in e])
    n = len(m) // 2
    for i, w in enumerate(ramp(ri) if ri else []):
        j = 2 * i
        m[j] = o[j] * (1 - w) + m[j] * w
        m[j + 1] = o[j + 1] * (1 - w) + m[j + 1] * w
    for i, w in enumerate(ramp(ro) if ro else []):
        j = 2 * (n - ro + i)
        m[j] = m[j] * (1 - w) + o[j] * w
        m[j + 1] = m[j + 1] * (1 - w) + o[j + 1] * w
    return m


def adjust_edges(raw, a, b, fin, ri, ro, L0, tmp, k):
    """Measure the actual ramped mix and compensate phase cancellation by adjusting the wet gain."""
    n = b - a
    o = read_array(raw, a, n)
    e = read_array(fin, 0, n)
    m = mix_edges(o, e, ri, ro)
    output = os.path.join(tmp, f'{k}_bordas.f32')
    write_array(output, m)
    L = loudness(output, 0, n)
    if L0 is None or L is None or not (ri or ro):
        return output, L
    gain = 1.0
    peak = None
    for _ in range(3):
        if abs(L0 - L) <= 0.15:
            break
        new = gain * 10 ** ((L0 - L) / 20)
        if peak is None:
            peak = max(max(e), -min(e)) or 1e-9
        gain = min(new, 10 ** (-0.5 / 20) / peak, 2.0)        # Never exceed -0.5 dBFS or +6 dB gain.
        if gain <= 1.0 and new > 1.0:
            break
        m = mix_edges(o, e, ri, ro, gain)
        write_array(output, m)
        L = loudness(output, 0, n)
    return output, L


# Restore speech suppressed by an effect
# Match the quality gate silence threshold using 10 ms windows and a speech-relative ceiling.
# Band-limiting can suppress word edges and introduce silence absent from the source.
# Mix only enough dry voice to cross the threshold in the affected windows.
WINDOW = 480                 # 10 ms at 48 kHz.
BREATH_SECONDS = 0.20             # Voice processing.
HEADROOM_DB = 1.0               # Headroom above the repaired interval threshold.


def mono_db(data):
    """Decibels for each 10 ms window of the interleaved stereo signal mixed to mono."""
    out = []
    n = len(data) // 2 // WINDOW
    for j in range(n):
        seg = data[2 * j * WINDOW: 2 * (j + 1) * WINDOW]
        e = 0.0
        for i in range(0, len(seg), 2):
            m = (seg[i] + seg[i + 1]) * 0.5
            e += m * m
        out.append(10 * math.log10(e / WINDOW + 1e-12))
    return out


def pause_threshold(raw, total):
    """Derive the whole-file silence threshold using the same ceiling as the quality gate."""
    r = subprocess.run(FF[:-1] + RAW + ['-i', raw, '-t', str(total / SR), '-ac', '1', '-f', 'f32le', '-'], capture_output=True)
    a = array('f'); a.frombytes(r.stdout[: len(r.stdout) // 4 * 4])
    if sys.byteorder == 'big':
        a.byteswap()
    n = len(a) // WINDOW
    if n < 10:
        return None
    db = sorted(10 * math.log10(sum(x * x for x in a[j * WINDOW:(j + 1) * WINDOW]) / WINDOW + 1e-12) for j in range(n))
    p10, p90 = db[int(0.10 * (n - 1))], db[int(0.90 * (n - 1))]
    # Use the speech-relative ceiling so added background music cannot invalidate this check.
    # An interval passing this ceiling remains valid with or without music.
    return max(min(max(p10 + 14.0, p90 - 28.0), p90 - 20.0), p90 - 20.0)


def silences(db, th, minimum):
    out, start_index = [], None
    for j, v in enumerate(db + [th + 99]):
        if v < th and start_index is None:
            start_index = j
        elif v >= th and start_index is not None:
            if (j - start_index) * WINDOW / SR >= minimum:
                out.append((start_index, j))
            start_index = None
    return out


def protect_breaths(raw, a, b, fin, th, tmp, k):
    """Restore dry voice only where processing creates a new silence of at least 0.2 seconds; return the processed file and repaired intervals."""
    if th is None:
        return fin, []
    pad = int(0.3 * SR)
    start_index = max(0, (a - pad) // WINDOW * WINDOW)
    end = b + pad
    original_signal = read_array(raw, start_index, end - start_index)
    processed_signal = read_array(fin, 0, b - a)
    new = array('f', original_signal)
    new[2 * (a - start_index): 2 * (b - start_index)] = processed_signal[: 2 * (b - a)]
    d_o, d_n = mono_db(original_signal), mono_db(new)
    j0, j1 = (a - start_index) // WINDOW, (b - start_index + WINDOW - 1) // WINDOW
    created = []
    window = SR / WINDOW
    for r0, r1 in silences(d_n, th, BREATH_SECONDS):
        if r1 <= j0 or r0 >= j1:
            continue
        old = max([q1 - q0 for q0, q1 in silences(d_o[r0:r1], th, 0.0)] or [0]) / window
        if (r1 - r0) / window <= old + 0.02:
            continue                    # The effect did not increase the source silence.
        created.append((max(r0, j0), min(r1, j1)))
    if not created:
        return fin, []
    target = 10 ** ((th + HEADROOM_DB) / 10)
    gain = [0.0] * (len(d_n) + 1)
    for r0, r1 in created:
        for j in range(r0, r1):
            en, eo = 10 ** (d_n[j] / 10), 10 ** (d_o[j] / 10)
            if d_o[j] < th or en >= target:
                continue                # Preserve original silence; restore only speech suppressed by processing.
            gain[j] = max(0.0, min(1.0, math.sqrt(max(0.0, min(target, eo) - en) / eo)))
    # Use 30 ms smoothing across neighboring windows to avoid clicks.
    smooth = [max(gain[max(0, j - 3): j + 4]) for j in range(len(gain))]
    m = array('f', processed_signal)
    for i in range(b - a):
        pos = (a - start_index + i) / WINDOW
        j = int(pos)
        if j >= len(smooth) - 1:
            break
        g = smooth[j] + (smooth[j + 1] - smooth[j]) * (pos - j)
        if g > 1e-4:
            q = 2 * (a - start_index + i)
            m[2 * i] += g * original_signal[q]
            m[2 * i + 1] += g * original_signal[q + 1]
    output = os.path.join(tmp, f'{k}_breath.f32')
    write_array(output, m)
    return output, [((start_index + r0 * WINDOW) / SR, (start_index + r1 * WINDOW) / SR) for r0, r1 in created]


def assemble(raw, output, total, pieces):
    """Copy the original first total frames, replacing only the requested ramped intervals."""
    block = 1 << 20
    with open(raw, 'rb') as fo, open(output, 'wb') as fs:
        pos = 0
        for a, b, fin in pieces + [(total, total, None)]:
            fo.seek(pos * QB)
            remaining = (a - pos) * QB
            while remaining > 0:
                d = fo.read(min(block, remaining))
                if not d:
                    break
                fs.write(d)
                remaining -= len(d)
            if fin:
                with open(fin, 'rb') as ff:
                    fs.write(ff.read((b - a) * QB))
            pos = b


CONTAINER = {'.mp4': 'aac', '.mov': 'aac', '.m4v': 'aac', '.mkv': 'aac', '.webm': 'opus'}
AUDIO_ONLY = {'.wav': ['-c:a', 'pcm_s24le'], '.flac': ['-c:a', 'flac'], '.mp3': ['-c:a', 'libmp3lame', '-b:a', '256k'],
            '.m4a': ['-c:a', 'aac', '-b:a', '256k'], '.aac': ['-c:a', 'aac', '-b:a', '256k'],
            '.opus': ['-c:a', 'libopus', '-b:a', '192k'], '.ogg': ['-c:a', 'libopus', '-b:a', '192k']}


def check_output(info, destination):
    ext = os.path.splitext(destination)[1].lower()
    if info['video'] and ext not in CONTAINER and ext not in AUDIO_ONLY:
        raise VoiceError(f'Unsupported video output "{ext or "no extension"}": use .mp4, .mov, .mkv, or .webm')
    codec_args = (info['video'] or {}).get('codec_name')
    if ext == '.webm' and codec_args not in ('vp8', 'vp9', 'av1'):
        raise VoiceError(f'Video codec is {codec_args} and WebM requires VP8, VP9, or AV1; use .mp4 '
                   f'or .mkv')
    if not info['video'] and ext not in AUDIO_ONLY and ext not in CONTAINER:
        raise VoiceError(f'Unsupported audio output "{ext or "no extension"}": use .wav, .m4a, .mp3, or .flac')


def encode(origin, info, raw, destination, t0):
    """Copy video without re-encoding and encode new audio with its original start offset."""
    ext = os.path.splitext(destination)[1].lower()
    # A unique name never collides with the origin, so cleanup cannot delete source footage.
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(destination) + '.', suffix='.processing' + ext,
                               dir=os.path.dirname(destination) or '.')
    os.close(fd)
    if info['video'] and ext in CONTAINER:
        codec_args = ['-c:a', 'libopus', '-b:a', '192k'] if CONTAINER[ext] == 'opus' else ['-c:a', 'aac', '-b:a', '256k']
        cmd = FF + ['-i', origin] + RAW + ['-i', raw, '-map', f'0:{info["video"]["index"]}', '-map', '1:a:0',
                                           '-c:v', 'copy'] + codec_args + ['-map_metadata', '0']
        if t0:
            # Raw input offsets are represented in sample timestamps instead of -itsoffset.
            cmd += ['-af', f'asetpts=PTS+{t0:.6f}/TB']
        if ext in ('.mp4', '.mov', '.m4v'):
            cmd += ['-movflags', '+faststart']
    else:
        cmd = FF + RAW + ['-i', raw] + AUDIO_ONLY.get(ext, ['-c:a', 'aac', '-b:a', '256k'])
    try:
        run(cmd + [tmp], f'write {destination}')
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def signature(media_path):
    st = os.stat(media_path)
    return f'{st.st_size}-{int(st.st_mtime)}'


def process(origin, destination, segments, presets, ramp_ms, labels=None):
    """Rebuild the destination from the original with every requested interval; return interval reports."""
    info = probe(origin)
    check_output(info, destination)
    t0 = audio_delay(info)
    tmp = tempfile.mkdtemp(prefix='editonny-voice-')
    try:
        raw = os.path.join(tmp, 'source.f32')
        decode(origin, raw, info['audio'].get('channels'))
        N = os.path.getsize(raw) // QB
        try:
            total = int(round(float(info['audio'].get('duration')) * SR))
        except (TypeError, ValueError):
            total = N
        if not 0 < total <= N:
            total = N
        end = t0 + total / SR                      # Player time at which audio ends.
        r0 = int(round(max(30.0, min(60.0, ramp_ms)) * SR / 1000))
        plans = []
        for k, t in enumerate(segments):            # Validate all intervals before processing any of them.
            a = max(0, int(round((t['start'] - t0) * SR)))
            b = total if t.get('end') is None else min(total, int(round((t['end'] - t0) * SR)))
            if a >= total:
                raise VoiceError(f'Interval {k + 1} starts at {fmt(t["start"])} seconds and audio ends at {fmt(end)} s')
            if b - a < int(MIN_SEGMENT * SR):
                raise VoiceError(f'Interval {k + 1} ({fmt(t0 + a / SR)} to {fmt(t0 + b / SR)} seconds) is too short; minimum duration is '
                           f'{fmt(MIN_SEGMENT)} s' + (f' (audio ends at {fmt(end)} s)' if b >= total else ''))
            if t.get('end') is not None and t['end'] > end + 0.05:
                warning(f'Interval {k + 1} requests an end at {fmt(t["end"])} seconds and audio ends at {fmt(end)} seconds; use the audio end')
            plans.append((a, b))
        pieces, rel = [], []
        th = pause_threshold(raw, total)
        for k, (t, (a, b)) in enumerate(zip(segments, plans)):
            p = presets[t['effect']]
            r = min(r0, (b - a) // 4)
            ri, ro = (0 if a == 0 else r), (0 if b >= total else r)
            speech_label = (labels or [None] * len(segments))[k]
            print(f'  {k + 1}. {p["id"]} from {fmt(t0 + a / SR)} to {fmt(t0 + b / SR)} s' +
                  (f', strength {fmt(t["strength"])}' if t['strength'] < 0.999 else '') + (f' ("{speech_label}")' if speech_label else '') + '...',
                  flush=True)
            fin, info_t = process_segment(raw, N, a, b, p, t['strength'], tmp, k)
            fin, info_t['lufsAfter'] = adjust_edges(raw, a, b, fin, ri, ro, info_t['lufsBefore'], tmp, k)
            fin, info_t['restoredSpeech'] = protect_breaths(raw, a, b, fin, th, tmp, k)
            pieces.append((a, b, fin))
            info_t.update(effect=p['id'], start=round(t0 + a / SR, 3), end=round(t0 + b / SR, 3), strength=t['strength'])
            rel.append(info_t)
            if p['id'] == 'analog-echo' and (b - a) / SR < 2.5:
                warning('The analog echo arrives after 2.342 seconds and may be inaudible in this short interval.')
        output = os.path.join(tmp, 'output.f32')
        assemble(raw, output, total, pieces)
        d = os.path.dirname(destination)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        encode(origin, info, output, destination, t0)
        new = probe(destination)
        d0, d1 = float(info['audio'].get('duration') or info['duration']), float(new['audio'].get('duration') or new['duration'])
        final_report = {'sourceDuration': d0, 'outputDuration': d1, 'sourceFrames': (info['video'] or {}).get('nb_frames'),
                     'outputFrames': (new['video'] or {}).get('nb_frames'), 'sourceDelay': t0,
                     'outputDelay': audio_delay(new) if new['video'] else 0.0}
        return rel, final_report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def print_report(rel, final_report, destination):
    for i, r in enumerate(rel, 1):
        if r['lufsBefore'] is not None and r['lufsAfter'] is not None:
            difference = r['lufsAfter'] - r['lufsBefore']
            status_text = 'unchanged' if abs(difference) <= LOUDNESS_TOLERANCE + 0.05 else f'difference of {fmt(difference, 1)} LU'
            volume_text = f'volume {fmt(r["lufsBefore"], 1)} -> {fmt(r["lufsAfter"], 1)} LUFS ({status_text})'
        else:
            volume_text = 'loudness unmeasured (silent interval)'
        print(f'  interval {i}: {r["effect"]} {fmt(r["start"])} to {fmt(r["end"])} s ({fmt(r["end"] - r["start"])} seconds of speech), {volume_text}')
        for x, y in r.get('restoredSpeech') or []:
            print(f'    Restored speech: the effect introduced silence from {fmt(x)} to {fmt(y)} seconds; '
                  f'mixed dry voice underneath only in that interval')
    d0, d1 = final_report['sourceDuration'], final_report['outputDuration']
    q0, q1 = final_report['sourceFrames'], final_report['outputFrames']
    s0, s1 = final_report.get('sourceDelay', 0.0), final_report.get('outputDelay', 0.0)
    if s0 or s1:
        # AAC may add initial padding; compare audio end time on the video clock.
        ok = abs((s0 + d0) - (s1 + d1)) < 0.005 and abs(s0 - s1) < 0.03 and q0 == q1
    else:
        ok = abs(d0 - d1) < 0.03 and q0 == q1
    print(f'  Audio duration {fmt(d0, 3)} -> {fmt(d1, 3)} s' + (f', video frames {q0} -> {q1}' if q0 else '') +
          (f', audio starts {fmt(s0 * 1000, 0)} ms after the first frame' if (s0 or s1) else '') +
          (' (unchanged)' if ok else ' (REVIEW: changed)'))
    print(f'ok: {destination}')


def find_project(p):
    p = os.path.abspath(os.path.expanduser(p))
    if os.path.basename(p) == 'project.json':
        p = os.path.dirname(p)
    pj = os.path.join(p, 'project.json')
    if not os.path.exists(pj):
        raise VoiceError(f'{p} is not an editonny project (missing project.json)')
    return p, pj


def read_project(pj):
    with open(pj, encoding='utf-8') as f:
        content = f.read()
    try:
        cfg = json.loads(content)
    except ValueError as e:
        raise VoiceError(f'{pj} contains invalid JSON ({e}): correct it and retry')
    if not isinstance(cfg, dict):
        raise VoiceError(f'{pj} is not a JSON object ({{...}})')
    return cfg, content


def top_keys(content):
    """Map top-level JSON keys to their key start, value start, and value end offsets."""
    out, n, i = {}, len(content), content.find('{') + 1
    pending = True
    while 0 < i < n:
        c = content[i]
        if c == '"' and pending:
            j = i + 1
            while content[j] != '"':
                j += 2 if content[j] == '\\' else 1
            key = json.loads(content[i:j + 1])
            k = content.index(':', j + 1) + 1
            while content[k] in ' \t\r\n':
                k += 1
            d, m = 0, k
            while m < n:
                ch = content[m]
                if ch == '"':
                    m += 1
                    while content[m] != '"':
                        m += 2 if content[m] == '\\' else 1
                elif ch in '[{':
                    d += 1
                elif ch in ']}':
                    if d == 0:
                        break
                    d -= 1
                elif ch == ',' and d == 0:
                    break
                m += 1
            end = m
            while content[end - 1] in ' \t\r\n':
                end -= 1
            out[key] = (i, k, end)
            pending, i = False, m
            continue
        if c == ',':
            pending = True
        elif c == '}':
            break
        i += 1
    return out


def json_value(key, v, ind):
    if key == 'voiceEffects' and isinstance(v, list) and v:
        return '[\n' + ',\n'.join(ind * 2 + json.dumps(t, ensure_ascii=False) for t in v) + '\n' + ind + ']'
    return json.dumps(v, ensure_ascii=False)


def edit_text(content, before, cfg, keys):
    """Change only requested JSON fields while preserving untouched source text byte for byte."""
    m = re.search(r'\n([ \t]+)"', content)
    ind = m.group(1) if m else '  '
    for key in keys:
        pos = top_keys(content)
        if key in cfg:
            v = json_value(key, cfg[key], ind)
            if key in pos:
                _, start_index, end = pos[key]
                content = content[:start_index] + v + content[end:]
            else:
                close = content.rindex('}')
                q = close - 1
                while content[q] in ' \t\r\n':
                    q -= 1
                row = f'{ind}{json.dumps(key)}: {v}'
                if content[q] == '{':
                    content = content[:q + 1] + '\n' + row + '\n' + content[close:]
                else:
                    content = content[:q + 1] + ',\n' + row + content[q + 1:]
        elif key in pos:
            ki, _, end = pos[key]
            q = ki - 1
            while content[q] in ' \t\r\n':
                q -= 1
            if content[q] == ',':                        # Remove the preceding comma with the deleted field.
                content = content[:q] + content[end:]
            else:                                    # For the first field, remove the following comma.
                r = end
                while content[r] in ' \t\r\n':
                    r += 1
                if content[r] == ',':
                    r += 1
                    while content[r] in ' \t\r\n':
                        r += 1
                content = content[:ki] + content[r:]
    return content


def write_project(pj, cfg, content):
    before = json.loads(content)
    keys = [k for k in ('video', 'voiceSource', 'voiceEffects') if cfg.get(k) != before.get(k) or (k in cfg) != (k in before)]
    try:
        new = edit_text(content, before, cfg, keys)
        if json.loads(new) != cfg:
            raise ValueError('JSON content mismatch')
    except (ValueError, IndexError):
        m = re.search(r'\n( +)"', content)
        new = json.dumps(cfg, ensure_ascii=False, indent=len(m.group(1)) if m else 2) + '\n'
    tmp = pj + '.writing'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
    os.replace(tmp, pj)


OWN_FILE_PATTERN = re.compile(r'_voice\d*\.[A-Za-z0-9]+$')


def edit_folder(proj, cfg):
    edit = os.path.normpath(os.path.join(proj, cfg.get('edit', '../edit')))
    if not os.path.isdir(edit):
        raise VoiceError(f'Cannot find the cut edit directory: {edit} (project.json edit field)')
    return edit


def same_folder(a, b):
    """Compare directory paths after filesystem Unicode normalization, including composed and decomposed names."""
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        import unicodedata

        def n(s):
            return unicodedata.normalize('NFC', os.path.normcase(os.path.normpath(os.path.abspath(s))))
        return n(a) == n(b)


def owner(edit, path, proj):
    """Check file ownership; refuse files still used by another project sharing the edit directory."""
    m = read_marker(edit, path)
    if not m:
        return not os.path.lexists(os.path.join(edit, path))
    if same_folder(m.get('project', ''), proj):
        return True
    try:
        other, _ = read_project(os.path.join(m['project'], 'project.json'))
        return other.get('video') != path
    except (OSError, KeyError, VoiceError):
        return True


def folders(proj, cfg):
    edit = edit_folder(proj, cfg)
    v = cfg.get('video')
    origin = cfg.get('voiceSource')
    if not origin:
        origin = v or 'cut.mp4'
        m = read_marker(edit, origin) if OWN_FILE_PATTERN.search(origin) else None
        if m and m.get('origin') and os.path.exists(os.path.join(edit, m['origin'])):
            # Effects are always rebuilt from the unchanged original.
            warning(f'"video" references {origin}, created by voice.py; use its original ({m["origin"]})')
            origin = m['origin']
    # Never overwrite another project or unrelated media sharing the edit folder.
    output = v if v and v != origin and OWN_FILE_PATTERN.search(v) and owner(edit, v, proj) else None
    if not os.path.exists(os.path.join(edit, origin)):
        raise VoiceError(f'Cannot find cut video: {os.path.join(edit, origin)}')
    if not output:
        base, ext = os.path.splitext(origin)
        output = f'{base}_voice{ext or ".mp4"}'
        n = 2
        while not owner(edit, output, proj):        # Allocate a distinct output for each project sharing an edit folder.
            output = f'{base}_voice{n}{ext or ".mp4"}'
            n += 1
    return edit, origin, output


def marker_from(edit, output):
    return os.path.join(edit, os.path.splitext(output)[0] + '.json')


def read_marker(edit, output):
    try:
        with open(marker_from(edit, output), encoding='utf-8') as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def normalize_segments(items, presets):
    out = []
    for t in items:
        if 'effect' not in t:
            raise VoiceError('Each interval needs --effect (see python3 voice.py --list)')
        p = preset_from(presets, t['effect'])
        start = num(t.get('start', 0.0), '--start')
        end = None if t.get('end') is None else num(t['end'], '--end')
        strength = num(t.get('strength', 1.0), '--strength')
        if start < 0:
            raise VoiceError('--start must not be negative')
        if end is not None and end <= start:
            raise VoiceError(f'--end ({fmt(end)}) must be greater than --start ({fmt(start)})')
        if not 0 <= strength <= 1:
            raise VoiceError('--strength must be between 0 and 1')
        if strength < 0.05:
            raise VoiceError(f'--strength {fmt(strength)} makes no change in the interval starting at {fmt(start)} seconds; use --remove N to remove it')
        out.append({'effect': p['id'], 'start': round(start, 3), 'end': None if end is None else round(end, 3),
                    'strength': round(strength, 3)})
    return out


def check_overlap(segments):
    tr = sorted(segments, key=lambda t: t['start'])
    for x, y in zip(tr, tr[1:]):
        end = x['end'] if x['end'] is not None else float('inf')
        if y['start'] < end - 1e-6:
            end = 'the end' if x['end'] is None else f'{fmt(end)} s'
            raise VoiceError(f'Intervals from {fmt(x["start"])} seconds to {end} ({x["effect"]}) and from {fmt(y["start"])} s ({y["effect"]}) '
                       f'overlap; use one effect at a time. ' + (
                           'Remove the old interval with apply.py --project P --voice remove N (use --voice inspect for indices), '
                           if os.environ.get('EDITONNY_VIA_SCENE') else
                           'Remove the old interval with python3 voice.py --project P --remove N, ')
                       + 'or adjust --start/--end')
    return tr


def show(segments):
    if not segments:
        print('  No voice-effect intervals')
    for i, t in enumerate(segments, 1):
        end = 'the end' if t.get('end') is None else f'{fmt(t["end"])} s'
        f = '' if t.get('strength', 1) >= 0.999 else f', strength {fmt(t["strength"])}'
        print(f'  {i}. {t["effect"]:<18} {fmt(t["start"])} seconds to {end}{f}')


def speech(edit, segments):
    """Read the transcript text in each effect interval to detect stale timing after a recut."""
    try:
        with open(os.path.join(edit, 'transcripts', 'cut.json'), encoding='utf-8') as f:
            ps = [w for w in json.load(f)['words'] if str(w.get('text', '')).strip()]
    except (OSError, ValueError, KeyError, TypeError):
        return [None] * len(segments)
    out = []
    for t in segments:
        end = float('inf') if t.get('end') is None else t['end']
        inside = [str(w['text']).strip() for w in ps if t['start'] <= (float(w['start']) + float(w['end'])) / 2 < end]
        content = ' '.join(inside)
        out.append((content[:57] + '...') if len(content) > 60 else (content or '(no speech)'))
    return out


def status(edit, origin, output, segments, presets, proj):
    """Return an empty string for a current processed file, otherwise a reason to rebuild."""
    m = read_marker(edit, output)
    dst = os.path.join(edit, output)
    if not os.path.exists(dst):
        return f'Missing {output}'
    if not m:
        return f'{output} is missing ownership marker {os.path.basename(marker_from(edit, output))}'
    if m.get('origin') != origin or m.get('sourceSignature') != signature(os.path.join(edit, origin)):
        return f'{origin} changed after processing; check for a recut'
    if m.get('intervals') != segments:
        return 'project.json intervals changed'
    if m.get('presets') != presets_signature(presets, [t['effect'] for t in segments]):
        return 'voice presets or processor changed'
    if m.get('outputSignature') != signature(dst):
        return f'{output} changed outside voice.py'
    return ''


def rebuild(proj, pj, cfg, content, segments, presets, ramp_ms, force=False):
    edit, origin, output = folders(proj, cfg)
    segments = check_overlap(segments)
    if not segments:
        return clear(proj, pj, cfg, content)
    reason = status(edit, origin, output, segments, presets, proj)
    if not reason and (read_marker(edit, output) or {}).get('rampMs') != ramp_ms:
        reason = 'ramp duration changed'
    if not reason and not force and cfg.get('video') == output:
        print(f'ok: {output} is already current with {len(segments)} interval(s)')
        show(segments)
        return
    dst = os.path.join(edit, output)
    before = read_marker(edit, output) or {}
    texts = speech(edit, segments)
    print(f'Rebuilding {output} from {origin} ({len(segments)} interval(s)):', flush=True)
    t0 = time.time()
    rel, final_report = process(os.path.join(edit, origin), dst, segments, presets, ramp_ms, labels=texts)
    marker = {'version': VERSION, 'project': proj, 'origin': origin, 'sourceSignature': signature(os.path.join(edit, origin)),
             'intervals': segments, 'speech': texts, 'presets': presets_signature(presets, [t['effect'] for t in segments]),
             'rampMs': ramp_ms, 'outputSignature': signature(dst), 'report': rel}
    with open(marker_from(edit, output), 'w', encoding='utf-8') as f:
        json.dump(marker, f, ensure_ascii=False, indent=1)
    cfg['voiceSource'] = origin
    cfg['video'] = output
    cfg['voiceEffects'] = segments
    write_project(pj, cfg, content)
    enabled = enable(proj, dst)
    print_report(rel, final_report, dst)
    if before.get('sourceSignature') and before.get('sourceSignature') != marker['sourceSignature']:
        # A recut can move an effect interval onto different speech.
        old_items = {json.dumps(t, sort_keys=True): f for t, f in zip(before.get('intervals') or [], before.get('speech') or [])}
        for i, (t, f) in enumerate(zip(segments, texts), 1):
            f0 = old_items.get(json.dumps(t, sort_keys=True))
            if f and f0 and f0 != f:
                warning(f'The cut changed and interval {i} now contains "{f}" (previously: "{f0}"). Check times with '
                      f'python3 build.py words and adjust with --remove {i} and a new --effect')
    print(f'  project.json: "video": "{output}" and {len(segments)} interval(s) in "voiceEffects" (completed in {fmt(time.time() - t0, 1)} s)')
    print(f'  Next: cd "{proj}" && python3 build.py sample START END (or compose); ' +
          (f'assets/cut.mp4 now uses {output}' if enabled else f'the builder links assets/cut.mp4 to {output}'))


def enable(proj, dst):
    """Relink project assets/cut.mp4 to the new audio version when possible; the builder handles copy or burned-in blur cases."""
    a = os.path.join(proj, 'assets', 'cut.mp4')
    if os.path.exists(os.path.join(proj, 'assets', 'generated', 'blur.json')) or not os.path.lexists(a):
        return False
    try:
        if os.path.exists(a) and os.path.samefile(a, dst):
            return True
        os.remove(a)
        os.link(dst, a)
        return True
    except OSError:
        return False


def clear(proj, pj, cfg, content):
    had = cfg.get('voiceEffects') or []
    try:
        edit, origin, output = folders(proj, cfg)
    except VoiceError as e:
        edit, origin, output = None, cfg.get('voiceSource') or 'cut.mp4', None
        warning(f'{e}: clear project.json only')
    if cfg.get('voiceSource') or 'voiceEffects' in cfg:
        if cfg.get('voiceSource'):
            if cfg['voiceSource'] == 'cut.mp4':
                cfg.pop('video', None)               # Restore the project video field to the normal builder default.
            else:
                cfg['video'] = cfg['voiceSource']
        for k in ('voiceSource', 'voiceEffects'):
            cfg.pop(k, None)
        write_project(pj, cfg, content)
    if edit:
        m = read_marker(edit, output)
        # Remove only an owned output that no other live project uses.
        if m and owner(edit, output, proj):
            for path in (os.path.join(edit, output), marker_from(edit, output)):
                if os.path.exists(path):
                    os.remove(path)
        enable(proj, os.path.join(edit, origin))
    print(f'ok: {len(had)} interval(s) removed; "video" restored to {origin}. Run build.py again.')


def parse_args(argv):
    op = {'intervals': []}
    pending, current, i = {}, None, 0
    while i < len(argv):
        a = argv[i]

        def value():
            if i + 1 >= len(argv):
                raise VoiceError(f'Missing value after {a}')
            return argv[i + 1]
        if a in ('-h', '--help'):
            op['help'] = True
        elif a in ('--list', '--clear', '--rebuild', '--overwrite'):
            op[a[2:]] = True
        elif a in ('--project', '--file', '--output'):
            op[a[2:]] = value()
            i += 1
        elif a == '--remove':
            op['remove'] = int(num(value(), '--remove'))
            i += 1
        elif a == '--ramp':
            op['rampMs'] = num(value(), '--ramp')
            i += 1
        elif a == '--effect':
            current = dict(pending, effect=value())
            pending = {}
            op['intervals'].append(current)
            i += 1
        elif a in ('--start', '--end', '--strength'):
            target = current if current is not None else pending
            target[a[2:]] = num(value(), a)
            i += 1
        else:
            raise VoiceError(f'Unknown argument "{a}". See python3 voice.py --help')
        i += 1
    if pending:
        if not op['intervals']:
            raise VoiceError('Missing --effect (list available effects with python3 voice.py --list)')
    return op


def main(argv):
    try:
        op = parse_args(argv)
        if op.get('help') or not argv:
            print(__doc__)
            return 0
        presets = load_presets()
        if op.get('list'):
            list_presets(presets)
            return 0
        require_ffmpeg()
        ramp_ms = float(op.get('rampMs', DEFAULT_RAMP))
        if not 30 <= ramp_ms <= 60:
            raise VoiceError('--ramp must be between 30 and 60 ms')
        if op.get('file'):
            if op.get('project'):
                raise VoiceError('Use either --project or --file, not both')
            src = os.path.abspath(os.path.expanduser(op['file']))
            if not op.get('output'):
                raise VoiceError('With --file, also specify --output OUT.mp4; the input is never modified')
            dst = os.path.abspath(os.path.expanduser(op['output']))
            if not os.path.exists(src):
                raise VoiceError(f'Cannot find {src}')
            if os.path.exists(dst) and os.path.samefile(src, dst):
                raise VoiceError('--output must be a different file; the original is never modified')
            if os.path.exists(dst) and not op.get('overwrite'):
                raise VoiceError(f'{dst} already exists: choose another name or use --overwrite')
            segments = check_overlap(normalize_segments(op['intervals'], presets))
            if not segments:
                raise VoiceError('Specify an effect: --effect old-phone --start 3.2 --end 5.0')
            for t in segments:
                if presets[t['effect']].get('caveat'):
                    warning(f'{t["effect"]}: {presets[t["effect"]]["caveat"]}')
            print(f'Applying {len(segments)} interval(s) in {os.path.basename(src)}:', flush=True)
            rel, final_report = process(src, dst, segments, presets, ramp_ms)
            print_report(rel, final_report, dst)
            return 0
        if not op.get('project'):
            raise VoiceError('Specify a project (--project PATH) or input video (--file VIDEO.mp4 --output OUT.mp4)')
        proj, pj = find_project(op['project'])
        cfg, content = read_project(pj)
        if op.get('clear'):                         # Allow clearing even if a saved interval is now invalid.
            return clear(proj, pj, cfg, content) or 0
        current_items = normalize_segments(cfg.get('voiceEffects') or [], presets)
        changed = False
        if op.get('remove') is not None:
            current_items = sorted(current_items, key=lambda t: t['start'])
            n = op['remove']
            if not 1 <= n <= len(current_items):
                show(current_items)
                raise VoiceError(f'Unknown interval {n}: project interval count: {len(current_items)}')
            t = current_items.pop(n - 1)
            print(f'Removing interval {n}: {t["effect"]} from {fmt(t["start"])} s')
            changed = True
        new_items = normalize_segments(op['intervals'], presets)
        for t in new_items:
            # Replace the same start/end interval instead of duplicating effects.
            current_items = [x for x in current_items if not (abs(x['start'] - t['start']) < 1e-3 and x['end'] == t['end'])]
            current_items.append(t)
            if presets[t['effect']].get('caveat'):
                warning(f'{t["effect"]}: {presets[t["effect"]]["caveat"]}')
            changed = True
        current_items = sorted(current_items, key=lambda t: t['start'])
        if changed or op.get('rebuild'):
            rebuild(proj, pj, cfg, content, current_items, presets, ramp_ms, force=bool(op.get('rebuild')))
            return 0
        print(f'Project: {proj}')
        show(current_items)
        if current_items:
            edit, origin, output = folders(proj, cfg)
            reason = status(edit, origin, output, current_items, presets, proj)
            if cfg.get('video') != output:
                reason = reason or f'The project.json video is not {output}'
            print(f'  {output}: ' + ('current' if not reason else f'OUTDATED ({reason}): run python3 voice.py --project "{proj}" --rebuild'))
        return 0
    except VoiceError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Stopped', file=sys.stderr)
        return 130


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main(sys.argv[1:]))
