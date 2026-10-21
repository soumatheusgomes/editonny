# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
"""Color grade presets, apply a grade to a video, or compare grades on one frame.

Usage:
    uv run python helpers/grade.py --list
    uv run python helpers/grade.py <in> -o <out> --preset vivid
    uv run python helpers/grade.py <in> -o <out> --filter "eq=contrast=1.1:saturation=1.2"
    uv run python helpers/grade.py <video> --candidates "a=eq=saturation=1.3;b=warm;original=" --frame 4.2 -o cmp.png

Imported by render_cut.py: `resolve_grade(value)` turns an EDL "grade" field (preset name or raw
ffmpeg filter) into a filter string, `check_filter(f)` validates it on a tiny test input.
Every grade runs after an 8-bit `format=yuv420p` guard (see `graded_chain`).
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from common import has_ffmpeg_filter, probe_video


def _apple_log_curve() -> str:
    """Basic Apple Log -> Rec.709 curve (no LUT): decode the log curve, soft shoulder, 709 OETF."""
    r0, rt, c = -0.05641088, 0.01, 47.28711236
    beta, gamma, delta = 0.00964052, 0.08550479, 0.69336945
    p_t = gamma * math.log2(rt + beta) + delta

    def to_linear(x: float) -> float:
        if x >= p_t:
            return 2 ** ((x - delta) / gamma) - beta
        return r0 + math.sqrt(max(0.0, x) / c)

    def oetf(v: float) -> float:
        v = min(1.0, max(0.0, v))
        return 4.5 * v if v < 0.018 else 1.099 * v ** 0.45 - 0.099

    pts = []
    last = -1.0
    for x in (0.0, 0.12, 0.16, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 1.0):
        lin = max(0.0, to_linear(x)) * 1.25          # exposure so 18% grey lands near 709 mid grey
        tm = lin * (1 + lin / 100.0) / (1 + lin)      # extended Reinhard, white point 10
        y = round(max(last + 0.001 if last >= 0 else 0.0, oetf(tm)), 4)
        y = min(y, 1.0)
        pts.append(f'{x:g}/{y:g}')
        last = y
    return "curves=interp=pchip:all='" + " ".join(pts) + "'"


# name -> (filter, English description); EDLs reference these names.
PRESETS: dict[str, tuple[str, str]] = {
    "none": ("", "No color adjustment."),
    "natural": (
        "curves=interp=pchip:master='0/0 0.1/0.093 0.5/0.515 0.9/0.92 1/1',eq=saturation=1.05",
        'Gentle contrast and faithful color; a restrained starting point.',
    ),
    "vivid": (
        "curves=interp=pchip:master='0/0 0.12/0.1 0.5/0.52 0.88/0.915 1/1',vibrance=intensity=0.2,eq=saturation=1.05",
        'Stronger contrast and color while protecting skin highlights.',
    ),
    "warm": (
        "colortemperature=temperature=4800:mix=0.75:pl=0.9,"
        "curves=interp=pchip:master='0/0 0.1/0.093 0.5/0.515 0.9/0.92 1/1',eq=saturation=1.04",
        "Warmer, inviting tones with golden skin highlights.",
    ),
    "cool": (
        "colortemperature=temperature=8500:mix=0.7:pl=0.9,"
        "curves=interp=pchip:master='0/0 0.1/0.093 0.5/0.515 0.9/0.92 1/1',eq=saturation=0.97",
        "Cooler, clean tones for a technology look.",
    ),
    "apple_log_basic": (
        _apple_log_curve() + ",eq=saturation=1.22",
        'Only for Apple Log footage; converts its flat appearance to standard color.',
    ),
}


def get_preset(name: str) -> str:
    if name not in PRESETS:
        raise KeyError(f"unknown color preset: {name!r}. Available: {', '.join(PRESETS)}")
    return PRESETS[name][0]


def resolve_grade(value: str | None) -> str:
    """EDL grade field -> ffmpeg filter string ('' = no grade). Raises ValueError for unsupported values."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text == "none":
        return ""
    if text in PRESETS:
        return PRESETS[text][0]
    if "=" in text or "," in text:
        return text
    if re.fullmatch(r"[A-Za-z0-9_]+", text) and has_ffmpeg_filter(text):
        return text
    raise ValueError(f"grade {text!r} is not a preset ({', '.join(PRESETS)}) nor an FFmpeg filter")


@lru_cache(maxsize=None)
def _has_filter(name: str) -> bool:
    return has_ffmpeg_filter(name)


def hdr_to_sdr_chain(info: dict) -> str:
    """Filters that turn an HDR frame (HLG or PQ) into SDR BT.709 8-bit; '' for SDR sources.

    Used only for SDR display derivatives: grade comparisons, contact sheets and timeline
    images. Master cuts preserve HDR through media_policy unless conversion was confirmed.
    Requires zscale+tonemap; an approximate color conversion is not an acceptable fallback.
    """
    if not info.get("is_hdr"):
        return ""
    if _has_filter("zscale") and _has_filter("tonemap"):
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    raise ValueError('HDR display conversion requires zscale and tonemap; approximate color fallback is disabled')


def graded_chain(filter_str: str) -> str:
    """8-bit guard before the grade and after it, so every grade sees and returns yuv420p."""
    if not filter_str:
        return "null"
    return f'format=yuv420p,{filter_str},format=yuv420p'


def check_filter(filter_str: str) -> str | None:
    """Run the filter on a tiny synthetic clip. Returns None when fine, else the error text."""
    if not filter_str:
        return None
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.1:r=10",
         "-vf", graded_chain(filter_str), "-f", "null", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        lines = [ln.split("] ", 1)[-1] for ln in (proc.stderr or "").splitlines() if ln.strip()]
        useful = [ln for ln in lines if "Error applying" in ln or "No such filter" in ln or "Invalid" in ln]
        return (useful or lines or ['unknown error'])[0][:400]
    return None


def parse_candidates(spec: str) -> list[tuple[str, str]]:
    """'a=<filter>;b=<preset>;original=' -> [(label, filter)]."""
    out: list[tuple[str, str]] = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            label, value = part, part          # bare preset name
        else:
            label, value = part.split("=", 1)
        label, value = label.strip(), value.strip()
        if not label:
            raise ValueError(f'unnamed candidate in {part!r} (use name=filter)')
        out.append((label, resolve_grade(value)))
    if not out:
        raise ValueError('no candidates provided in --candidates')
    return out


def candidates_montage(video: Path, candidates: list[tuple[str, str]], t: float, out: Path,
                       tile_width: int = 0) -> Path:
    from contact_sheet import BG, DIM, FG, PANEL, extract_frame, ui_font

    info = probe_video(video)
    vertical = (info.get("display_height") or 0) >= (info.get("display_width") or 0)
    tw = tile_width or (420 if vertical else 640)
    tw -= tw % 2
    fps, dur = info.get("fps") or 30.0, info.get("duration") or 0.0
    tiles: list[Image.Image] = []
    for label, flt in candidates:
        err = check_filter(flt)
        if err:
            raise ValueError(f"candidate '{label}' has an invalid filter: {err}")
        # Grade at a decent size first (grades are resolution independent), then downscale.
        # Compare grades on SDR display derivatives; this does not convert the master cut.
        work_w = min(1080, info.get("display_width") or 1080)
        work_w -= work_w % 2
        hdr = hdr_to_sdr_chain(info)
        chain = f"scale={work_w}:-2:flags=lanczos,{(hdr + ',' if hdr else '')}{graded_chain(flt)}"
        tiles.append(extract_frame(video, t, width=tw, vf=chain, fps=fps, duration=dur, hdr=False))
    th = tiles[0].size[1]
    gap, pad, head, lab_h = 12, 20, (84 if info.get("is_hdr") else 60), 64
    n = len(tiles)
    W = pad * 2 + n * tw + (n - 1) * gap
    H = head + th + lab_h + pad
    sheet = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(sheet)
    d.text((pad, 18), f'{video.name} · color comparison at {t:.2f}s', fill=FG, font=ui_font(24, bold=True))
    f_lab, f_small = ui_font(20, bold=True), ui_font(14)
    if info.get("is_hdr"):
        d.text((pad, 54), "SDR display comparison of HDR source; master color policy is unchanged", fill=DIM, font=f_small)
    for i, ((label, flt), img) in enumerate(zip(candidates, tiles)):
        x = pad + i * (tw + gap)
        sheet.paste(img.resize((tw, th)) if img.size != (tw, th) else img, (x, head))
        d.rectangle((x, head + th, x + tw - 1, head + th + lab_h - 1), fill=PANEL)
        d.text((x + 10, head + th + 8), label, fill=FG, font=f_lab)
        desc = flt or 'no filter'
        preset = next((k for k, (f, _) in PRESETS.items() if f == flt and f), None)
        if preset:
            desc = PRESETS[preset][1] if preset == label else f'preset {preset}'
        while d.textlength(desc, font=f_small) > tw - 20 and len(desc) > 6:
            desc = desc[:-4] + "…"
        d.text((x + 10, head + th + 38), desc, fill=DIM, font=f_small)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "PNG", optimize=True)
    return out


def apply_grade(src: Path, dst: Path, filter_str: str) -> None:
    from common import ensure_distinct_output
    from media_policy import encoder_args, frame_tags
    ensure_distinct_output(dst, [src])
    info = probe_video(src)
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats", "-nostdin", "-i", str(src), "-map", "0:v:0"]
    if info.get("has_audio"):
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    if filter_str and (info.get('is_hdr') or info.get('bit_depth', 8) > 8):
        raise ValueError('This creative preset requires an explicit tested SDR conversion; no automatic precision reduction')
    if filter_str:
        cmd += ['-vf', graded_chain(filter_str) + (',' + frame_tags(info) if frame_tags(info) else ''),
                *encoder_args(info), '-preset', 'medium']
    else:
        cmd += ['-c:v', 'copy']
    cmd += ['-movflags', '+faststart', str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def main() -> int:
    ap = argparse.ArgumentParser(description='List color presets, apply them to a video, or compare them on one frame.')
    ap.add_argument('input', type=Path, nargs="?", help='input video')
    ap.add_argument("-o", "--output", type=Path, help='output video, or PNG with --candidates', dest='output')
    ap.add_argument("--list", action="store_true", help="list the presets", dest='list')
    ap.add_argument("--preset", help="preset name (see --list)", dest='preset')
    ap.add_argument("--filter", help="raw FFmpeg filter, e.g. eq=contrast=1.1", dest='filter')
    ap.add_argument("--candidates", help='side-by-side comparison: "a=<filter>;b=<preset>;original="', dest='candidates')
    ap.add_argument("--frame", default="0", help='frame time for --candidates in seconds (default 0)', dest='frame')
    ap.add_argument("--tile-width", type=int, default=0, help='width of each comparison frame', dest='tile_width')
    args = ap.parse_args()

    if args.list:
        for name, (flt, desc) in PRESETS.items():
            print(f'{name:18s} {desc}')
            if flt:
                print(f"{'':18s} {flt}")
        return 0

    if not args.input:
        ap.print_usage(sys.stderr)
        print('error: provide an input video or use --list', file=sys.stderr)
        return 2
    from contact_sheet import abspath, default_verify_dir

    src = abspath(args.input)
    if not src.exists():
        print(f'error: video not found: {src}', file=sys.stderr)
        return 2

    if args.candidates is not None:
        from contact_sheet import parse_time
        try:
            t = parse_time(args.frame)
            cands = parse_candidates(args.candidates)
            out = abspath(args.output) if args.output else default_verify_dir(src) / f'colors_{src.stem}.png'
            candidates_montage(src, cands, t, out, args.tile_width)
        except (ValueError, RuntimeError, argparse.ArgumentTypeError) as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 2
        print(f'comparison saved: {out} ({len(cands)} candidates)')
        return 0

    if not args.output:
        print('error: provide output with -o', file=sys.stderr)
        return 2
    if args.preset and args.filter:
        print('error: use either --preset or --filter, not both', file=sys.stderr)
        return 2
    try:
        flt = get_preset(args.preset) if args.preset else resolve_grade(args.filter or "")
    except (KeyError, ValueError) as exc:
        print(f'error: {(exc.args[0] if exc.args else exc)}', file=sys.stderr)
        return 2
    err = check_filter(flt)
    if err:
        print(f'error: invalid filter: {err}', file=sys.stderr)
        return 2
    print(f"applying color to {src.name}: {flt or 'no filter; re-encode only'}")
    try:
        apply_grade(src, abspath(args.output), flt)
    except RuntimeError as exc:
        print(f'error: FFmpeg failed: {exc}', file=sys.stderr)
        return 1
    print(f'ready: {abspath(args.output)}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
