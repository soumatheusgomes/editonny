"""N labeled frames of a video in one PNG (contact sheet).

Usage:
    uv run python helpers/contact_sheet.py <video> --times 1.5 3 4.25 -o sheet.png [--cols 4]
    uv run python helpers/contact_sheet.py edit/cut.mp4 --junctions -o edit/verify/junctions.png

`--junctions` reads the cut timeline next to the video (cut.mp4 -> cut_timeline.json) and
shows, for every junction, the last two frames of the outgoing range and the first two
frames of the incoming range, frame-exact.

Also the shared frame utilities for the cut helpers (grade.py, timeline_view.py,
verify_cut.py import `extract_frame`, `ui_font`, `find_timeline`, `default_verify_dir`).
"""

from __future__ import annotations

import argparse
import io
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import fmt_time, load_json, load_font, probe_video

BG = (20, 20, 24)
PANEL = (32, 32, 38)
FG = (238, 238, 240)
DIM = (150, 150, 160)
ACCENT = (255, 196, 0)

_SYSTEM_FONTS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def ui_font(size: int, bold: bool = False):
    """Font for labels on analysis images: bundled Figtree, then system, then PIL default."""
    for key in (("text_bold", "sans_bold") if bold else ("text_semibold", "text_bold")):
        try:
            return load_font(key, size)
        except Exception:
            continue
    for fp in _SYSTEM_FONTS:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def parse_time(value: str) -> float:
    """'12.5', '00:12.50' or '1:02:03.5' -> seconds."""
    text = str(value).strip().replace(",", ".")
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) or len(parts) > 3:
        raise argparse.ArgumentTypeError(f'invalid time: {value!r} (use seconds such as 12.5, or mm:ss.cc)')
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def extract_frame(video: Path | str, t: float, width: int | None = None, height: int | None = None,
                  vf: str | None = None, fps: float | None = None, duration: float | None = None,
                  hdr: bool | None = None) -> Image.Image:
    """Decode the frame shown at time `t` (nearest frame), upright, as an RGB PIL image.

    The seek lands half a frame early so the frame whose timestamp is closest to `t`
    is the first one kept (ffmpeg drops frames that start before the seek point).
    `vf` is an extra filter chain applied before scaling (e.g. a grade).
    `hdr`: True tone-maps HDR to an SDR display image; None probes the file; False means the
    caller already handled it (or SDR). This display derivative does not convert the master
    or verify its HDR appearance.
    """
    info = None
    if fps is None or duration is None or hdr is None:
        info = probe_video(video)
        fps = fps or info.get("fps") or 30.0
        duration = duration if duration is not None else info.get("duration") or 0.0
        if hdr is None:
            hdr = bool(info.get("is_hdr"))
    t = max(0.0, float(t))
    if duration:
        t = min(t, max(0.0, duration - 1.0 / fps))
    seek = max(0.0, t - 0.5 / fps)
    chain: list[str] = []
    if hdr:
        from grade import hdr_to_sdr_chain
        chain.append(hdr_to_sdr_chain({"is_hdr": True}))
    if vf:
        chain.append(vf)
    if width or height:
        chain.append(f'scale={width or -2}:{height or -2}:flags=bicubic')
    chain.append("format=rgb24")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f'{seek:.4f}', "-i", str(video),
           "-frames:v", "1", "-an", "-vf", ",".join(chain), "-f", "image2pipe", "-c:v", "png", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f'could not extract the frame at {t:.3f}s of {Path(video).name}: {err}')
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def extract_frames(video: Path | str, times: list[float], width: int | None = None, height: int | None = None,
                   vf: str | None = None, workers: int = 4) -> list[Image.Image]:
    info = probe_video(video)
    fps, dur, hdr = info.get("fps") or 30.0, info.get("duration") or 0.0, bool(info.get("is_hdr"))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda t: extract_frame(video, t, width, height, vf, fps, dur, hdr), times))


def abspath(path: Path | str) -> Path:
    """Absolute path WITHOUT resolving symlinks (a symlinked source keeps its own folder)."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def default_verify_dir(video: Path) -> Path:
    """<edit>/verify for a file inside an edit dir, else <video dir>/edit/verify."""
    video = abspath(video)
    if video.parent.name == "edit" or (video.parent / "state.json").exists() or (video.parent / "edl.json").exists():
        return video.parent / "verify"
    return video.parent / "edit" / "verify"


def find_timeline(video: Path) -> Path | None:
    """cut.mp4 -> cut_timeline.json next to it; fallback edit/cut_timeline.json."""
    video = abspath(video)
    for cand in (video.with_name(f'{video.stem}_timeline.json'), video.parent / "cut_timeline.json"):
        if cand.exists():
            return cand
    return None


def junction_times(timeline: dict) -> tuple[list[float], list[str]]:
    fps = float(timeline.get("fps") or 30)
    segs = timeline.get("segments") or []
    times: list[float] = []
    labels: list[str] = []
    for a, b in zip(segs, segs[1:]):
        a_end = int(a.get("out_end_frame", round(float(a["out_end"]) * fps)))
        b_start = int(b.get("out_start_frame", round(float(b["out_start"]) * fps)))
        for k, lab in ((a_end - 2, f"{a['id']} penultimate"), (a_end - 1, f"{a['id']} last"),
                       (b_start, f"{b['id']} first"), (b_start + 1, f"{b['id']} second")):
            times.append(max(0, k) / fps)
            labels.append(f'{lab} · q{max(0, k)}')
    return times, labels


def fit_label(d: ImageDraw.ImageDraw, text: str, max_w: int, size: int = 16):
    """Cut the middle of a label that does not fit, never the ' · qNNN' tail."""
    font = ui_font(size)
    if d.textlength(text, font=font) <= max_w:
        return text, font
    head, sep, tail = text.rpartition(" · ")
    if not sep:
        head, tail, sep = text, "", ""
    while head and d.textlength(f'{head}…{sep}{tail}', font=font) > max_w:
        head = head[:-1]
    return (f'{head}…{sep}{tail}' if head else tail or text), font


def build_sheet(frames: list[Image.Image], labels: list[str], cols: int, title: str) -> Image.Image:
    cols = max(1, min(cols, len(frames)))
    rows = math.ceil(len(frames) / cols)
    tw, th = frames[0].size
    gap, pad, head, lab_h = 10, 18, 58, 34
    f_title = ui_font(24, bold=True)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    W = max(pad * 2 + cols * tw + (cols - 1) * gap, int(probe.textlength(title, font=f_title)) + pad * 2)
    H = head + pad + rows * (th + lab_h) + (rows - 1) * gap + pad
    sheet = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(sheet)
    size = 16
    while size > 12 and any(d.textlength(lab, font=ui_font(size)) > tw - 12 for lab in labels):
        size -= 1
    d.text((pad, 16), title, fill=FG, font=f_title)
    for i, (img, lab) in enumerate(zip(frames, labels)):
        r, c = divmod(i, cols)
        x = pad + c * (tw + gap)
        y = head + pad + r * (th + lab_h + gap)
        sheet.paste(img, (x, y))
        d.rectangle((x, y + th, x + tw - 1, y + th + lab_h - 1), fill=PANEL)
        text, font = fit_label(d, lab, tw - 12, size)
        d.text((x + 6, y + th + 8), text, fill=FG, font=font)
    return sheet


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Build a contact sheet with timestamped video frames.')
    ap.add_argument('video', type=Path, help='video source or cut.mp4')
    ap.add_argument("--times", nargs="+", type=parse_time, help="times in seconds or mm:ss.cc", dest='times')
    ap.add_argument("--junctions", action="store_true",
                    help='use the adjacent cut_timeline.json: two frames before and after each join', dest='junctions')
    ap.add_argument("--timeline", type=Path, help='timeline path (default: <name>_timeline.json beside the video)', dest='timeline')
    ap.add_argument("-o", "--output", type=Path, help='output PNG (default: edit/verify/contact_<name>.png)', dest='output')
    ap.add_argument("--cols", type=int, default=4, help='columns (default 4)', dest='cols')
    ap.add_argument("--tile-width", type=int, default=0, help='frame width in pixels (automatic by default)', dest='tile_width')
    args = ap.parse_args()

    video = abspath(args.video)
    if not video.exists():
        print(f'error: video not found: {video}', file=sys.stderr)
        return 2
    try:
        info = probe_video(video)
    except Exception as exc:  # noqa: BLE001
        print(f'error: could not read video {video.name}: {exc}', file=sys.stderr)
        return 2
    if not info.get("has_video"):
        print(f'error: {video.name} has no video stream', file=sys.stderr)
        return 2

    labels: list[str]
    single = False
    if args.junctions:
        tl_path = args.timeline or find_timeline(video)
        if not tl_path or not Path(tl_path).exists():
            print('error: timeline not found. Run render_cut.py first or pass --timeline.', file=sys.stderr)
            return 2
        timeline = load_json(tl_path)
        if len(timeline.get("segments") or []) < 2:
            # One range: no junction, but the cut still needs a look (black, crooked, cropped frames).
            single = True
            fps = float(timeline.get("fps") or info.get("fps") or 30.0)
            last = max(0, int(timeline.get("frames") or round(float(info.get("duration") or 0) * fps)) - 1)
            frame_ids = sorted({0, round(last * 0.25), round(last * 0.5), round(last * 0.75), last})
            times = [k / fps for k in frame_ids]
            labels = [f'{fmt_time(t)} · q{k}' for t, k in zip(times, frame_ids)]
            print('the cut has one interval and no joins: showing its beginning, middle, and end')
        else:
            times, labels = junction_times(timeline)
    elif args.times:
        times = list(args.times)
        fps = info.get("fps") or 30.0
        labels = [f'{fmt_time(t)} · q{int(round(t * fps))}' for t in times]
    else:
        print('error: provide --times t1 t2 ... or --junctions', file=sys.stderr)
        return 2

    dw, dh = info.get("display_width") or 1080, info.get("display_height") or 1920
    tile_w = args.tile_width or (230 if dh > dw else 400)
    tile_w -= tile_w % 2
    try:
        frames = extract_frames(video, times, width=tile_w)
    except RuntimeError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    th = max(f.size[1] for f in frames)
    frames = [f if f.size == (tile_w, th) else f.resize((tile_w, th)) for f in frames]
    title = f'{video.name} · {len(frames)} frames'
    if args.junctions and not single:
        title = f'{video.name} · joins ({len(frames) // 4})'
    elif single:
        title = f'{video.name} · one interval: beginning, middle, and end'
    sheet = build_sheet(frames, labels, 5 if single else 4 if args.junctions else args.cols, title)

    out = args.output or default_verify_dir(video) / f'contact_{video.stem}.png'
    out = abspath(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "PNG", optimize=True)
    print(f'sheet saved: {out} ({len(frames)} frames)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
