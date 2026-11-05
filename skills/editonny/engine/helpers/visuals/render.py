"""Renderer: one motion item (template + params + place + length) -> frames, an alpha clip for the
compose, previews and a contact sheet.

Transport into compose_final (chosen by measurement): one QuickTime Animation (qtrle, ARGB,
lossless) .mov per item, sized to the item's rectangle, placed with overlay at even x/y and started
with `setpts=N+<start frame>` like the insert clips. Frame-exact, cacheable by content, tiny for held
frames (qtrle only stores the lines that changed).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import engine as E
from . import registry as Rg
from .engine import VisualError

RENDER_VERSION = 2
EXIT_FRAMES = 6                 # generic exit (fade + 3% shrink) of templates with exit "fade"
EXIT_SHRINK = 0.03
INK_ALPHA = 24                  # a pixel counts as drawn ("ink") above this alpha: faint glows and shadows do not
_INK_LUT = [0] * (INK_ALPHA + 1) + [255] * (255 - INK_ALPHA)
_OWN_HASH = hashlib.sha1(b"".join(p.read_bytes() for p in (Path(E.__file__), Path(__file__)))).hexdigest()[:12]


@dataclass
class VisualItem:
    template: Rg.Template
    params: dict                 # resolved (defaults filled)
    box: E.Box
    frames: int
    region: list[float]          # [x, y, w, h] design px actually used
    region_spec: Any
    moved_by: float = 0.0        # design px the region moved to clear a face / text
    warnings: list[str] = field(default_factory=list)
    ink: tuple[float, float, float, float] | None = None   # box-local design px (measured when placing)

    @property
    def fps(self) -> int:
        return self.box.fps

    @property
    def canvas(self) -> tuple[int, int]:
        return self.box.canvas

    @property
    def key(self) -> str:
        b = self.box
        blob = json.dumps({
            "engine": [E.ENGINE_VERSION, RENDER_VERSION, _OWN_HASH],
            "template": [self.template.id, self.template.version, self.template.source_hash],
            "params": self.params, "box": [round(b.x, 4), round(b.y, 4), round(b.w, 4), round(b.h, 4), b.bleed],
            "canvas": list(b.canvas), "fps": b.fps, "frames": self.frames,
            "theme": [b.theme.name, list(b.theme.accent)], "exit": self.template.exit,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Frame:
    image: Image.Image           # RGBA
    x: int                       # render px of the image's top-left on the output canvas
    y: int


# ------------------------------------------------------------------ planning

def _accent_for(params: dict, accent: str) -> str:
    color_value = params.get("color")
    if color_value in (None, ""):
        return accent
    if isinstance(color_value, str) and not color_value.startswith("#"):
        c = E.make_theme(accent).get(E.COLOR_NAMES[color_value])
        return "#%02X%02X%02X" % c[:3]
    return str(color_value)[:7]


def plan_item(template_id: str, params: dict | None = None, *, fps: int = 30,
              canvas: Sequence[int] = (1080, 1920), region=None, duration: float | None = None,
              frames: int | None = None, accent: str = E.DEFAULT_ACCENT,
              blocked: Sequence[tuple[float, float]] | None = None,
              dirs: list[Path] | None = None) -> VisualItem:
    """Check everything and decide where and how long the item is. `blocked` = vertical bands (y0, y1) in
    design px of the canvas that the content must not touch (faces, other texts); the region slides
    vertically inside the safe area to the free place closest to where it was."""
    tpl = Rg.get(template_id, dirs)
    resolved = Rg.resolve_params(tpl, params)
    fps = int(fps)
    if fps <= 0:
        raise VisualError('fps must be positive')
    canvas = (int(canvas[0]), int(canvas[1]))
    if canvas[0] < 16 or canvas[1] < 16:
        raise VisualError(f'invalid canvas size: {canvas}')
    if frames is None:
        secs = tpl.suggested_duration(resolved) if duration is None else float(duration)
        if not math.isfinite(secs) or secs <= 0:
            raise VisualError(f'invalid duration: {duration!r}')
        frames = E.frame_of(secs, fps)
    frames = int(frames)
    if frames < 2:
        raise VisualError(f"'{tpl.id}' must last at least two frames")
    if frames > Rg.MAX_DURATION * fps:
        raise VisualError(f"'{tpl.id}' maximum duration is {Rg.MAX_DURATION:g} s")
    spec = tpl.default_region if region in (None, "", "auto") else region
    x, y, w, h = E.region_box(spec, canvas)
    warnings: list[str] = []
    sx0, sy0, sx1, sy1 = E.safe_area(canvas)
    if not isinstance(spec, str) and (y < sy0 - 0.5 or y + h > sy1 + 0.5):
        warnings.append(f"'{tpl.id}': region [{x:g}, {y:g}, {w:g}, {h:g}] exceeds the safe area ({sy0:.0f} to {sy1:.0f} px of height): the Reels interface may cover part of it")
    # without a card the text sits on the video: white letters with a dark outline read there, the dark letters
    # of the light card do not, so "theme" only matters when there is a card
    theme_value = resolved.get("theme", "dark") if resolved.get("card", True) is not False else "dark"
    # A palette-specific look (paper, pop, neon) keeps its colors despite the video's accent;
    # only an item's own "color" changes them
    own_palette = theme_value in E.PAPER or theme_value in ("pop", "neon")
    base_accent = E.DEFAULT_ACCENT if own_palette and resolved.get("color") in (None, "") else accent
    theme = E.make_theme(_accent_for(resolved, base_accent), theme_value)
    box = E.Box(x=x, y=y, w=w, h=h, scale=min(canvas) / E.REF, canvas=canvas, fps=fps, frames=frames,
                theme=theme, bleed=tpl.bleed)
    item = VisualItem(template=tpl, params=resolved, box=box, frames=frames, region=[x, y, w, h],
                      region_spec=spec, warnings=warnings)
    hook = getattr(tpl.module, "warnings", None)
    if callable(hook):
        with E.theme_fonts(theme):
            texts = hook(resolved, box) or []
        for text in texts:
            item.warnings.append(f"'{tpl.id}': {text}")
    if blocked:
        place(item, blocked)
    return item


def _ink_hook(item: VisualItem) -> tuple[float, float, float, float] | None | bool:
    """The template's own `ink(params, box)` (box-local design px), or False when it has none."""
    fn = getattr(item.template.module, "ink", None)
    if not callable(fn):
        return False
    with E.theme_fonts(item.box.theme):
        box = fn(item.params, item.box)
    if box is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def measure_ink(item: VisualItem, samples: int = 6) -> tuple[float, float, float, float] | None:
    """Union of the drawn pixels (alpha above INK_ALPHA) over a few frames (without the exit), in box-local
    design px. A template with `ink(params, box)` gives it directly (a full-screen flash is not "ink")."""
    hooked = _ink_hook(item)
    if hooked is not False:
        item.ink = hooked  # type: ignore[assignment]
        return item.ink
    n = item.frames
    last = max(0, n - 1 - (EXIT_FRAMES if item.template.exit == "fade" else 0))
    picks = sorted({int(round(last * k / max(1, samples - 1))) for k in range(samples)})
    s = item.box.scale
    union = None
    for i in picks:
        fr = render_frame(item, i, with_exit=False)
        bb = fr.image.getchannel("A").point(_INK_LUT).getbbox()
        if not bb:
            continue
        x0, y0 = (fr.x + bb[0]) / s - item.box.x, (fr.y + bb[1]) / s - item.box.y
        x1, y1 = (fr.x + bb[2]) / s - item.box.x, (fr.y + bb[3]) / s - item.box.y
        union = (x0, y0, x1, y1) if union is None else (min(union[0], x0), min(union[1], y0),
                                                        max(union[2], x1), max(union[3], y1))
    item.ink = union
    return union


def free_offset(item: VisualItem, blocked: Sequence[tuple[float, float]], gap: float = 23.0) -> float | None:
    """Vertical move (design px, from where the item is now) that puts its drawn part at the free place
    closest to where it is, inside the safe band and `gap` away from every blocked (y0, y1) band. 0 when it
    draws nothing, None when there is no free place. The item is not changed."""
    import face_layout  # noqa: PLC0415  (helpers/ is on sys.path through engine)

    ink = item.ink if item.ink is not None else measure_ink(item)
    if ink is None:
        return 0.0
    b = item.box
    sx0, sy0, sx1, sy1 = E.safe_area(b.canvas)
    top, bottom = b.y + ink[1], b.y + ink[3]
    ref = (top + bottom) / 2.0
    above, below = ref - top, bottom - ref
    bands = [(float(a), float(c)) for a, c in blocked if c > a]
    center = face_layout.free_center(ref, above, below, bands, sy0, sy1, gap)
    return None if center is None else center - ref


def move_item(item: VisualItem, dy: float) -> VisualItem:
    """Slide the item `dy` design px down (negative: up)."""
    if abs(dy) >= 0.5:
        b = item.box
        item.box = E.with_box(b, y=b.y + dy)
        item.region = [b.x, b.y + dy, b.w, b.h]
        item.moved_by += dy
    return item


def place(item: VisualItem, blocked: Sequence[tuple[float, float]], gap: float = 23.0) -> VisualItem:
    """Slide the item vertically so its drawn part clears every blocked band (face_layout.free_center)."""
    if not item.template.movable:
        return item
    dy = free_offset(item, blocked, gap)
    if dy is None:
        item.warnings.append(f"'{item.template.id}' found no face- and text-safe space; retained the requested region")
        return item
    return move_item(item, dy)


# ------------------------------------------------------------------ frames

def _normalize(item: VisualItem, out: Any, i: int) -> Frame:
    b = item.box
    if isinstance(out, E.Canvas):
        return Frame(out.image, out.origin_x, out.origin_y)
    if isinstance(out, Image.Image):
        img = out if out.mode == "RGBA" else out.convert("RGBA")
        if img.size == tuple(b.canvas):
            return Frame(img, 0, 0)
        bw, bh = int(round(b.w * b.scale)), int(round(b.h * b.scale))
        if abs(img.width - bw) <= 1 and abs(img.height - bh) <= 1:
            return Frame(img, int(round(b.x * b.scale)), int(round(b.y * b.scale)))
        raise VisualError(f"'{item.template.id}': render frame {i} returned {img.size}; expected canvas {tuple(b.canvas)}, the box ({bw}, {bh}), or a Canvas")
    raise VisualError(f"'{item.template.id}': render must return a Canvas or RGBA image (received {type(out).__name__})")


def _exit(item: VisualItem, fr: Frame, i: int) -> Frame:
    """Generic exit: the last frames fade out and shrink 3% around the box center. Only the drawn part is
    resampled (premultiplied, so transparent pixels never darken the edges)."""
    n = item.frames
    k = min(EXIT_FRAMES, n // 3)
    j = i - (n - k)
    if item.template.exit != "fade" or k <= 0 or j < 0:
        return fr
    p = E.in_quad((j + 1) / (k + 1))
    img = fr.image
    bb = img.getchannel("A").getbbox()
    if bb is None:
        return fr
    b = item.box
    cx = (b.x + b.w / 2.0) * b.scale - fr.x
    cy = (b.y + b.h / 2.0) * b.scale - fr.y
    sc = 1.0 - EXIT_SHRINK * p
    # the shrunk drawing stays between the drawn area and the center: resample only that rectangle
    x0 = int(math.floor(min(bb[0], cx + (bb[0] - cx) * sc))) - 2
    y0 = int(math.floor(min(bb[1], cy + (bb[1] - cy) * sc))) - 2
    x1 = int(math.ceil(max(bb[2], cx + (bb[2] - cx) * sc))) + 2
    y1 = int(math.ceil(max(bb[3], cy + (bb[3] - cy) * sc))) + 2
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(img.width, x1), min(img.height, y1)
    part = img.crop((x0, y0, x1, y1)).convert("RGBa")
    inv = 1.0 / sc
    lx, ly = cx - x0, cy - y0
    part = part.transform(part.size, Image.AFFINE, (inv, 0.0, lx - lx * inv, 0.0, inv, ly - ly * inv),
                          resample=Image.BICUBIC).convert("RGBA")
    a = np.asarray(part.getchannel("A"), np.float32) * (1.0 - p)
    part.putalpha(Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8), "L"))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(part, (x0, y0))
    return Frame(out, fr.x, fr.y)


def render_frame(item: VisualItem, i: int, with_exit: bool = True) -> Frame:
    if not 0 <= i < item.frames:
        raise VisualError(f'frame {i} outside the item (0 to {item.frames - 1})')
    with E.theme_fonts(item.box.theme):
        out = item.template.render(int(i), int(item.fps), item.params, item.box)
    fr = _normalize(item, out, i)
    return _exit(item, fr, i) if with_exit else fr


def iter_frames(item: VisualItem) -> Iterator[tuple[int, Frame, bool]]:
    """(index, frame, repeated) for every frame; frames after the template's settle time reuse the last one."""
    settle = item.template.settle(item.params, item.fps)
    n = item.frames
    exit_from = n - min(EXIT_FRAMES, n // 3) if item.template.exit == "fade" else n
    still_from = E.frame_of(settle, item.fps) if settle is not None else None
    held: Frame | None = None
    for i in range(n):
        if still_from is not None and still_from <= i < exit_from and held is not None:
            yield i, held, True
            continue
        fr = render_frame(item, i)
        if still_from is not None and i >= still_from and i < exit_from:
            held = fr
        yield i, fr, False


def clip_rect(item: VisualItem, first: Frame) -> tuple[int, int, int, int]:
    """(x, y, w, h) render px on the canvas that holds every frame: even origin and even size."""
    W, H = item.canvas
    x0, y0 = max(0, first.x), max(0, first.y)
    x1, y1 = min(W, first.x + first.image.width), min(H, first.y + first.image.height)
    x0, y0 = x0 // 2 * 2, y0 // 2 * 2
    x1, y1 = min(W, (x1 + 1) // 2 * 2), min(H, (y1 + 1) // 2 * 2)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise VisualError(f"'{item.template.id}' draws outside the canvas")
    return x0, y0, x1 - x0, y1 - y0


def _place_on(fr: Frame, rect: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = rect
    if (fr.x, fr.y, fr.image.width, fr.image.height) == rect:
        return fr.image
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(fr.image, (fr.x - x, fr.y - y))
    return out


def _check_inside(item: VisualItem, fr: Frame, rect: tuple[int, int, int, int], i: int) -> None:
    """Every frame must fit the rectangle of the first one (same kind of Canvas on every frame)."""
    W, H = item.canvas
    x0, y0 = max(0, fr.x), max(0, fr.y)
    x1, y1 = min(W, fr.x + fr.image.width), min(H, fr.y + fr.image.height)
    if x0 < rect[0] or y0 < rect[1] or x1 > rect[0] + rect[2] or y1 > rect[1] + rect[3]:
        raise VisualError(f"'{item.template.id}': frame {i} draws outside frame zero's bounds. Use a consistent Canvas type across frames, including full=True when full-canvas drawing is needed")


def full_frame(item: VisualItem, fr: Frame) -> Image.Image:
    W, H = item.canvas
    return _place_on(fr, (0, 0, W, H))


# ------------------------------------------------------------------ sounds

def sfx_cues(item: VisualItem, check_names: bool = False) -> list[dict]:
    """[{frame (from the item start), name, volume, why?}] inside the item, sorted by frame."""
    tpl = item.template
    fn = getattr(tpl.module, "sfx", None)
    raw = fn(item.params, item.fps, item.frames) if callable(fn) else tpl.sfx
    if not isinstance(raw, list):
        raise VisualError(f"'{tpl.id}': sfx() must return a list")
    out = []
    for k, cue in enumerate(raw):
        if not isinstance(cue, dict) or not isinstance(cue.get("name"), str):
            raise VisualError(f"'{tpl.id}': sound {k} lacks 'name'")
        if "frame" in cue:
            frame = int(cue["frame"])
        else:
            frame = E.frame_of(float(cue.get("at", 0.0)), item.fps)
        if not 0 <= frame < item.frames:
            continue
        vol = float(cue.get("volume", 1.0))
        entry = {"frame": frame, "name": cue["name"], "volume": round(min(2.0, max(0.0, vol)), 3)}
        if cue.get("length") is not None and float(cue["length"]) > 0:
            entry["length"] = round(float(cue["length"]), 4)       # seconds the sound plays after its strong point
        if cue.get("why"):
            entry["why"] = str(cue["why"])
        out.append(entry)
    if check_names:
        import make_sfx  # noqa: PLC0415
        manifest = make_sfx.load_manifest()
        for cue in out:
            if make_sfx.resolve_name(cue["name"], manifest) is None:
                raise VisualError(f"'{tpl.id}': sound effect '{cue['name']}' does not exist; use one of {', '.join(make_sfx.NAMES)})")
    return sorted(out, key=lambda c: (c["frame"], c["name"]))


# ------------------------------------------------------------------ outputs

def _ffmpeg() -> str:
    from shutil import which
    exe = which("ffmpeg")
    if not exe:
        raise VisualError('FFmpeg not found in PATH. macOS: brew install ffmpeg; Windows: winget install Gyan.FFmpeg')
    return exe


class _Pipe:
    def __init__(self, args: list[str], what: str):
        self.what = what
        self.proc = subprocess.Popen([_ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-y", *args],
                                     stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)  # type: ignore[union-attr]
        except BrokenPipeError:
            self.close()

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
        if self.proc.wait() != 0:
            raise VisualError(f'FFmpeg failed to {self.what}: {err.strip()[-400:]}', code=1)

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait()
        except OSError:
            pass


def _qtrle_args(w: int, h: int, fps: int, n: int, out: Path) -> list[str]:
    return ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f'{w}x{h}', "-framerate", str(fps), "-i", "-",
            "-c:v", "qtrle", "-pix_fmt", "argb", "-g", str(max(1, n)), "-frames:v", str(n),
            "-f", "mov", str(out)]


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(".tmp-" + path.name)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def render_clip(item: VisualItem, out_dir: Path | str, cache: bool = True) -> dict:
    """The alpha clip compose_final overlays: renders/motion/<id>_<key>.mov + .json (cached by content).

    Returns {file, json, template, x, y, w, h (render px on the canvas), frames, fps, canvas, region
    (design px), moved_by, ink (render px on the canvas or null), sfx (cues from the item start), params,
    key, cached, warnings}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = item.key
    clip = out_dir / f'{item.template.id}_{key}.mov'
    meta_path = clip.with_suffix(".json")
    if cache and clip.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("key") == key and clip.stat().st_size > 0:
                meta.update({"file": str(clip), "json": str(meta_path), "cached": True,
                             "warnings": list(item.warnings), "region": [round(v, 2) for v in item.region],
                             "moved_by": round(item.moved_by, 2)})
                return meta
        except (OSError, ValueError):
            pass
    tmp = clip.with_name(".tmp-" + clip.name)
    pipe = None
    rect = None
    ink = None
    last_bytes = None
    hooked = _ink_hook(item)
    try:
        for i, fr, repeated in iter_frames(item):
            if rect is None:
                rect = clip_rect(item, fr)
                pipe = _Pipe(_qtrle_args(rect[2], rect[3], item.fps, item.frames, tmp), 'write animation')
            if not repeated or last_bytes is None:
                _check_inside(item, fr, rect, i)
                img = _place_on(fr, rect)
                if hooked is False:
                    bb = img.getchannel("A").point(_INK_LUT).getbbox()
                    if bb:
                        ink = bb if ink is None else (min(ink[0], bb[0]), min(ink[1], bb[1]),
                                                      max(ink[2], bb[2]), max(ink[3], bb[3]))
                last_bytes = img.tobytes()
            pipe.write(last_bytes)  # type: ignore[union-attr]
        pipe.close()  # type: ignore[union-attr]
    except BaseException:
        if pipe is not None:
            pipe.kill()
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, clip)
    x, y, w, h = rect  # type: ignore[misc]
    if hooked is False:
        ink_px = [x + ink[0], y + ink[1], x + ink[2], y + ink[3]] if ink else None
    elif hooked is None:
        ink_px = None
    else:
        s, b = item.box.scale, item.box
        ink_px = [int(math.floor((b.x + hooked[0]) * s)), int(math.floor((b.y + hooked[1]) * s)),
                  int(math.ceil((b.x + hooked[2]) * s)), int(math.ceil((b.y + hooked[3]) * s))]
    meta = {
        "file": str(clip), "json": str(meta_path), "template": item.template.id, "x": x, "y": y, "w": w, "h": h,
        "frames": item.frames, "fps": item.fps, "canvas": list(item.canvas),
        "region": [round(v, 2) for v in item.region], "moved_by": round(item.moved_by, 2),
        "ink": ink_px,
        "sfx": sfx_cues(item), "params": item.params, "key": key, "cached": False, "warnings": list(item.warnings),
    }
    _atomic_json(meta_path, meta)
    return meta


def overlay_filters(clip: dict, input_index: int, base_label: str, out_label: str, start_frame: int,
                    windows: Sequence[tuple[int, int]] | None = None) -> list[str]:
    """Filter-graph lines that put a render_clip() result on the video from `start_frame` on (frame-exact).
    `windows` = [(first, end)] output frames where it shows (default: the whole item); the clip keeps its own
    timing, so a hidden stretch just skips those frames."""
    fps, n = clip["fps"], clip["frames"]
    lab = f'mo{input_index}'
    spans = [(start_frame, start_frame + n)] if windows is None else list(windows)
    enable = "+".join(f'between(n,{a},{b - 1})' for a, b in spans if b > a) or "0"
    return [
        f'[{input_index}:v]settb=1/{fps},setpts=N+{start_frame},format=rgba,scale=out_color_matrix=bt709:out_range=tv,format=yuva420p[{lab}]',
        f"[{base_label}][{lab}]overlay=x={clip['x']}:y={clip['y']}:eof_action=pass:format=auto:enable='{enable}'[{out_label}]",
    ]


def write_mov(item: VisualItem, path: Path) -> Path:
    """Transparent full-canvas QuickTime Animation .mov (for a video editor)."""
    W, H = item.canvas
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    pipe = _Pipe(_qtrle_args(W, H, item.fps, item.frames, tmp), "write the .mov")
    try:
        last = None
        for _, fr, repeated in iter_frames(item):
            if not repeated or last is None:
                last = full_frame(item, fr).tobytes()
            pipe.write(last)
        pipe.close()
    except BaseException:
        pipe.kill()
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, path)
    return path


def write_pngs(item: VisualItem, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, fr, _ in iter_frames(item):
        p = folder / f'{item.template.id}_{i:04d}.png'
        full_frame(item, fr).save(p, "PNG", compress_level=3)
        out.append(p)
    return out


def background(canvas: Sequence[int], image: str | Path | None = None) -> Image.Image:
    """A frame to judge the animation on: the given picture (cover) or a neutral talking-head mock."""
    W, H = int(canvas[0]), int(canvas[1])
    if image:
        p = Path(image)
        if not p.exists():
            raise VisualError(f'background not found: {p}')
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return ImageOps.fit(im, (W, H), Image.LANCZOS).convert("RGBA")
    y = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :]
    top, bot = np.array([62, 70, 84], np.float32), np.array([142, 128, 114], np.float32)
    rgb = top[None, None, :] * (1 - y[..., None]) + bot[None, None, :] * y[..., None]
    rgb = rgb + 14 * np.sin(x * 3.1)[..., None]
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    person = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(person)
    k = min(W, H) / 1080.0
    cx = W / 2
    head_cy = H * 0.36
    d.ellipse((cx - 175 * k, head_cy - 230 * k, cx + 175 * k, head_cy + 230 * k), fill=255)
    d.ellipse((cx - 470 * k, head_cy + 300 * k, cx + 470 * k, head_cy + 1300 * k), fill=255)
    person = person.filter(ImageFilter.GaussianBlur(10 * k))
    shade = Image.new("RGBA", (W, H), (40, 34, 32, 255))
    shade.putalpha(person.point(lambda v: v * 150 // 255))
    img.alpha_composite(shade)
    return img


def _dashed(d: ImageDraw.ImageDraw, x0, y0, x1, y1, fill, width=1, dash=10):
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return
    n = max(1, int(length // (2 * dash)))
    for k in range(n + 1):
        a = min(1.0, k * 2 * dash / length)
        b = min(1.0, (k * 2 * dash + dash) / length)
        d.line((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a, x0 + (x1 - x0) * b, y0 + (y1 - y0) * b), fill=fill, width=width)


def contact_sheet(item: VisualItem, path: Path, count: int = 8, bg: str | Path | None = None,
                  guides: bool = True, tile_width: int | None = None, cols: int = 4) -> Path:
    """`count` frames spread over the item on one PNG, with the safe area and the region drawn."""
    n = item.frames
    picks = sorted({int(round(v)) for v in np.linspace(0, n - 1, max(2, min(count, n)))})
    W, H = item.canvas
    base = background(item.canvas, bg)
    tw = tile_width or (270 if H >= W else 480)
    th = int(round(tw * H / W))
    rows = (len(picks) + cols - 1) // cols
    label_h = 26
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 8, rows * (th + label_h) + (rows + 1) * 8), (24, 24, 26))
    d_sheet = ImageDraw.Draw(sheet)
    lab_font = E.font("text_bold", 17)
    s = item.box.scale
    sx0, sy0, sx1, sy1 = (v * s for v in E.safe_area(item.canvas))
    rx, ry, rw, rh = (v * s for v in item.region)
    for k, i in enumerate(picks):
        frame = base.copy()
        frame.alpha_composite(full_frame(item, render_frame(item, i)))
        if guides:
            d = ImageDraw.Draw(frame)
            lw = max(1, int(round(2 * s)))
            for yy in (sy0, sy1):
                _dashed(d, 0, yy, W, yy, (255, 80, 80, 255), lw, int(14 * s) + 2)
            if H > W:
                rail = W - E.SAFE_ZONE_VERTICAL["right_rail"] * s
                _dashed(d, rail, H * 0.5, rail, sy1, (255, 80, 80, 255), lw, int(14 * s) + 2)
            for (a, b_), (c, e) in (((rx, ry), (rx + rw, ry)), ((rx + rw, ry), (rx + rw, ry + rh)),
                                    ((rx + rw, ry + rh), (rx, ry + rh)), ((rx, ry + rh), (rx, ry))):
                _dashed(d, a, b_, c, e, (120, 220, 255, 255), lw, int(10 * s) + 2)
        tile = frame.convert("RGB").resize((tw, th), Image.LANCZOS)
        col, row = k % cols, k // cols
        ox, oy = 8 + col * (tw + 8), 8 + row * (th + label_h + 8)
        sheet.paste(tile, (ox, oy + label_h))
        d_sheet.text((ox + 2, oy + 3), f'frame {i} · {E.format_number(i / item.fps, 2)} s', font=lab_font,
                     fill=(235, 235, 235))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, "PNG")
    return path


def write_mp4(item: VisualItem, path: Path, bg: str | Path | None = None) -> Path:
    """H.264 preview of the animation over a background (to watch it, not to compose)."""
    W, H = item.canvas
    base = background(item.canvas, bg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    pipe = _Pipe(["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f'{W}x{H}', "-framerate", str(item.fps), "-i", "-",
                  "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "18", "-colorspace", "bt709", "-color_primaries", "bt709",
                  "-color_trc", "bt709", "-color_range", "tv", "-frames:v", str(item.frames),
                  "-movflags", "+faststart", "-f", "mp4", str(tmp)], 'write MP4 preview')
    try:
        last = None
        for _, fr, repeated in iter_frames(item):
            if not repeated or last is None:
                img = base.copy()
                img.alpha_composite(full_frame(item, fr))
                last = img.convert("RGB").tobytes()
            pipe.write(last)
        pipe.close()
    except BaseException:
        pipe.kill()
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, path)
    return path


def decode_clip(path: Path | str, w: int, h: int) -> np.ndarray:
    """Frames of a clip as (n, h, w, 4) RGBA uint8 (tests and checks)."""
    proc = subprocess.run([_ffmpeg(), "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise VisualError(f"could not read {path}: {proc.stderr.decode('utf-8', 'replace')[-300:]}", code=1)
    return np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w, 4)


if __name__ == "__main__":  # pragma: no cover
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit("use helpers/render_visuals.py")
