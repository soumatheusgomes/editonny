#!/usr/bin/env python3
"""Replace damaged frames or freeze a clip at a chosen frame. A contact sheet helps select the frame before impact. Original timing stays unchanged until the hold."""
import argparse
import subprocess
import sys

def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 'stream=r_frame_rate,nb_read_packets', '-show_entries', 'format=duration', '-of', 'csv=p=0:nk=1', path], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.split()
    (num, den) = out[0].split(',')[0].split('/')
    fps = float(num) / float(den)
    frames = int(out[0].split(',')[1]) if ',' in out[0] else 0
    dur = float(out[-1])
    return (fps, frames, dur)

def main():
    ap = argparse.ArgumentParser(description='Freeze a clip at a selected frame.')
    ap.add_argument('input_file')
    ap.add_argument('output')
    ap.add_argument('--frame', type=int, help='Zero-based frame to freeze')
    ap.add_argument('--at', type=float, help='Alternatively, time in clip seconds to freeze')
    ap.add_argument('--hold', type=float, default=0.0, help='Hold duration (default: through the clip end)')
    ap.add_argument('--sheet', help='Save only a neighboring-frame contact sheet for inspection')
    ap.add_argument('--replace', help='Replace damaged frames, e.g. 110:109,111:112 (target:replacement)')
    a = ap.parse_args()
    (fps, frames, dur) = probe(a.input_file)
    if a.frame is None and a.at is None and (not a.replace):
        sys.exit('error: supply --frame, --at or --replace')
    q = a.frame if a.frame is not None else int(round(a.at * fps)) if a.at is not None else None
    if q is not None and frames and (not 0 <= q < frames):
        sys.exit(f'error: clip has {frames} frames (0 to {frames - 1})')
    if a.sheet and q is None:
        sys.exit('error: contact sheet requires --frame or --at')
    if a.sheet:
        start = max(0, q - 4)
        sel = '+'.join((f'eq(n\\,{k})' for k in range(start, start + 8)))
        r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.input_file, '-vf', f"select='{sel}',scale=240:-2,drawtext=text='%{{eif\\:n+{start}\\:d}}':x=10:y=10:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6,tile=8x1", '-frames:v', '1', a.sheet], capture_output=True)
        if r.returncode != 0:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.input_file, '-vf', f"select='{sel}',scale=240:-2,tile=8x1", '-frames:v', '1', a.sheet], check=True)
        print(f'ok {a.sheet}: frames {start} to {start + 7}')
        return
    (chain, last) = ([], '0:v')
    if a.replace:
        for (k, pair) in enumerate(a.replace.split(',')):
            (target, neighbors) = (int(x) for x in pair.split(':'))
            chain.append(f'[{last}]split[t{k}a][t{k}b];[t{k}a][t{k}b]freezeframes=first={target}:last={target}:replace={neighbors}[t{k}]')
            last = f't{k}'
    msg = []
    if q is not None:
        hold = a.hold if a.hold > 0 else max(0.0, dur - (q + 1) / fps)
        chain.append(f'[{last}]trim=end_frame={q + 1},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={hold:.4f}[c]')
        last = 'c'
        msg.append(f'freeze at frame {q} ({q / fps:.3f} s into clip), hold {hold:.2f} s')
    if a.replace:
        msg.insert(0, f'replaced {a.replace}')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.input_file, '-filter_complex', ';'.join(chain), '-map', f'[{last}]', '-an', '-r', f'{fps:.6f}', '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', a.output], check=True)
    print(f'ok {a.output}: ' + ' · '.join(msg))
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
