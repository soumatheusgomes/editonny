#!/usr/bin/env python3
"""Remove a generated green background and save transparent VP9 WebM plus a contact sheet. Supports cropping, retiming, neutral smoke/glass, warm fire, illuminated green, masks and bottom fades."""
import argparse
import os
import subprocess
import sys
import numpy as np
from PIL import Image

def main():
    ap = argparse.ArgumentParser(description='Remove generated green background and save transparent VP9 WebM.')
    ap.add_argument('input_file')
    ap.add_argument('output')
    ap.add_argument('--sheet', help='Contact-sheet JPEG (default: <output>_sheet.jpg)')
    ap.add_argument('--speed', type=float, default=1.0, help='Playback speed (1.2 = 20%% faster)')
    ap.add_argument('--width', type=int, default=720, help='WebM width (default 720; use 1080 for sharp fullscreen effects)')
    ap.add_argument('--start', type=float, default=0.0, help='Start time in clip seconds')
    ap.add_argument('--end', type=float, default=99.0, help='End time in clip seconds')
    ap.add_argument('--margin', type=float, default=14.0, help='Green margin (higher removes more edge spill)')
    ap.add_argument('--mode', choices=['fire', 'neutral'], default='fire', help='fire preserves warm colors; neutral makes smoke and glass grayscale')
    ap.add_argument('--crop', help='x,y,width,height in source pixels; crop effect area before resizing')
    ap.add_argument('--fade-bottom', help='y0,y1 as height fractions: fade the effect out between them')
    ap.add_argument('--erase', help='x0,y0,x1,y1 in output pixels: erase a rectangle')
    ap.add_argument('--warm', action='store_true', help='Recolor pale fire with a red-orange-yellow ramp')
    ap.add_argument('--lit-green', action='store_true', help='Treat illuminated/yellowed green as background (explosions)')
    ap.add_argument('--min-alpha', type=float, default=0.05, help='Set alpha below this threshold to zero to remove dirty haze')
    a = ap.parse_args()
    (src, out) = (a.input_file, a.output)
    q = a.sheet or os.path.splitext(out)[0] + '_sheet.jpg'
    pr = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', src], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if pr.returncode != 0:
        sys.exit(f'error: could not read {src}')
    (W, H) = map(int, pr.stdout.strip().split(','))
    LW = a.width
    MG = a.margin
    pre = ''
    if a.crop:
        (cx, cy, cw, ch) = map(int, a.crop.split(','))
        pre = f'crop={cw}:{ch}:{cx}:{cy},'
        (W, H) = (cw, ch)
    LH = int(round(LW * H / W / 2)) * 2
    dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', str(a.start), '-to', str(a.end), '-i', src, '-vf', f'{pre}setpts=PTS/{a.speed},fps=30,scale={LW}:{LH}:flags=lanczos', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{LW}x{LH}', '-r', '30', '-i', '-', '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-b:v', '0', '-crf', '32', '-g', '30', '-keyint_min', '30', '-auto-alt-ref', '0', '-metadata:s:v:0', 'alpha_mode=1', '-an', out], stdin=subprocess.PIPE)
    BG = None
    BS = 20
    n = 0
    frames = []
    while True:
        buf = dec.stdout.read(LW * LH * 3)
        if len(buf) < LW * LH * 3:
            break
        f = np.frombuffer(buf, np.uint8).reshape(LH, LW, 3).astype(np.float32)
        if a.erase:
            (x0, y0, x1, y1) = map(int, a.erase.split(','))
            f = f.copy()
            B8 = 10
            top = f[y0 - B8:y0, x0:x1].mean(0)
            bot = f[y1:y1 + B8, x0:x1].mean(0)
            lef = f[y0:y1, x0 - B8:x0].mean(1)
            rig = f[y0:y1, x1:x1 + B8].mean(1)
            for arr in (top, bot):
                k = np.ones(31) / 31
                arr[:] = np.stack([np.convolve(np.pad(arr[:, c], 15, mode='edge'), k, 'valid') for c in range(3)], 1)
            for arr in (lef, rig):
                k = np.ones(15) / 15
                arr[:] = np.stack([np.convolve(np.pad(arr[:, c], 7, mode='edge'), k, 'valid') for c in range(3)], 1)
            v = np.linspace(0, 1, y1 - y0)[:, None, None]
            u = np.linspace(0, 1, x1 - x0)[None, :, None]
            (c00, c01, c10, c11) = ((top[0] + lef[0]) / 2, (top[-1] + rig[0]) / 2, (bot[0] + lef[-1]) / 2, (bot[-1] + rig[-1]) / 2)
            f[y0:y1, x0:x1] = (1 - v) * top[None] + v * bot[None] + (1 - u) * lef[:, None] + u * rig[:, None] - ((1 - u) * (1 - v) * c00 + u * (1 - v) * c01 + (1 - u) * v * c10 + u * v * c11)
        if BG is None:
            c = np.concatenate([f[:20, :20].reshape(-1, 3), f[:20, -20:].reshape(-1, 3), f[-20:, :20].reshape(-1, 3), f[-20:, -20:].reshape(-1, 3)])
            BG = np.median(c, 0)
            print('background', BG.round(1))
            BG_GD = (BG[1] - max(BG[0], BG[2])) / max(BG[1], 1)
            if BG[1] < 100 or BG_GD < 0.3:
                print('warning: first-frame corners are not greenscreen green; check --crop and the contact sheet')
        (r, g, b) = (f[..., 0], f[..., 1], f[..., 2])
        gd = (g - np.maximum(r, b)) / np.maximum(g, 1)
        lim = BG_GD * 0.82
        if a.lit_green:
            ok = ((g > r * 1.22) & (g > b * 1.5) & (g - r > 35)).astype(np.float32)
        else:
            ok = ((gd > lim) & (g - r > 70)).astype(np.float32)
        (hb, wb) = (LH // BS, LW // BS)
        okb = ok[:hb * BS, :wb * BS].reshape(hb, BS, wb, BS).mean((1, 3))
        fb_ = (f[:hb * BS, :wb * BS] * ok[:hb * BS, :wb * BS, None]).reshape(hb, BS, wb, BS, 3).sum((1, 3)) / np.maximum(okb * BS * BS, 1)[..., None]
        wv = (okb > 0.25).astype(np.float32)
        val = fb_ * wv[..., None]
        wgt = wv.copy()
        for _ in range(12):
            pv = np.pad(val, ((1, 1), (1, 1), (0, 0)), mode='edge')
            pw = np.pad(wgt, 1, mode='edge')
            sv = sum((pv[i:i + hb, j:j + wb] for i in range(3) for j in range(3)))
            sw = sum((pw[i:i + hb, j:j + wb] for i in range(3) for j in range(3)))
            val = np.where(wv[..., None] > 0, fb_ * wv[..., None], sv / 9)
            wgt = np.where(wv > 0, 1.0, sw / 9)
        field = val / np.maximum(wgt, 0.0001)[..., None]
        field = np.where(wgt[..., None] > 0.02, field, BG)
        for _ in range(4):
            pc = np.pad(field, ((1, 1), (1, 1), (0, 0)), mode='edge')
            field = sum((pc[i:i + hb, j:j + wb] for i in range(3) for j in range(3))) / 9
        LB = np.array(Image.fromarray(field.clip(0, 255).astype(np.uint8)).resize((LW, LH), Image.BILINEAR)).astype(np.float32)
        span = np.maximum(LB[..., 1] - LB[..., 0] - 2 * MG, 30)
        al = 1.0 - np.clip((g - r - MG) / span, 0, 1)
        a3 = np.maximum(al, 0.001)[..., None]
        F = ((f - (1.0 - al)[..., None] * LB) / a3).clip(0, 255)
        (fr, fg, fb) = (F[..., 0], F[..., 1], F[..., 2])
        neutral = 0.5 * fr + 0.5 * fb
        warm_color = np.clip((fr - fb - 25.0) / 40.0, 0, 1) * np.clip((al - 0.25) / 0.3, 0, 1) * (a.mode == 'fire')
        fg = np.minimum(fg, fr * 0.97)
        (fr, fg, fb) = [cc * warm_color + neutral * (1 - warm_color) for cc in (fr, fg, fb)]
        if a.warm:
            L = (0.45 * F[..., 0] + 0.45 * F[..., 1] + 0.1 * F[..., 2]) / 255
            t = np.clip(L * 0.95 + 0.04, 0, 1)
            fr = np.clip(t * 2.1, 0, 1) * 255
            fg = np.clip(t * 1.55 - 0.28, 0, 1) * 255
            fb = np.clip(t * 2.2 - 1.35, 0, 1) * 255
        if a.fade_bottom:
            (y0f, y1f) = map(float, a.fade_bottom.split(','))
            yy = np.linspace(0, 1, LH)[:, None]
            al = al * np.clip((y1f - yy) / max(y1f - y0f, 0.001), 0, 1)
        al = np.where(al < a.min_alpha, 0, al)
        al[:3] = 0
        al[-3:] = 0
        al[:, :3] = 0
        al[:, -3:] = 0
        z = al <= 0
        (fr, fg, fb) = [np.where(z, 128.0, cc) for cc in (fr, fg, fb)]
        o = np.dstack([fr, fg, fb, al * 255]).clip(0, 255).astype(np.uint8)
        if n % 15 == 0:
            frames.append(o)
        enc.stdin.write(o.tobytes())
        n += 1
    enc.stdin.close()
    enc.wait()
    if not frames:
        sys.exit('error: no frames read; check --start/--end')
    th = [Image.fromarray(x).resize((180, int(180 * LH / LW))) for x in frames[:12]]
    (tw, tht) = th[0].size
    sheet = Image.new('RGB', (tw * len(th), tht * 2))
    for (i, t) in enumerate(th):
        for (row, color) in enumerate(((235, 232, 225), (22, 22, 26))):
            bgi = Image.new('RGBA', t.size, color + (255,))
            bgi.alpha_composite(t)
            sheet.paste(bgi.convert('RGB'), (i * tw, row * tht))
    sheet.save(q, quality=88)
    print(f'ok {out}: {n} frames ({n / 30:.2f} s), {LW}x{LH} · sheet {q}')
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
