#!/usr/bin/env python3
'Build a timestamped filmstrip for a short video interval.\n\nUse it to inspect details missed by a one-frame-per-second contact sheet: caption entrances, zoom duration, and the exact frame where a title disappears.\n\nUsage: uv run --directory ENGINE python helpers/filmstrip.py VIDEO START END --output filmstrip.jpg\nUse --fps, --columns, --width, and --track to control sampling and layout. START and END accept seconds or mm:ss.cc. A track range such as 0.6:0.85 crops a vertical band using top-to-bottom height fractions, for example the caption area.'

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SKILL = Path(__file__).resolve().parent.parent   # engine directory (helpers/..)


def time_value(txt: str) -> float:
    txt = txt.strip().replace(",", ".")
    if ":" in txt:
        m, s = txt.split(":", 1)
        return int(m) * 60 + float(s)
    return float(txt)


def font_value(size_value: int):
    for p in (SKILL / "assets" / "fonts" / "figtree" / "Figtree-Variable.ttf",
              Path("/System/Library/Fonts/Menlo.ttc"), Path("C:/Windows/Fonts/arial.ttf")):
        try:
            return ImageFont.truetype(str(p), size_value)
        except Exception:
            continue
    return ImageFont.load_default()


def main() -> int:
    ap = argparse.ArgumentParser(description='Extract a timestamped filmstrip from a video interval.')
    ap.add_argument('video', type=Path)
    ap.add_argument('start_value')
    ap.add_argument('end_value')
    ap.add_argument("--output", type=Path, required=True, help='output .jpg file', dest='output_value')
    ap.add_argument("--fps", type=float, default=10.0, help='filmstrip frames per second (default 10; use 30 for frame-by-frame inspection)', dest='fps')
    ap.add_argument("--columns", type=int, default=6, dest='columns')
    ap.add_argument("--width", type=int, default=240, help="width of each filmstrip frame in pixels", dest='width_value')
    ap.add_argument("--track", help="height fraction, for example 0.6:0.85", dest='track_value')
    a = ap.parse_args()
    start_time, end_value = time_value(a.start_value), time_value(a.end_value)
    if end_value <= start_time:
        print('error: end must exceed start', file=sys.stderr)
        return 2
    if (end_value - start_time) * a.fps > 240:
        print('error: filmstrip exceeds 240 frames. Shorten the interval or lower --fps', file=sys.stderr)
        return 2
    video = a.video.expanduser().absolute()
    if not video.is_file():
        print(f'error: not found {video}', file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="editonny-filmstrip-") as tmp:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-ss", f'{start_time:.3f}',
                            "-i", str(video), "-t", f'{end_value - start_time:.3f}', "-an", "-vf", f'fps={a.fps},showinfo',
                            "-q:v", "3", str(Path(tmp) / "t_%04d.jpg")], capture_output=True)
        if r.returncode != 0:
            print('error: FFmpeg failed: ' + r.stderr.decode("utf-8", "replace")[-400:], file=sys.stderr)
            return 1
        relative_times = [float(m.group(1)) for m in re.finditer(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+)",
                                                      r.stderr.decode("utf-8", "replace"))]
        files_value = sorted(Path(tmp).glob("t_*.jpg"))
        if not files_value:
            print('error: no frames in this interval', file=sys.stderr)
            return 1
        if len(relative_times) != len(files_value):
            relative_times = [i / a.fps for i in range(len(files_value))]
        f = font_value(15)
        tiles = []
        for t_rel, file_value in zip(relative_times, files_value):
            im = Image.open(file_value).convert("RGB")
            if a.track_value:
                y0, y1 = [float(x) for x in a.track_value.split(":")]
                im = im.crop((0, int(y0 * im.height), im.width, int(y1 * im.height)))
            h = max(1, int(round(im.height * a.width_value / im.width)))
            im = im.resize((a.width_value, h))
            d = ImageDraw.Draw(im)
            t = start_time + t_rel
            timestamp = f'{int(t // 60):02d}:{t % 60:05.2f}'
            box_value = d.textbbox((4, 2), timestamp, font=f)
            d.rectangle([0, 0, box_value[2] + 4, box_value[3] + 3], fill=(0, 0, 0))
            d.text((4, 2), timestamp, fill=(255, 230, 0), font=f)
            tiles.append(im)
    th = tiles[0].height
    rows = (len(tiles) + a.columns - 1) // a.columns
    sheet_value = Image.new("RGB", (a.width_value * a.columns, th * rows), (18, 18, 18))
    for i, im in enumerate(tiles):
        sheet_value.paste(im, ((i % a.columns) * a.width_value, (i // a.columns) * th))
    output_value = a.output_value.expanduser().absolute()
    output_value.parent.mkdir(parents=True, exist_ok=True)
    sheet_value.save(output_value, quality=90)
    print(f'Filmstrip ready: {output_value} ({len(tiles)} frames, {a.fps:g} per second)')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
