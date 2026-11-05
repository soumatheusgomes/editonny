#!/usr/bin/env python3
"""Extract frame-aligned source clips for video generation and save timing metadata. Multiple pieces may share one request; clips over 15 seconds are rejected by default. HDR converts to SDR with the engine color pipeline, dimensions fit 1080x1920 or 1920x1080, and source rates above 31 fps are halved. Use original source time so later editing changes do not require regeneration."""
import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction
LIMIT_SECONDS = 15.0

def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if out.returncode != 0:
        sys.exit(f'error: could not read {path}: {out.stderr.strip()[:200]}')
    d = json.loads(out.stdout)
    v = next((s for s in d['streams'] if s.get('codec_type') == 'video'), None)
    if v is None:
        sys.exit(f'error: {path} has no video stream')
    a = next((s for s in d['streams'] if s.get('codec_type') == 'audio'), None)
    fps = Fraction(v.get('avg_frame_rate') or v.get('r_frame_rate') or '30/1')
    if fps <= 0:
        fps = Fraction(v.get('r_frame_rate') or '30/1')
    (w, h) = (int(v['width']), int(v['height']))
    rot = 0
    for sd in v.get('side_data_list') or []:
        if 'rotation' in sd:
            rot = int(float(sd['rotation']))
    if 'rotate' in (v.get('tags') or {}):
        rot = int(float(v['tags']['rotate']))
    if abs(rot) % 180 == 90:
        (w, h) = (h, w)
    hdr = v.get('color_transfer') in ('arib-std-b67', 'smpte2084')
    return dict(fps=fps, w=w, h=h, audio=a is not None, hdr=hdr, dur=float(d['format'].get('duration') or 0))

def has_filter(name):
    out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    return any((len(l.split()) > 1 and l.split()[1] == name for l in out.splitlines()))

def sdr_chain():
    if has_filter('zscale') and has_filter('tonemap'):
        return 'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'
    return 'format=yuv420p10le,colorspace=all=bt709:iall=bt2020:itrc=bt2020-10:irange=tv:range=tv:fast=0:format=yuv420p'

def main():
    ap = argparse.ArgumentParser(description='Extract frame-aligned source footage for video generation (up to 15 seconds).')
    ap.add_argument('--source', required=True, help='Original video from sources in edl_manual.json')
    ap.add_argument('--start', type=float, help='Start in original-source seconds')
    ap.add_argument('--end', type=float, help='End in original-source seconds')
    ap.add_argument('--pieces', help='Join pieces, e.g. 39.7-41.3,42.6-46.0 in source seconds')
    ap.add_argument('--output', required=True, help='Output MP4; timing JSON is saved beside it with the same stem')
    ap.add_argument('--no-audio', action='store_true', help='Omit audio (included by default because some models use it)')
    ap.add_argument('--limit', type=float, default=LIMIT_SECONDS, help='Maximum duration (default 15 seconds, observed Kling edit limit)')
    a = ap.parse_args()
    if a.pieces:
        try:
            pieces = [tuple((float(x) for x in p.split('-'))) for p in a.pieces.split(',')]
        except ValueError:
            sys.exit('error: --pieces requires start-end,start-end in seconds')
    elif a.start is not None and a.end is not None:
        pieces = [(a.start, a.end)]
    else:
        sys.exit('error: supply --start and --end, or --pieces')
    for (start, end) in pieces:
        if end <= start:
            sys.exit(f'error: piece {start}-{end}: end must follow start')
    info = probe(a.source)
    fps = info['fps']
    fps_out = fps / 2 if fps > 31 else fps
    step = 2 if fps > 31 else 1
    exact_frames = []
    for (start, end) in pieces:
        q0 = int(round(start * fps))
        q1 = int(round(end * fps))
        q1 = q0 + (q1 - q0) // step * step
        if q1 <= q0:
            sys.exit(f'error: piece {start}-{end} is too short')
        if info['dur'] and end > info['dur'] + 0.05:
            sys.exit(f"error: piece {start}-{end} exceeds source duration ({info['dur']:.2f} s)")
        exact_frames.append((q0, q1))
    total = float(sum((Fraction(q1 - q0) / fps for (q0, q1) in exact_frames)))
    if total > a.limit:
        sys.exit(f'error: {total:.2f} s exceeds {a.limit:.0f} s per request. Split requests and join them at a speech cut.')
    if total < 3.0:
        print(f'warning: {total:.2f} s is short; clips from about 3.5 seconds worked. Add handles on both ends.')
    portrait = info['h'] >= info['w']
    box = (1080, 1920) if portrait else (1920, 1080)
    scale_filter = f"scale=w='min({box[0]},iw)':h='min({box[1]},ih)':force_original_aspect_ratio=decrease:flags=lanczos,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    pre = sdr_chain() + ',' if info['hdr'] else ''
    if info['hdr']:
        print('HDR source: converting to SDR with the engine color pipeline')
    (parts, ins, inputs) = ([], [], [])
    audio = info['audio'] and (not a.no_audio)
    for (i, (q0, q1)) in enumerate(exact_frames):
        ss = max(0.0, (q0 - 0.5) / float(fps))
        inputs += ['-ss', f'{ss:.6f}', '-i', a.source]
        nq = q1 - q0
        sel = f'trim=end_frame={nq}' + (",select='not(mod(n\\,2))'" if step == 2 else '')
        parts.append(f'[{i}:v]{sel},setpts=N/({float(fps_out)}*TB),{pre}{scale_filter},format=yuv420p[v{i}]')
        if audio:
            off = q0 / float(fps) - ss
            parts.append(f'[{i}:a]atrim=start={off:.6f}:duration={nq / float(fps):.6f},asetpts=PTS-STARTPTS,aresample=48000[a{i}]')
        ins.append(f'[v{i}]' + (f'[a{i}]' if audio else ''))
    n = len(exact_frames)
    parts.append(''.join(ins) + f'concat=n={n}:v=1:a={(1 if audio else 0)}[v]' + ('[a]' if audio else ''))
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    cmd = ['ffmpeg', '-v', 'error', '-y'] + inputs + ['-filter_complex', ';'.join(parts), '-map', '[v]']
    if audio:
        cmd += ['-map', '[a]', '-c:a', 'aac', '-b:a', '192k']
    cmd += ['-r', f'{fps_out.numerator}/{fps_out.denominator}', '-c:v', 'libx264', '-crf', '16', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-write_tmcd', '0', '-map_metadata', '-1', '-movflags', '+faststart', a.output]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit('error: FFmpeg failed to extract the clip')
    (request_data, c) = ([], 0.0)
    for (q0, q1) in exact_frames:
        d = (q1 - q0) / fps
        request_data.append({'start': round(float(Fraction(q0) / fps), 6), 'end': round(float(Fraction(q1) / fps), 6), 'firstFrame': q0, 'frames': q1 - q0, 'sourceStart': round(float(c), 6)})
        c += d
    meta = {'source': os.path.abspath(a.source), 'sourceFps': float(fps), 'fileFps': float(fps_out), 'file': os.path.abspath(a.output), 'duration': round(float(total), 4), 'pieces': request_data, 'hdrConverted': info['hdr'], 'note': "Times are original-source seconds; sourceStart is the piece's offset within the submitted file"}
    js = os.path.splitext(a.output)[0] + '.json'
    with open(js, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f'ok {a.output} ({float(total):.2f} s, {n} piece(s), {float(fps_out):.2f} fps) + {os.path.basename(js)}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
