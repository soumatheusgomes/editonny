"""Person cutout without a green screen: the speaker cut out of the cut and placed over a
background, as the base layer of compose_final.py.

edit_data.json:
    "edit_type": "greenScreen",
    "greenScreen": {"position": "right" | "left" | "center", "height": 0.55,
                   "outline": {"color": "#FFFFFF", "width": 8} | null, "shadow": true,
                   "background": [{"src": "assets/x.mp4" | "assets/y.png" | "#101010", "start": 0, "end": 12.3,
                              "ken_burns": true, "darken": 0.15, "blur": 0,
                              "fit": "auto" | "cover" | "contain"}],
                   "blurOriginalBackground": false, "withoutFace": "original" | "cutout", "mirror": false}
    "inserts": [{"type": "split_top" | "split_bottom", ...}]   -> backgrounds too (add_insert_backgrounds)

How a frame is made (numpy + OpenCV, several frames at once in threads):
  - background track: the items of "background" are the base; the split_top / split_bottom inserts of the edit become
    background items that win during their own [start, end) (no split panel). A later item wins where two of
    the same kind overlap; a gap keeps the item before it; before the first item, the blurred original with
    blurOriginalBackground, else a dark color.
  - fit: "cover" fills the screen (cropping; a page taller than the screen keeps its top), "contain" shows
    the whole picture or video, sharp, scaled to the width and placed in the stage above the person (the part of
    the screen the person does not cover, minus a band for auto captions when there is room) over a blurred and
    darkened cover copy of itself; "auto" is cover when the shape is close to the screen's (or taller), else
    contain. Ken Burns is an eased 6% zoom over the item's time (at most twice that time when a gap holds it,
    then it holds). Videos start at their own frame 0 and play untouched, except empty frames at the start or
    end of the clip (scan_video): the background right before it dissolves into an empty start while the clip
    builds up (with nothing before it, the first full frame is held there) and an empty end holds the last full
    frame; only a video held past its end loops, dissolving from its last frame
    into the new pass (no jump, no freeze). A video shown whole with a plain border (a studio, a flat color)
    stays wide (down to QUIET_MIN of the width, to leave the captions a band above the head), its main area
    (the rows that hold most of its detail over the whole clip) placed in the stage, and its border is carried
    out to the screen edges, so it has no box around it; the part that does not fit above the head goes behind
    the captions and the person.
  - person (cut ranges where face_layout found a face, or every range with "withoutFace": "cutout"): the frame
    of cut.mp4 times the cached matte (helpers/matte.py; on a tripod take, without the room its RoomPlate
    recognizes where the matte is unsure, and with that room's color out of its soft edge), scaled so the
    person, from the top of the head to
    the bottom of the screen, is `height` of the screen height (never wider than 72% of the screen in a
    corner, or than the screen in the center, before the zoom; a centered tight shot cut on both sides grows up to
    CENTER_FILL to fill the width), flush with the bottom. In a corner the side the camera cut sits on the screen
    edge; in the center a cut side never lands inside the screen when the frame is wide enough; in a corner, a
    segment cut on both sides fills the screen width (up to CORNER_FILL times its corner size), a little lower
    (up to FILL_DROP of the camera frame goes below the screen), so no cut lands inside the screen. A segment whose
    face is cut by a side of the camera goes to that side (a face is never sliced in the middle of the screen). A
    cut edge that still falls inside the screen gets a fade only on the rows the body really touches (SIDE_FADE
    on a side, so an arm or shoulder dissolves instead of ending in a straight line; a short one on the top; the
    person, its outline and its shadow fade together, so the other rows stay solid and the outline never shows
    through).
    The outline follows the outer body only: a gap it encloses (between an arm and the body, counting the
    bottom and the cut sides as closed) shows the background with no outline around it.
    zoom_cuts and push_in scale the person (anchored on that edge, or on the head for center). "mirror" flips
    the person segments (not the ones shown as recorded). Outline and shadow are drawn at half size and blended
    with cv2.blendLinear.
  - ranges without a face ("withoutFace": "original", the default) show the cut frame itself, full screen,
    with the zoom of the camera section (screen recordings, product close-ups).
The result is one H.264 clip (renders/<full|preview>/green_screen/tv_<key>.mp4, cached by everything that goes
into it) that compose_final.py uses in place of cut.mp4.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

import matte as M

TV_VERSION = "tv5"                  # bump to redo cached composites
POSITIONS = ("right", "left", "center")
POSITION_ALIASES = {name: name for name in POSITIONS}
DEFAULT_HEIGHT = {"right": 0.55, "left": 0.55, "center": 0.75}
HEIGHT_RANGE = (0.3, 1.0)
CORNER_MAX_WIDTH = 0.72             # a corner person is never wider than this share of the screen (before the zoom)
CENTER_MAX_WIDTH = 1.0              # a centered person is never wider than the screen (a tight shot is not enlarged)
CENTER_FILL = 1.15                  # ...except a shot cut on both sides, grown up to this much to fill the width
CORNER_FILL = 1.6                   # a corner person cut on both sides grows up to this much to fill the width
FILL_DROP = 0.2                     # ...and loses up to this share of the bottom of the camera frame, so the head
                                    # stays where the corner size puts it (the images above keep their room)
FACE_EDGE = 0.015                   # a face box this close to a side of the camera frame is cut by it
INNER_CUT_WARN = 0.4                # share of the person time with a body cut inside the screen that gets advice
MAX_SCALE = 2.0                     # never enlarge the camera frame more than this
SIDE_MARGIN = 40                    # px (1080 wide) between a person that is not cut by the camera and the edge
EDGE_FADE = 28                      # px (1080 wide) of fade where the top of the camera frame cuts the body
SIDE_FADE = 90                      # px (1080 wide) of fade where a side of the camera frame cuts the body inside
                                    # the screen (the arm or shoulder dissolves instead of ending in a straight line)
TOUCH_FLUSH = 0.2                   # share of frames touching a side for that side to sit on the screen edge
OUTLINE_DEFAULT = {"color": "#FFFFFF", "width": 8}
OUTLINE_MAX = 40
SHADOW = {"alpha": 0.5, "blur": 10.0, "dx": 6.0, "dy": 12.0}     # px on 1080 wide
DARK_DEFAULT = "#101010"
DARKEN_DEFAULT = 0.15
DARKEN_MAX = 0.9
BLUR_MAX = 80.0
ORIGINAL_BLUR = {"sigma": 40.0, "darken": 0.35}
BACKDROP = {"sigma": 70.0, "darken": 0.55, "saturation": 0.55, "feather": 36.0}   # behind a picture shown whole
KB_DRIFT = 0.06
KB_HOLD_AFTER = 2.0                 # a held gap stretches the Ken Burns to at most this many times the item's time
FIT_RATIO = 1.25                    # auto: a shape this close to the screen's (or taller) fills it, else it is whole
FITS = ("auto", "cover", "contain")
FIT_ALIASES = {name: name for name in FITS}
STAGE_TOP = 120 / 1920              # the stage for a whole picture starts this far down (the app bar sits above it)
STAGE_GAP = 0.012                   # share of the height between a whole picture and the head (and the captions)
WHOLE_MIN = 0.5                     # a whole picture shrinks to fit the stage down to this share of the width
RESERVE_KEEP = 0.75                 # the stage leaves room for captions when the picture keeps this share of its size
LOOP_FADE = 0.4                     # s of crossfade where a background video loops
SCAN_VERSION = "scan3"              # bump when scan_video changes (its cache is redone)
SCAN_SIDE = 160                     # px of the long side the videos are scanned at
SCAN_DETAIL = 24                    # 0-255: a pixel this far from its blurred surroundings is detail
SCAN_EMPTY = (0.003, 0.15, 0.5)     # a frame with less detail than max(abs, share of the clip's median) is empty;
                                    # next to an empty first or last frame, the ones under the last share still
                                    # build up or fade out (they count as empty too)
SCAN_FADE = (0.02, 0.65)            # a clip that ends empty after a steady fall (noise allowed: this share of the
                                    # median) holds the last frame that still has this share of the fall's peak
SCAN_ROW = 0.01                     # share of detail (over the clip, and 10% of the busiest) a row or column
                                    # needs to be main area
SCAN_MASS = 0.85                    # ...and the main rows are the band that holds this share of the detail (a thin
                                    # cable or a glow at the edge of the clip is not its main area)
QUIET_MIN = 0.75                    # a plain-border video shrinks down to this share of the width (its border is
                                    # carried out, so it has no box) to leave the captions a band above the head
QUIET_RING = (0.06, 0.04)           # top and bottom bands (share of the height) with less detail: a plain border
EXTEND = {"blur": 48.0, "fall": 320.0, "darken": 0.3}   # px on 1080 wide: the plain border carried to the screen
PLATE_VERSION = "placa1"            # bump when the room plate changes (its cache is redone)
PLATE_ENV = "EDITONNY_BACKGROUND_PLATE"   # =0 turns the room plate off (to compare)
PLATE = {
    "step": 2,                      # every other frame of a take feeds its plate
    "bg": 8,                        # matte (0-255) under this, `fringe` px away from the body, is the room
    "fringe": 4,
    "n": [2.0, 8.0],                # frames that saw the room there: none trusted .. fully trusted
    "std": [6.0, 12.0],             # spread of the room color (noise, a 1 px drift, a screen): trusted .. not
    "static": 6.0,                  # median spread above this: the camera moved, no plate
    "known": 0.08,                  # share of the frame the plate must know
    "blur": 0.7, "win": 5,          # px (plate size): blur of the frame and the plate, window of the comparison
    "tol": 4.0, "width": 8.0,       # color distance (0-255) that still is the room (plus the spread there)
    "unsure": [0.8, 0.98],          # matte values the model was not sure of: only there the room goes
    "near": 8.0, "unlike": [18.0, 32.0],   # the body color nearby (px), how far a dark room color must be from it
    "bright": [45.0, 70.0],         # a room brighter than this needs no such distance
    "halo": [30.0, 50.0],           # a room this much brighter than the body next to it is a halo (per pixel)
    "on": 0.5,                      # evidence that counts
    "unsure_px": 7, "round_px": 5, "soft": 1.5,   # narrowest piece (px), rounding of the pieces, blur of their
                                                  # edge
    "edge": [8, 247], "edge_floor": 0.35,         # soft edge pixels (0-255) whose color is cleaned of the room
}
FIELDS = ("position", "height", "outline", "shadow", "background", "blurOriginalBackground", "withoutFace", "mirror")
BG_FIELDS = ("src", "start", "end", "ken_burns", "darken", "blur", "fit")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".gif", ".mts"}
EXAMPLE = '"greenScreen": {"position": "right", "background": [{"src": "assets/page.png"}]}'
ALIASES = {}
SPLIT_LABEL = {"split_top": "upper image", "split_bottom": "lower image"}


def _hint(name: str, fields) -> str:
    near = ALIASES.get(str(name).strip().lower())
    if near not in fields:
        import difflib  # noqa: PLC0415
        close = difflib.get_close_matches(str(name), list(fields), n=1, cutoff=0.6)
        near = close[0] if close else None
    return f' Did you mean "{near}"?' if near else ""


class CutoutError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _cv2():
    return M._cv2()


def hex_rgb(value) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or not value.strip().startswith("#"):
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 0.5 - 0.5 * math.cos(math.pi * p)


# ================================================================== config

@dataclass
class Background:
    idx: int                      # 1-based position in "background" (or in "inserts" when insert is set)
    kind: str                     # color | image | video
    src: Path | None
    color: tuple[int, int, int] | None
    f0: int
    f1: int                       # as asked (a gap after it is still this item, see runs)
    ken_burns: bool
    darken: float
    blur: float
    asked: str = ""
    fit_asked: str = "auto"
    fit: str = "cover"           # cover | contain (resolved)
    size: tuple[int, int] | None = None                    # the source's width and height (upright)
    insert: int | None = None     # 1-based insert number: a split_top / split_bottom insert shown as background
    insert_type: str | None = None
    content: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)   # main area of the picture (fractions)
    place: tuple[int, int, int, int] | None = None         # contain: x, y, w, h of the sharp copy (render px)
    stage: tuple[float, float] | None = None               # contain: its fitted band (render px)
    quiet: bool = False           # video with a plain border (a studio, a flat color): it melts into the screen
    lead: int = 0                 # video: empty frames at the start of a pass (the first full one shows instead)
    tail: int = 0                 # video: empty frames at the end of a pass (the last full one shows instead)
    pass_frames: int = 0          # video: frames of one pass at the edit fps (0 = not scanned)

    @property
    def name(self) -> str:
        return f'insert {self.insert}' if self.insert else f'background {self.idx}'

    @property
    def where(self) -> str:
        return f'inserts[{self.idx - 1}]' if self.insert else f'greenScreen.background[{self.idx - 1}]'

    def content_rect(self, W: int, H: int) -> tuple[float, float, float, float] | None:
        """Where the main area of a whole picture lands (render px), grown by the Ken Burns zoom."""
        if self.fit != "contain" or self.place is None:
            return None
        x, y, w, h = self.place
        c = self.content
        x0, y0, x1, y1 = x + c[0] * w, y + c[1] * h, x + c[2] * w, y + c[3] * h
        if self.ken_burns:
            g = KB_DRIFT / 2.0
            dx, dy = (x1 - x0) * g, (y1 - y0) * g
            x0, y0, x1, y1 = x0 - dx, y0 - dy, x1 + dx, y1 + dy
        return (max(0.0, x0), max(0.0, y0), min(float(W), x1), min(float(H), y1))

    def signature(self) -> list:
        sig = None
        if self.src is not None:
            st = self.src.stat()
            sig = [str(self.src), st.st_size, st.st_mtime_ns]
        return [self.idx, self.insert, self.kind, sig, self.color, self.f0, self.f1, self.ken_burns,
                round(self.darken, 4), round(self.blur, 3), self.fit, self.size,
                [round(v, 4) for v in self.content], self.place, self.quiet, self.lead, self.tail,
                self.pass_frames]


def average_size(path: Path | None, kind: str) -> tuple[int, int] | None:
    """Upright width and height of an image or video (None when it cannot be read)."""
    if path is None or kind == "color":
        return None
    try:
        if kind == "image":
            from PIL import Image  # noqa: PLC0415
            with Image.open(path) as im:
                w, h = im.size
                try:
                    orient = im.getexif().get(274, 1)
                except Exception:  # noqa: BLE001
                    orient = 1
            return (h, w) if orient in (5, 6, 7, 8) else (w, h)
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height:stream_side_data=rotation", "-of", "json", str(path)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        st = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        rot = 0
        for sd in st.get("side_data_list") or []:
            if "rotation" in sd:
                rot = abs(int(round(float(sd["rotation"])))) % 180
        if w <= 0 or h <= 0:
            return None
        return (h, w) if rot == 90 else (w, h)
    except (OSError, ValueError, TypeError, IndexError):
        return None


def choose_fit(asked: str, kind: str, size: tuple[int, int] | None, screen: tuple[int, int]) -> str:
    """fit "auto": cover when the shape is close to the screen's (a page taller than a vertical screen too),
    contain otherwise (a wide print, a square clip)."""
    if kind == "color":
        return "cover"
    if asked in ("cover", "contain"):
        return asked
    if not size:
        return "cover"
    ar, car = size[0] / max(1, size[1]), screen[0] / max(1, screen[1])
    if car / FIT_RATIO <= ar <= car * FIT_RATIO:
        return "cover"
    if ar < car / FIT_RATIO and car < 1.0:
        return "cover"                      # a long page on a vertical screen: fill it and keep the top
    return "contain"


def _fit_value(value, where: str) -> str:
    if value is None:
        return "auto"
    fit = FIT_ALIASES.get(value.strip().lower()) if isinstance(value, str) else None
    if fit is None:
        raise CutoutError(f'{where}.fit must be "auto", "cover", or "contain" (received {_json(value)})')
    return fit


def _look_values(item: dict, where: str, kind: str, fit: str, warnings: list[str]) -> tuple[float, float]:
    """darken and blur of a background item (the default darkening is 0 for a whole picture and a color)."""
    default = 0.0 if (kind == "color" or fit == "contain") else DARKEN_DEFAULT
    darken = item.get("darken", default)
    dk = _num(darken)
    if dk is None or dk < 0 or dk > 1:
        raise CutoutError(f'{where}.darken must be between 0 and {DARKEN_MAX}; zero preserves the original (received {_json(darken)})')
    if dk > DARKEN_MAX:
        warnings.append(f'{where}.darken {dk:g} was limited to {DARKEN_MAX}')
        dk = DARKEN_MAX
    blur = _num(item.get("blur", 0))
    if blur is None or blur < 0:
        raise CutoutError(f"{where}.blur must be between 0 and {BLUR_MAX:g} (blur pixels; received {_json(item.get('blur'))})")
    return dk, min(BLUR_MAX, blur)


@dataclass
class Config:
    position: str
    height: float
    height_asked: bool
    outline: tuple[tuple[int, int, int], float] | None     # (rgb, width px on 1080 wide)
    shadow: bool
    backgrounds: list[Background]
    blurred_original: bool
    in_face: str                                            # original | cutout
    mirror: bool = False                                    # mirror: person segments flipped horizontally
    runs: list[tuple[int, int, int | None]] = field(default_factory=list)   # (f0, f1, background index or None)
    warnings: list[str] = field(default_factory=list)
    run_notes: list[str] = field(default_factory=list)      # the warnings about the runs (also in warnings)
    screen: tuple[int, int] = (1080, 1920)

    @property
    def from_inserts(self) -> list[Background]:
        return [b for b in self.backgrounds if b.insert]


def parse_config(section, fps: int, nf: int, resolve: Callable[[str, str], tuple[Path, str]],
                 fr: Callable[[float], int], screen: tuple[int, int] = (1080, 1920),
                 probe: Callable[[Path | None, str], tuple[int, int] | None] = average_size) -> Config:
    """Check "greenScreen" (exit 2 on a mistake, before anything renders)."""
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise CutoutError(f'greenScreen must be an object, for example {EXAMPLE})')
    unknown = [k for k in section if k not in FIELDS]
    if unknown:
        raise CutoutError(f"greenScreen.{unknown[0]} does not exist.{_hint(unknown[0], FIELDS)} Fields: {', '.join(FIELDS)}")
    warnings: list[str] = []
    raw_pos = section.get("position", "right")
    key = raw_pos.strip().lower() if isinstance(raw_pos, str) else None
    position = POSITION_ALIASES.get(key or "")
    if position is None:
        raise CutoutError(f'greenScreen.position must be "right", "left", or "center" (received {_json(raw_pos)})')
    height_asked = section.get("height") is not None
    height = DEFAULT_HEIGHT[position]
    if height_asked:
        h = _num(section.get("height"))
        if h is None or h <= 0:
            raise CutoutError(f"greenScreen.height must be the fraction of canvas height occupied by the person, from {HEIGHT_RANGE[0]} to {HEIGHT_RANGE[1]} (ex: 0.55; received {_json(section.get('height'))})")
        if h > 1.0 and h <= 100:
            h = h / 100.0 if h > 1.5 else 1.0                 # "55" read as percent
        if not HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]:
            clipped = min(HEIGHT_RANGE[1], max(HEIGHT_RANGE[0], h))
            warnings.append(f'greenScreen.height {h:g} was limited to {clipped:g} (use from {HEIGHT_RANGE[0]} to {HEIGHT_RANGE[1]})')
            h = clipped
        height = h
    outline_raw = section.get("outline", OUTLINE_DEFAULT)
    outline = None
    if outline_raw is True:
        outline_raw = OUTLINE_DEFAULT
    if outline_raw not in (None, False):
        if not isinstance(outline_raw, dict) or set(outline_raw) - {"color", "width"}:
            raise CutoutError(f'greenScreen.outline must be {{"color": "#FFFFFF", "width": 8}} or null (received {_json(outline_raw)})')
        color = hex_rgb(outline_raw.get("color", OUTLINE_DEFAULT["color"]))
        if color is None:
            raise CutoutError(f"greenScreen.outline.color must be #RRGGBB (received {_json(outline_raw.get('color'))})")
        width = _num(outline_raw.get("width", OUTLINE_DEFAULT["width"]))
        if width is None or width < 0:
            raise CutoutError(f"greenScreen.outline.width must be pixels from 0 to {OUTLINE_MAX} (received {_json(outline_raw.get('width'))})")
        if width > OUTLINE_MAX:
            warnings.append(f'greenScreen.outline.width {width:g} was limited to {OUTLINE_MAX}')
            width = float(OUTLINE_MAX)
        outline = (color, float(width)) if width > 0 else None
    shadow = section.get("shadow", True)
    if not isinstance(shadow, bool):
        raise CutoutError(f'greenScreen.shadow must be true or false (received {_json(shadow)})')
    blurred = section.get("blurOriginalBackground", False)
    if not isinstance(blurred, bool):
        raise CutoutError(f'greenScreen.blurOriginalBackground must be true or false (received {_json(blurred)})')
    in_face = section.get("withoutFace", "original")
    if in_face not in ("original", "cutout"):
        raise CutoutError(f'greenScreen.withoutFace must be "original" or "cutout" (received {_json(in_face)})')
    mirror = section.get("mirror", False)
    if not isinstance(mirror, bool):
        raise CutoutError(f'greenScreen.mirror must be true or false; true mirrors the speaker (received {_json(mirror)})')
    background_value = section.get("background")
    if background_value is None:
        background_value = []
    if isinstance(background_value, (str, dict)):
        background_value = [background_value]
    if not isinstance(background_value, list):
        raise CutoutError('greenScreen.background must be a list, for example [{"src": "assets/page.png", "start": 0, "end": 8}]')
    items: list[Background] = []
    prev_end = 0.0
    for i, item in enumerate(background_value):
        where = f'greenScreen.background[{i}]'
        if isinstance(item, str):
            item = {"src": item}
        if not isinstance(item, dict):
            raise CutoutError(f'{where} must be an object, for example {{"src": "assets/page.png", "start": 0}}')
        bad = [k for k in item if k not in BG_FIELDS]
        if bad:
            raise CutoutError(f"{where}.{bad[0]} does not exist.{_hint(bad[0], BG_FIELDS)} Fields: {', '.join(BG_FIELDS)}")
        src = item.get("src")
        if not isinstance(src, str) or not src.strip():
            raise CutoutError(f'{where} requires "src": a local image/video file or a hex color')
        color = hex_rgb(src) if src.strip().startswith("#") else None
        if src.strip().startswith("#") and color is None:
            raise CutoutError(f'{where}.src has invalid color {_json(src)} (use #RRGGBB)')
        path, kind = (None, "color") if color else resolve(src, f'{where}.src')
        start = item.get("start")
        if start is None:
            t0 = prev_end
        else:
            t0 = _num(start)
            if t0 is None or t0 < 0:
                raise CutoutError(f'{where}.start must be nonnegative seconds (received {_json(start)})')
        end = item.get("end")
        t1 = None
        if end is not None:
            t1 = _num(end)
            if t1 is None or t1 <= t0:
                raise CutoutError(f'{where}.end must be seconds greater than start ({t0:g}) (received {_json(end)})')
        kb = item.get("ken_burns", kind == "image")
        if not isinstance(kb, bool):
            raise CutoutError(f'{where}.ken_burns must be true or false (received {_json(kb)})')
        fit_asked = _fit_value(item.get("fit"), where)
        size = probe(path, kind) if kind != "color" else None
        fit = choose_fit(fit_asked, kind, size, screen)
        dk, blur = _look_values(item, where, kind, fit, warnings)
        f0 = fr(t0)
        f1 = fr(t1) if t1 is not None else None
        if f0 >= nf:
            warnings.append(f'{where} starts after the video and was skipped')
            prev_end = t1 if t1 is not None else t0
            continue
        items.append(Background(idx=i + 1, kind=kind, src=path, color=color, f0=f0, f1=f1 if f1 is not None else -1,
                                ken_burns=kb and kind != "color", darken=dk, blur=blur, asked=src.strip(),
                                fit_asked=fit_asked, fit=fit, size=size))
        prev_end = t1 if t1 is not None else t0
    # an item without "end" lasts until the next item starts (or the end of the video)
    for k, b in enumerate(items):
        if b.f1 < 0:
            later = [o.f0 for o in items[k + 1:] if o.f0 > b.f0]
            b.f1 = min(later) if later else nf
        b.f1 = min(nf, b.f1)
        if b.f1 - b.f0 < 1:
            warnings.append(f'greenScreen.background[{b.idx - 1}] lasts fewer than one frame and was ignored')
    items = [b for b in items if b.f1 > b.f0]
    for k, a in enumerate(items):
        for b in items[k + 1:]:
            if b.f0 < a.f1 and a.f0 < b.f1:
                warnings.append(f'backgrounds {a.idx} and {b.idx} overlap: {b.idx} appears later in the list and is drawn on top')
    cfg = Config(position=position, height=height, height_asked=height_asked, outline=outline, shadow=shadow,
                 backgrounds=items, blurred_original=blurred, in_face=in_face, mirror=mirror, warnings=warnings,
                 screen=screen)
    set_runs(cfg, nf, fps)
    return cfg


def set_runs(cfg: Config, nf: int, fps: int) -> None:
    """(Re)compute the runs and their warnings (after the split inserts joined the track)."""
    for note in cfg.run_notes:
        if note in cfg.warnings:
            cfg.warnings.remove(note)
    notes: list[str] = []
    items = cfg.backgrounds
    cfg.runs = background_runs(items, nf)
    if not items and not cfg.blurred_original:
        notes.append('greenScreen has no background; using a dark fill. Provide supporting media inserts, background entries, or blurOriginalBackground')
    elif items and cfg.runs and cfg.runs[0][2] is None:
        what = 'blurred original video' if cfg.blurred_original else "a dark background"
        notes.append(f'the first background starts at {cfg.runs[0][1] / fps:.2f}s: before it use {what}')
    cfg.run_notes = notes
    cfg.warnings.extend(notes)


def add_insert_backgrounds(cfg: Config, inserts: list[dict], nf: int, fps: int,
                           probe: Callable[[Path | None, str], tuple[int, int] | None] = average_size) -> list[Background]:
    """split_top / split_bottom inserts -> background items that win over greenScreen.background during their own frames.

    inserts: [{"insert": 1-based number, "type", "src" (Path), "kind", "f0", "f1", "ken_burns", "item" (the raw
    dict, for darken / blur / fit)}]."""
    added = []
    for it in inserts:
        where = f"inserts[{it['insert'] - 1}]"
        raw = it.get("item") or {}
        fit_asked = _fit_value(raw.get("fit"), where)
        size = probe(it["src"], it["kind"])
        fit = choose_fit(fit_asked, it["kind"], size, cfg.screen)
        dk, blur = _look_values(raw, where, it["kind"], fit, cfg.warnings)
        f0, f1 = int(it["f0"]), min(nf, int(it["f1"]))
        if f1 <= f0:
            continue
        b = Background(idx=it["insert"], kind=it["kind"], src=it["src"], color=None, f0=f0, f1=f1,
                       ken_burns=bool(it["ken_burns"]), darken=dk, blur=blur, asked=str(raw.get("src") or it["src"]),
                       fit_asked=fit_asked, fit=fit, size=size, insert=it["insert"], insert_type=it["type"])
        added.append(b)
    # Background items first; an insert drawn later wins during its own frames.
    cfg.backgrounds = [b for b in cfg.backgrounds if not b.insert] + added
    set_runs(cfg, nf, fps)
    return added


def background_runs(items: list[Background], nf: int) -> list[tuple[int, int, int | None]]:
    """(f0, f1, index in items or None): a later item wins; a gap keeps the item before it."""
    owner = np.full(nf, -1, np.int32)
    for k, b in enumerate(items):
        owner[b.f0:b.f1] = k
    last = -1
    for f in range(nf):          # gaps: hold the previous background
        if owner[f] < 0:
            owner[f] = last
        else:
            last = owner[f]
    runs: list[tuple[int, int, int | None]] = []
    start = 0
    for f in range(1, nf + 1):
        if f == nf or owner[f] != owner[start]:
            runs.append((start, f, int(owner[start]) if owner[start] >= 0 else None))
            start = f
    return runs


# ================================================================== placement

@dataclass
class Segment:
    f0: int
    f1: int
    mode: str                        # person | original
    zoom: float = 1.0
    zoom_max: float = 1.0
    push: float = 0.0                # push_in amount (0 = none)
    focus_y: float = 0.4             # eye line of the cut frame (zoom anchor of an original segment)
    stats: dict | None = None
    scale: float = 1.0               # camera frame -> screen, before the zoom
    flush_left: bool = False
    flush_right: bool = False
    face: dict | None = None         # face_layout face / zone / core (fractions of the cut frame)
    side: str | None = None          # left | right: the segment sits on its cropped side
    fill: bool = False               # corner, cut on both sides: the camera frame fills the screen width
    drop: float = 0.0                # fill: share of the camera frame below the bottom of the screen
    note: str = ""

    @property
    def frames(self) -> int:
        return self.f1 - self.f0

    def zoom_at(self, f: int) -> float:
        if self.push <= 0 or self.frames <= 1:
            return self.zoom
        return self.zoom * (1.0 + self.push * ease((f - self.f0) / (self.frames - 1)))


def base_scale(cfg: Config, st: dict) -> float:
    top = float(st.get("top", 0.0))
    s = cfg.height / max(0.05, 1.0 - top)
    width = max(0.05, float(st.get("right", 1.0)) - float(st.get("left", 0.0)))
    s = min(s, (CENTER_MAX_WIDTH if cfg.position == "center" else CORNER_MAX_WIDTH) / width)
    if (cfg.position == "center" and float(st.get("touch_left", 0)) >= TOUCH_FLUSH
            and float(st.get("touch_right", 0)) >= TOUCH_FLUSH and s < 1.0 and s * CENTER_FILL >= 1.0):
        s = 1.0                  # cut on both sides: fill the width instead of showing two cut edges
    return min(MAX_SCALE, max(0.05, s))


def mirror_stats(st: dict | None) -> dict | None:
    """Placement numbers of a flipped camera frame."""
    if not st or not st.get("person_frames"):
        return st
    out = dict(st)
    out["left"] = round(1.0 - float(st.get("right", 1.0)), 5)
    out["right"] = round(1.0 - float(st.get("left", 0.0)), 5)
    out["head_cx"] = round(1.0 - float(st.get("head_cx", 0.5)), 5)
    out["touch_left"], out["touch_right"] = st.get("touch_right", 0), st.get("touch_left", 0)
    return out


def mirror_face(face: dict | None) -> dict | None:
    """face_layout segment (face / zone / core boxes) of a flipped camera frame."""
    if not isinstance(face, dict):
        return face
    out = dict(face)
    for key in ("face", "zone", "core"):
        r = face.get(key)
        if isinstance(r, dict) and "x0" in r and "x1" in r:
            out[key] = {**r, "x0": round(1.0 - float(r["x1"]), 5), "x1": round(1.0 - float(r["x0"]), 5)}
    return out


def face_cut_sides(seg: "Segment") -> tuple[bool, bool]:
    """(left, right): the detected face touches that side of the camera frame."""
    f = seg.face.get("face") if isinstance(seg.face, dict) else None
    if not isinstance(f, dict):
        return False, False
    try:
        return float(f.get("x0", 0.5)) <= FACE_EDGE, float(f.get("x1", 0.5)) >= 1.0 - FACE_EDGE
    except (TypeError, ValueError):
        return False, False


def plan_segments(cfg: Config, segs: list[Segment]) -> None:
    """Scale and flush sides per person segment; segments close to the typical size share it (no jump at a cut
    of the same take)."""
    people = [s for s in segs if s.mode == "person" and s.stats and s.stats.get("person_frames")]
    for s in people:
        st = s.stats
        s.scale = base_scale(cfg, st)
        cut_l, cut_r = face_cut_sides(s)
        s.flush_left = float(st.get("touch_left", 0)) >= TOUCH_FLUSH or cut_l
        s.flush_right = float(st.get("touch_right", 0)) >= TOUCH_FLUSH or cut_r
        s.side = None
        if cut_l != cut_r:
            side = "left" if cut_l else "right"
            if side != cfg.position:
                s.side = side
                s.note = (f'the face leaves the source frame on the {side}: here the person is placed against the {side} canvas edge to avoid a cropped face inside the canvas')
    if people:
        weights = sorted((s.scale, s.frames) for s in people)
        half, acc, med = sum(w for _, w in weights) / 2.0, 0.0, weights[-1][0]
        for v, w in weights:
            acc += w
            if acc >= half:
                med = v
                break
        for s in people:
            if abs(s.scale - med) <= 0.08 * med:
                s.scale = med
    if cfg.position != "center":
        # cut on both sides: in a corner one of the cuts would land in the middle of the screen (an arm or a
        # shoulder ending on a vertical line), so the person fills the width instead, a little lower
        for s in people:
            st = s.stats
            if min(float(st.get("touch_left", 0)), float(st.get("touch_right", 0))) < TOUCH_FLUSH:
                continue
            corner = s.scale
            if corner >= 1.0 or 1.0 / corner > CORNER_FILL:
                continue
            top = float(st.get("top", 0.0))
            s.fill, s.scale = True, 1.0
            s.flush_left = s.flush_right = True
            s.drop = round(min(FILL_DROP, max(0.0, (1.0 - top) * (1.0 - corner))), 5)
    fallback = people[0] if people else None
    for s in segs:
        if s.mode == "person" and s not in people:
            s.note = 'no person detected in this interval; only the background is visible'
            if fallback is not None:
                s.scale = fallback.scale


def layer_rect(cfg: Config, seg: Segment, z: float, W: int, H: int) -> tuple[float, float, float]:
    """(scale, x0, y0) of the camera frame on the screen for zoom z (float px)."""
    st = seg.stats or {}
    s = seg.scale * z
    lw, lh = W * s, H * s
    y0 = H - lh
    margin = SIDE_MARGIN * W / 1080.0
    pos = seg.side or cfg.position
    if seg.fill:
        # both cut edges on or past the screen edges; the extra width goes to the side away from the corner
        return s, (0.0 if pos == "right" else W - lw), H - lh + seg.drop * lh
    if pos == "right":
        x0 = W - lw if seg.flush_right else W - margin - float(st.get("right", 1.0)) * lw
    elif pos == "left":
        x0 = 0.0 if seg.flush_left else margin - float(st.get("left", 0.0)) * lw
    else:
        x0 = W / 2.0 - float(st.get("head_cx", 0.5)) * lw
        # a side the camera cut stays on (or past) the screen edge when the frame allows it
        if seg.flush_left and seg.flush_right:
            if lw >= W:
                x0 = min(0.0, max(W - lw, x0))
        elif seg.flush_left:
            x0 = min(x0, 0.0)
        elif seg.flush_right:
            x0 = max(x0, W - lw)
    return s, x0, y0


def inner_cut(cfg: Config, segs: list[Segment], W: int, H: int) -> dict | None:
    """Corner positions: how much of the person time shows the body cut by the camera inside the screen, and
    what would look more natural. None when it is little (or center)."""
    if cfg.position == "center":
        return None
    total = inner = both = 0.0
    for s in segs:
        if s.mode != "person" or not s.stats or not s.stats.get("person_frames"):
            continue
        st = s.stats
        sc, x0, _ = layer_rect(cfg, s, s.zoom, W, H)
        lw = W * sc
        pos = s.side or cfg.position
        if pos == "right":
            t_in = float(st.get("touch_left", 0)) if x0 > 0.5 else 0.0
            t_out = float(st.get("touch_right", 0))
        else:
            t_in = float(st.get("touch_right", 0)) if x0 + lw < W - 0.5 else 0.0
            t_out = float(st.get("touch_left", 0))
        total += s.frames
        inner += s.frames * t_in
        both += s.frames * min(t_in, t_out)
    if total <= 0 or inner / total < INNER_CUT_WARN:
        return None
    other = "left" if cfg.position == "right" else "right"
    corner = "right corner" if cfg.position == "right" else "left corner"
    if both / total >= INNER_CUT_WARN:
        tip = '"position": "center" because the source crops both sides of the body'
        better = "center"
    else:
        tip = (f'"position": "{other}" or enable mirror only when visible clothing and objects contain no text')
        better = other
    return {"side": other, "portion": round(inner / total, 3), "better": better, "mirror": better == other,
            'warning': (f'the source crops the body on the {other} for {inner / total * 100:.0f}% of the time: at {corner}; this edge appears inside the canvas. A different placement may look more natural: {tip}')}


def _map_rect(rect: dict, s: float, x0: float, y0: float, W: int, H: int) -> tuple[float, float, float, float]:
    return (x0 + rect["x0"] * W * s, y0 + rect["y0"] * H * s, x0 + rect["x1"] * W * s, y0 + rect["y1"] * H * s)


def _union(rects: list[tuple]) -> tuple:
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def _fraction(rect: tuple, W: int, H: int) -> dict:
    return {"x0": round(max(0.0, rect[0] / W), 5), "y0": round(max(0.0, rect[1] / H), 5),
            "x1": round(min(1.0, rect[2] / W), 5), "y1": round(min(1.0, rect[3] / H), 5)}


def screen_zones(cfg: Config, seg: Segment, W: int, H: int) -> dict:
    """Where the face and the body end up on the screen (fractions), over the zoom range of the segment.

    -> {"face": {zone, core, eyes_y} | None, "person": rect | None}"""
    if seg.mode != "person" or not seg.stats or not seg.stats.get("person_frames"):
        return {"face": None, "person": None}
    st = seg.stats
    zooms = {seg.zoom, seg.zoom * (1 + seg.push)}
    places = [layer_rect(cfg, seg, z, W, H) for z in zooms]
    body = {"x0": float(st.get("left", 0.0)), "y0": float(st.get("top", 0.0)),
            "x1": float(st.get("right", 1.0)), "y1": 1.0}
    person = _union([_map_rect(body, s, x, y, W, H) for s, x, y in places])
    face = None
    src = seg.face
    if src and src.get("zone"):
        zone = _union([_map_rect(src["zone"], s, x, y, W, H) for s, x, y in places])
        core = _union([_map_rect(src.get("core") or src["zone"], s, x, y, W, H) for s, x, y in places])
        eyes = src.get("face", {}).get("eyes_y") if isinstance(src.get("face"), dict) else None
        eyes = eyes if eyes is not None else (src["zone"]["y0"] + src["zone"]["y1"]) / 2.0
        s, _, y = places[0]
        face = {"zone": _fraction(zone, W, H), "core": _fraction(core, W, H), "eyes_y": round((y + eyes * H * s) / H, 5)}
    else:
        # no face found by the detector: protect the top of the body (head) anyway
        head_h = 0.28 * (1.0 - float(st.get("top", 0.0)))
        cx = float(st.get("head_cx", 0.5))
        head = {"x0": max(0.0, cx - 0.16), "y0": float(st.get("top", 0.0)), "x1": min(1.0, cx + 0.16),
                "y1": float(st.get("top", 0.0)) + head_h}
        zone = _union([_map_rect(head, s, x, y, W, H) for s, x, y in places])
        s, _, y = places[0]
        face = {"zone": _fraction(zone, W, H), "core": _fraction(zone, W, H),
                "eyes_y": round((y + (head["y0"] + head_h * 0.45) * H * s) / H, 5)}
    return {"face": face, "person": _fraction(person, W, H)}


# ================================================================== backgrounds

def _darken(img: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0:
        return img
    cv2 = _cv2()
    return cv2.convertScaleAbs(img, alpha=1.0 - amount, beta=0)


def _blur(img: np.ndarray, sigma: float, W: int) -> np.ndarray:
    """Gaussian blur of `sigma` px (1080 wide), done at a smaller size (fast and the same look)."""
    if sigma <= 0.3:
        return img
    cv2 = _cv2()
    s = sigma * W / 1080.0
    h, w = img.shape[:2]
    k = max(1, min(8, int(s // 3)))
    small = cv2.resize(img, (max(2, w // k), max(2, h // k)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), s / k)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _cover(img: np.ndarray, W: int, H: int, top: bool = False) -> np.ndarray:
    cv2 = _cv2()
    ih, iw = img.shape[:2]
    sc = max(W / iw, H / ih)
    nw, nh = max(W, int(math.ceil(iw * sc))), max(H, int(math.ceil(ih * sc)))
    big = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
    x0 = (nw - W) // 2
    y0 = 0 if top else (nh - H) // 2
    return np.ascontiguousarray(big[y0:y0 + H, x0:x0 + W])


def content_box(img: np.ndarray) -> tuple[float, float, float, float]:
    """The main area of a picture (fractions): where it differs from its border color, so the empty margin of a
    print may go behind the captions or the person. A busy border (a photo) means the whole picture matters."""
    cv2 = _cv2()
    ih, iw = img.shape[:2]
    k = max(1, int(round(max(iw, ih) / 320)))
    small = cv2.resize(img, (max(8, iw // k), max(8, ih // k)), interpolation=cv2.INTER_AREA).astype(np.int16)
    sh, sw = small.shape[:2]
    ring = max(1, int(round(min(sw, sh) * 0.03)))
    border = np.concatenate([small[:ring].reshape(-1, 3), small[-ring:].reshape(-1, 3),
                             small[:, :ring].reshape(-1, 3), small[:, -ring:].reshape(-1, 3)])
    ref = np.median(border, axis=0)
    spread = float(np.percentile(np.abs(border - ref).max(axis=1), 90))
    whole = (0.0, 0.0, 1.0, 1.0)
    if spread > 24:
        return whole
    diff = np.abs(small - ref).max(axis=2) > max(28.0, spread + 16.0)
    rows = np.flatnonzero(diff.mean(axis=1) > 0.015)
    cols = np.flatnonzero(diff.mean(axis=0) > 0.015)
    if rows.size == 0 or cols.size == 0:
        return whole
    pad = 0.02
    box = (max(0.0, cols[0] / sw - pad), max(0.0, rows[0] / sh - pad),
           min(1.0, (cols[-1] + 1) / sw + pad), min(1.0, (rows[-1] + 1) / sh + pad))
    if box[2] - box[0] > 0.9 and box[3] - box[1] > 0.9:
        return whole
    return tuple(round(v, 4) for v in box)


def whole_place(size: tuple[int, int], content: tuple[float, float, float, float], stage: tuple[float, float],
                W: int, H: int, min_share: float = WHOLE_MIN) -> tuple[int, int, int, int]:
    """x, y, w, h of a picture shown whole: as wide as the screen, smaller (down to min_share of the width) only
    when its main area does not fit the stage, main area centered in the stage (or on its top when it still does
    not fit)."""
    iw, ih = size
    top, bottom = stage
    room = max(1.0, bottom - top)
    s = W / iw
    main = max(1e-6, (content[3] - content[1]) * ih)
    if main * s > room:
        s = max(room / main, min_share * W / iw)
    w = min(W, max(2, int(round(iw * s))))
    h = max(2, int(round(ih * s)))
    x = (W - w) // 2
    c0, c1 = content[1] * h, content[3] * h
    y = (top + bottom) / 2.0 - (c0 + c1) / 2.0 if c1 - c0 <= room + 0.5 else top - c0
    return (x, int(round(y)), w, h)


def backdrop(img: np.ndarray, W: int, H: int) -> np.ndarray:
    """A blurred, desaturated and darkened cover copy of img, made at 1/8 size (behind a picture shown whole)."""
    cv2 = _cv2()
    sw, sh = max(2, int(round(W / 8))), max(2, int(round(H / 8)))
    ih, iw = img.shape[:2]
    sc = max(sw / iw, sh / ih)
    nw, nh = max(sw, int(math.ceil(iw * sc))), max(sh, int(math.ceil(ih * sc)))
    small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    x0, y0 = (nw - sw) // 2, (nh - sh) // 2
    small = cv2.GaussianBlur(np.ascontiguousarray(small[y0:y0 + sh, x0:x0 + sw]), (0, 0), BACKDROP["sigma"] * sw / 1080.0)
    gray = cv2.cvtColor(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), cv2.COLOR_GRAY2RGB)
    small = cv2.addWeighted(small, BACKDROP["saturation"], gray, 1.0 - BACKDROP["saturation"], 0)
    small = cv2.convertScaleAbs(small, alpha=1.0 - BACKDROP["darken"], beta=0)
    return cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)


def detail_map(img: np.ndarray) -> np.ndarray:
    """0/1 per pixel of a small frame: detail (an edge, an object) against its blurred surroundings."""
    cv2 = _cv2()
    f = img.astype(np.float32)
    sigma = 6.0 * max(img.shape[:2]) / SCAN_SIDE
    return (np.abs(f - cv2.GaussianBlur(f, (0, 0), sigma)).max(axis=2) > SCAN_DETAIL).astype(np.float32)


def quiet_border(acc: np.ndarray) -> bool:
    """acc: share of detail per pixel. A plain top and bottom border (a studio wall and floor, a flat color) has
    almost none; those are the borders carried out to the screen (the sides of a full-width video are the
    screen's)."""
    h, w = acc.shape
    r = max(1, int(round(min(h, w) * QUIET_RING[0])))
    return max(float(acc[:r].mean()), float(acc[-r:].mean())) < QUIET_RING[1]


def scan_video(path: Path, fps: int, cache_dir: Path | None = None) -> dict:
    """What a background video has where: {frames (one pass at fps), lead and tail (empty frames at its start and
    end, at most a third of it each; a tail also covers a clip shrinking away after its last full frame),
    content (main area, fractions, over the whole clip), quiet (plain border)}. Cached by the file signature."""
    _cv2()  # fail before scanning when OpenCV is unavailable
    st = path.stat()
    key = hashlib.sha1(json.dumps([SCAN_VERSION, str(path), st.st_size, st.st_mtime_ns, fps, SCAN_SIDE, SCAN_DETAIL,
                                   SCAN_EMPTY, SCAN_FADE, SCAN_ROW, SCAN_MASS,
                                   QUIET_RING]).encode("utf-8")).hexdigest()[:16]
    cached = cache_dir / f'scan_{key}.json' if cache_dir else None
    if cached and cached.exists():
        data = M.load_json(cached) or {}
        if data.get("key") == key:
            return data
    size = average_size(path, "video") or (SCAN_SIDE, SCAN_SIDE)
    k = SCAN_SIDE / max(size)
    sw, sh = max(8, int(round(size[0] * k / 2)) * 2), max(8, int(round(size[1] * k / 2)) * 2)
    cmd = M._ff() + ["-i", str(path), "-map", "0:v:0", "-an", "-vf",
                     f'fps={fps},scale={sw}:{sh}:flags=area,setsar=1,format=rgb24', "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    reader = M.FrameReader(cmd, (sw, sh), 3, 'read background video', queue_len=8)
    energy: list[float] = []
    acc = np.zeros((sh, sw), np.float32)
    try:
        while True:
            fr = reader.read()
            if fr is None:
                break
            d = detail_map(fr)
            energy.append(float(d.mean()))
            acc += d
    finally:
        reader.close()
    n = len(energy)
    out = {"key": key, "frames": n, "lead": 0, "tail": 0, "content": [0.0, 0.0, 1.0, 1.0], "quiet": False}
    if n:
        acc /= n
        e = np.array(energy)
        med = float(np.median(e))
        empty = max(SCAN_EMPTY[0], SCAN_EMPTY[1] * med)
        thin = e < max(empty, SCAN_EMPTY[2] * med)

        def run(flags: np.ndarray, edge: float) -> int:
            """Frames from that end that are empty or still building up (0 unless the end frame is empty; 0 in a
            clip with no detail at all, a flat color)."""
            if edge >= empty or med < SCAN_EMPTY[0]:
                return 0
            full = np.flatnonzero(~flags)
            return int(full[0]) if full.size else n
        def fade_out() -> int:
            """Frames after the last full one of a clip that shrinks away to nothing (an X that shrinks after it
            landed): the fall from its last peak, past the frames with SCAN_FADE[1] of that peak."""
            if float(e[-1]) >= empty or med < SCAN_EMPTY[0]:
                return 0
            i = n - 1
            while i > 0 and e[i - 1] >= e[i] - SCAN_FADE[0] * med:
                i -= 1
            fall = e[i:]
            kept = np.flatnonzero(fall >= SCAN_FADE[1] * float(fall.max()))
            return n - 1 - (i + int(kept[-1]))
        out["lead"] = int(min(run(thin, float(e[0])), n // 3))
        out["tail"] = int(min(max(run(thin[::-1], float(e[-1])), fade_out()), n // 3))
        rm, cm = acc.mean(axis=1), acc.mean(axis=0)
        rows = np.flatnonzero(rm > max(SCAN_ROW, 0.1 * float(rm.max())))
        cols = np.flatnonzero(cm > max(SCAN_ROW, 0.1 * float(cm.max())))
        if rows.size and cols.size:
            pad = 0.02
            box = [max(0.0, cols[0] / sw - pad), max(0.0, rows[0] / sh - pad),
                   min(1.0, (cols[-1] + 1) / sw + pad), min(1.0, (rows[-1] + 1) / sh + pad)]
            if not (box[2] - box[0] > 0.9 and box[3] - box[1] > 0.9):      # a busy clip is all main area
                cum = np.cumsum(rm) / max(1e-9, float(rm.sum()))
                cut = (1.0 - SCAN_MASS) / 2.0
                r0 = max(int(rows[0]), int(np.searchsorted(cum, cut)))
                r1 = min(int(rows[-1]) + 1, int(np.searchsorted(cum, 1.0 - cut)) + 1)
                box[1], box[3] = max(0.0, r0 / sh - pad), min(1.0, r1 / sh + pad)
                out["content"] = [round(float(v), 4) for v in box]
        out["quiet"] = quiet_border(acc)
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        M.save_json(cached, out)
    return out


def extend_weights(place: tuple[int, int, int, int], W: int, H: int) -> np.ndarray:
    """0 on a plain-border copy and 1 far from it (EXTEND fall), at 1/8 size: how far the border color has
    faded into its darker average."""
    sw, sh = max(2, int(round(W / 8))), max(2, int(round(H / 8)))
    x, y, w, h = (v / 8.0 for v in place)
    xs = np.arange(sw, dtype=np.float32) + 0.5
    ys = np.arange(sh, dtype=np.float32) + 0.5
    dx = np.maximum(0.0, np.maximum(x - xs, xs - (x + w)))
    dy = np.maximum(0.0, np.maximum(y - ys, ys - (y + h)))
    d = np.sqrt(dx[None, :] ** 2 + dy[:, None] ** 2) / max(1.0, EXTEND["fall"] * W / 1080.0 / 8.0)
    t = np.clip(d, 0, 1)
    return (t * t * (3 - 2 * t)).astype(np.float32)


def extend_backdrop(sharp: np.ndarray, place: tuple[int, int, int, int], W: int, H: int,
                    weights: np.ndarray) -> np.ndarray:
    """The screen around a plain-border copy: its own border carried outwards (blurred) and fading into its
    average color, a little darker, so the copy has no visible box."""
    cv2 = _cv2()
    sw, sh = weights.shape[1], weights.shape[0]
    x, y, w, h = place
    cw, ch = max(1, int(round(w / 8))), max(1, int(round(h / 8)))
    small = cv2.resize(sharp, (cw, ch), interpolation=cv2.INTER_AREA)
    x8, y8 = int(round(x / 8)), int(round(y / 8))
    # the part of the small copy on the screen, then its border carried to the screen edges
    cx0, cy0 = max(0, -x8), max(0, -y8)
    cx1, cy1 = min(cw, sw - x8), min(ch, sh - y8)
    if cx1 <= cx0 or cy1 <= cy0:
        part = small
        top = bottom = left = right = 0
        canvas = cv2.resize(small, (sw, sh), interpolation=cv2.INTER_AREA)
    else:
        part = np.ascontiguousarray(small[cy0:cy1, cx0:cx1])
        top, left = max(0, y8), max(0, x8)
        bottom, right = max(0, sh - top - part.shape[0]), max(0, sw - left - part.shape[1])
        canvas = cv2.copyMakeBorder(part, top, bottom, left, right, cv2.BORDER_REPLICATE)[:sh, :sw]
    sigma = EXTEND["blur"] * W / 1080.0 / 8.0
    canvas = cv2.GaussianBlur(canvas, (0, 0), sigma).astype(np.float32)
    ring = np.concatenate([part[:1].reshape(-1, 3), part[-1:].reshape(-1, 3), part[:, :1].reshape(-1, 3),
                           part[:, -1:].reshape(-1, 3)]).astype(np.float32)
    flat = np.median(ring, axis=0) * (1.0 - EXTEND["darken"])
    t = weights[..., None]
    out = canvas * (1.0 - t) + flat[None, None, :] * t
    return cv2.resize(np.clip(out, 0, 255).astype(np.uint8), (W, H), interpolation=cv2.INTER_LINEAR)


def feather_mask(w: int, h: int, x: int, y: int, W: int, H: int) -> np.ndarray:
    """Blend weights of a sharp copy at (x, y): smooth ramps on the sides that fall inside the screen."""
    f = max(2, int(round(BACKDROP["feather"] * W / 1080.0)))

    def ramp(n: int, lead: bool, trail: bool) -> np.ndarray:
        r = np.ones(n, np.float32)
        m = min(f, n // 4)
        if m > 1:
            e = np.linspace(0.0, 1.0, m, dtype=np.float32)
            e = e * e * (3 - 2 * e)
            if lead:
                r[:m] = np.minimum(r[:m], e)
            if trail:
                r[n - m:] = np.minimum(r[n - m:], e[::-1])
        return r

    return np.ascontiguousarray(ramp(h, y > 0, y + h < H)[:, None] * ramp(w, x > 0, x + w < W)[None, :])


def whole_frame(sharp: np.ndarray, place: tuple[int, int, int, int], W: int, H: int, mask: np.ndarray,
                extend: np.ndarray | None = None) -> np.ndarray:
    """The sharp copy (already w x h) at its place over the backdrop made from it (its own border carried
    outwards when extend, the weights of extend_weights, is given)."""
    cv2 = _cv2()
    base = backdrop(sharp, W, H) if extend is None else extend_backdrop(sharp, place, W, H, extend)
    x, y, w, h = place
    if sharp.shape[1] != w or sharp.shape[0] != h:
        sharp = cv2.resize(sharp, (w, h), interpolation=cv2.INTER_AREA if w < sharp.shape[1] else cv2.INTER_CUBIC)
    X0, Y0, X1, Y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if X1 <= X0 or Y1 <= Y0:
        return base
    part = np.ascontiguousarray(sharp[Y0 - y:Y1 - y, X0 - x:X1 - x])
    m = np.ascontiguousarray(mask[Y0 - y:Y1 - y, X0 - x:X1 - x])
    base[Y0:Y1, X0:X1] = cv2.blendLinear(np.ascontiguousarray(base[Y0:Y1, X0:X1]), part, 1.0 - m, m)
    return base


def picture_frame(img: np.ndarray, b: Background, W: int, H: int) -> tuple[np.ndarray, tuple[float, float]]:
    """A still picture on the screen -> (frame, Ken Burns center)."""
    cv2 = _cv2()
    ih, iw = img.shape[:2]
    if b.fit == "contain" and b.place:
        x, y, w, h = b.place
        sharp = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA if w < iw else cv2.INTER_CUBIC)
        frame = whole_frame(sharp, b.place, W, H, feather_mask(w, h, x, y, W, H))
        c = b.content
        return frame, (x + (c[0] + c[2]) / 2.0 * w, y + (c[1] + c[3]) / 2.0 * h)
    tall = iw / ih < (W / H) / FIT_RATIO and W < H
    return _cover(img, W, H, top=tall), (W / 2.0, H * 0.3 if tall else H / 2.0)


def _kb_frame(base: np.ndarray, center: tuple[float, float], p: float, W: int, H: int) -> np.ndarray:
    cv2 = _cv2()
    z = 1.0 + KB_DRIFT * ease(p)
    cx, cy = center
    m = np.array([[z, 0.0, cx - z * cx], [0.0, z, cy - z * cy]], np.float32)
    return cv2.warpAffine(base, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def read_image(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps  # noqa: PLC0415
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (18, 18, 18))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        return np.ascontiguousarray(np.asarray(im.convert("RGB")))


class LoopReader:
    """Frames of a background video in order. Every pass plays untouched (an insert shows its clip frame by frame,
    as in the split screen), except its empty frames: the `lead` empty frames at the start show the first frame
    with something in it and the `tail` ones at the end the last (a pass never flashes an empty scene;
    first_lead replaces lead on the first pass, 0 when a dissolve covers the start). Only
    when the clip has to go on past its end does it start again (a new ffmpeg reader), and the first `fade`
    frames of the new pass dissolve from the last frame shown, so the loop neither jumps nor freezes."""

    def __init__(self, cmd: list[str], size: tuple[int, int], what: str, fade: int, lead: int = 0, tail: int = 0,
                 frames: int = 0, first_lead: int | None = None):
        self.cmd, self.size, self.what = cmd, size, what
        self.fade = max(0, int(fade))
        self.lead, self.tail, self.frames = max(0, int(lead)), max(0, int(tail)), max(0, int(frames))
        self.first_lead = self.lead if first_lead is None else max(0, int(first_lead))   # the first pass
        self.cur: M.FrameReader | None = self._open()
        self.cycle = 0                              # frames given from the current pass
        self.length = 0                             # frames of one pass (known when the first one ends)
        self.shown: np.ndarray | None = None        # the last frame given (before a dissolve)
        self.ahead = 0                              # frames of this pass already read (the empty start)
        self.hold: np.ndarray | None = None         # the frame shown over the empty start or end
        self.blend_from: np.ndarray | None = None   # the last frame of the pass before, while the new one dissolves in
        self.blend_n = 0
        self.loops = 0

    def _open(self) -> "M.FrameReader":
        return M.FrameReader(self.cmd, self.size, 3, self.what, queue_len=4)

    def read(self) -> np.ndarray | None:
        if self.cur is None:
            return None
        if self.ahead > 0:
            self.ahead -= 1
            return self._give(self.hold)
        fr = self.cur.read()
        if fr is None:
            if self.cycle == 0 or self.shown is None:
                self.close()
                return None
            self.length = self.length or self.cycle
            self.cur.close()
            self.cur = self._open()
            self.loops += 1
            self.cycle = 0
            self.blend_from, self.blend_n = self.shown, min(self.fade, self.length // 3)
            fr = self.cur.read()
            if fr is None:
                self.close()
                return None
        i = self.cycle
        lead = self.first_lead if self.loops == 0 else self.lead
        if i == 0 and lead > 0:
            for _ in range(lead):
                nxt = self.cur.read()
                if nxt is None:
                    break
                fr = nxt
                self.ahead += 1
            self.hold = fr
        elif self.frames and self.tail > 0 and i >= self.frames - self.tail and self.hold is not None:
            fr = self.hold
        if self.frames and self.tail > 0 and i == self.frames - self.tail - 1:
            self.hold = fr
        return self._give(fr)

    def _give(self, fr: np.ndarray) -> np.ndarray:
        self.cycle += 1
        self.shown = fr
        if self.blend_from is not None and self.cycle <= self.blend_n:
            w = self.cycle / (self.blend_n + 1)
            return _cv2().addWeighted(self.blend_from, 1.0 - w, fr, w, 0)
        self.blend_from = None
        return fr

    def close(self) -> None:
        if self.cur is not None:
            self.cur.close()
            self.cur = None


class BackgroundTrack:
    """Background frames in order: fetch(f) reads what has to be read in order (video frames) and returns a
    function that makes the frame (run in the worker threads)."""

    def __init__(self, cfg: Config, W: int, H: int, fps: int):
        self.cfg, self.W, self.H, self.fps = cfg, W, H, fps
        self.runs = cfg.runs
        self.run_i = 0
        self.stills: dict[int, tuple[np.ndarray, tuple[float, float]]] = {}
        self.videos: dict[int, dict] = {}
        self.masks: dict[int, np.ndarray] = {}
        self.dark = np.empty((H, W, 3), np.uint8)
        self.dark[:] = np.array(hex_rgb(DARK_DEFAULT), np.uint8)
        self.ends: dict[int, int] = {}
        for f0, f1, k in cfg.runs:       # a background lasts until the last run it owns (gaps included)
            if k is not None:
                self.ends[k] = max(self.ends.get(k, 0), f1)
        self.extends: dict[int, np.ndarray] = {}
        self.last: tuple[int, int | None, Callable[[], np.ndarray]] | None = None   # (frame, owner, make)
        self.dissolve: tuple[int, np.ndarray | None] | None = None                 # (owner, frame it comes from)
        self.in_hold: set[int] = set()   # videos whose empty start plays under a dissolve (first pass)

    def kb_span(self, k: int) -> int:
        """Frames of the Ken Burns: the item's own time, stretched over a held gap up to KB_HOLD_AFTER times it."""
        b = self.cfg.backgrounds[k]
        own = max(1, b.f1 - b.f0)
        total = max(own, self.ends.get(k, b.f1) - b.f0)
        return max(1, min(total, int(round(own * KB_HOLD_AFTER))) - 1)

    def _still(self, k: int) -> tuple[np.ndarray, tuple[float, float]]:
        if k not in self.stills:
            b = self.cfg.backgrounds[k]
            if b.kind == "color":
                frame = np.empty((self.H, self.W, 3), np.uint8)
                frame[:] = np.array(b.color, np.uint8)
                self.stills[k] = (_darken(frame, b.darken), (self.W / 2.0, self.H / 2.0))
            else:
                frame, center = picture_frame(read_image(b.src), b, self.W, self.H)
                frame = _darken(_blur(frame, b.blur, self.W), b.darken)
                self.stills[k] = (frame, center)
        return self.stills[k]

    def _mask(self, k: int) -> np.ndarray:
        if k not in self.masks:
            b = self.cfg.backgrounds[k]
            x, y, w, h = b.place
            self.masks[k] = feather_mask(w, h, x, y, self.W, self.H)
            if b.quiet:
                self.extends[k] = extend_weights(b.place, self.W, self.H)
        return self.masks[k]

    def _video_raw(self, k: int, local: int) -> np.ndarray:
        b = self.cfg.backgrounds[k]
        v = self.videos.get(k)
        if v is None:
            if b.fit == "contain" and b.place:
                size = (b.place[2], b.place[3])
                vf = f'fps={self.fps},scale={size[0]}:{size[1]}:flags=bicubic,setsar=1,format=rgb24'
            else:
                size = (self.W, self.H)
                vf = (f'fps={self.fps},scale={self.W}:{self.H}:force_original_aspect_ratio=increase:flags=bicubic,crop={self.W}:{self.H},setsar=1,format=rgb24')
            cmd = M._ff() + ["-i", str(b.src), "-map", "0:v:0", "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            v = {"reader": LoopReader(cmd, size, 'read background video', int(round(LOOP_FADE * self.fps)),
                                      lead=b.lead, tail=b.tail, frames=b.pass_frames,
                                      first_lead=0 if k in self.in_hold else None),
                 "pos": 0, "last": None}
            self.videos[k] = v
        while v["pos"] <= local:
            fr = v["reader"].read()
            if fr is None:
                break
            v["last"] = fr
            v["pos"] += 1
        if v["last"] is None:
            raise CutoutError(f'could not read background video ({b.name}): {b.src}', 1)
        return v["last"]

    def _video_frame(self, k: int, raw: np.ndarray, local: int) -> np.ndarray:
        b = self.cfg.backgrounds[k]
        frame, center = raw, (self.W / 2.0, self.H / 2.0)
        if b.fit == "contain" and b.place:
            frame = whole_frame(raw, b.place, self.W, self.H, self._mask(k), self.extends.get(k))
            x, y, w, h = b.place
            center = (x + w / 2.0, y + h / 2.0)
        if b.ken_burns:
            frame = _kb_frame(frame, center, min(1.0, local / self.kb_span(k)), self.W, self.H)
        frame = _darken(_blur(frame, b.blur, self.W), b.darken)
        return frame if frame is not raw else frame.copy()

    def owner(self, f: int) -> int | None:
        while self.run_i < len(self.runs) - 1 and f >= self.runs[self.run_i][1]:
            self.run_i += 1
        return self.runs[self.run_i][2]

    def fetch(self, f: int, original: np.ndarray | None) -> Callable[[], np.ndarray]:
        k = self.owner(f)
        src, w = None, 1.0
        if k is not None:
            b = self.cfg.backgrounds[k]
            local = f - b.f0
            if b.kind == "video" and b.lead > 0 and local < b.lead:
                # the clip starts empty: the background right before it dissolves into it while it builds up
                # (its own frames play under the dissolve); with nothing before it, its first full frame is held
                if self.dissolve is None or self.dissolve[0] != k:
                    prev = self.last
                    first = local == 0 and prev and prev[0] == f - 1 and prev[1] is not None and prev[1] != k
                    self.dissolve = (k, prev[2]() if first else None)
                    if first and k not in self.videos:
                        self.in_hold.add(k)
                src = self.dissolve[1]
                w = (local + 1) / (b.lead + 1)
        make = self._make(f, k, original)
        if src is not None:
            inner = make
            make = lambda: _cv2().addWeighted(src, 1.0 - w, inner(), w, 0)  # noqa: E731
        self.last = (f, k, make)
        return make

    def _make(self, f: int, k: int | None, original: np.ndarray | None) -> Callable[[], np.ndarray]:
        W, H = self.W, self.H
        if k is None:
            if self.cfg.blurred_original and original is not None:
                return lambda: _darken(_blur(original, ORIGINAL_BLUR["sigma"], W), ORIGINAL_BLUR["darken"])
            return self.dark.copy
        b = self.cfg.backgrounds[k]
        local = f - b.f0
        if b.kind == "video":
            raw = self._video_raw(k, local)
            if k not in self.masks and b.fit == "contain" and b.place:
                self._mask(k)            # made once, here, before the workers share it
            return lambda: self._video_frame(k, raw, local)
        base, center = self._still(k)
        if b.ken_burns:
            p = min(1.0, local / self.kb_span(k))
            return lambda: _kb_frame(base, center, p, W, H)
        return base.copy

    def frame(self, f: int, original: np.ndarray | None) -> np.ndarray:
        return self.fetch(f, original)()

    def close(self) -> None:
        for v in self.videos.values():
            v["reader"].close()
        self.videos.clear()


# ================================================================== room plate (the room of a tripod shot)

def _ss(x: np.ndarray, a: float, b: float) -> np.ndarray:
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def plate_size(full: tuple[int, int]) -> tuple[int, int]:
    """The plate works at the matte work size (half the output)."""
    return max(2, (full[0] // 2) // 2 * 2), max(2, (full[1] // 2) // 2 * 2)


@dataclass
class RoomPlate:
    """What the room behind a take looks like, learned from every other frame of its ranges where the matte says
    room (the camera on a tripod). evidence() finds where a frame's matte is unsure (a haze, a patch the model
    flickers on) and the picture there is exactly that room: those pieces go (a dark room only when it is unlike
    the body next to it), and so does each pixel of a light halo of the room around a darker body. A matte that
    is sure is never touched (a kept object), nor is the rest of the soft edge of the body or a dark shirt
    blurred over a dark desk; clean_edge() takes the room's color out of that soft edge."""
    key: str
    size: tuple[int, int]
    P: np.ndarray                 # blurred room color (float32 h x w x 3)
    rel: np.ndarray               # 0..1 how much the plate is trusted there
    tol: np.ndarray               # color distance that still is the room there
    known: float = 0.0            # share of the frame the plate knows
    spread: float = 0.0           # median spread of the room color (0-255)
    mask: np.ndarray | None = None

    def __post_init__(self):
        self.mask = self.rel > 0.02

    def evidence(self, frame: np.ndarray, alpha: np.ndarray) -> np.ndarray | None:
        """frame: RGB uint8 at the plate size; alpha: 0..1. -> 0..1 map of room to take out of the matte (None
        when there is none)."""
        cv2 = _cv2()
        lo, hi = PLATE["unsure"]
        cand = self.mask & (alpha > 0.02) & (alpha < hi)
        rows = np.flatnonzero(cand.any(axis=1))
        if rows.size == 0:
            return None
        cols = np.flatnonzero(cand.any(axis=0))
        w, h = self.size
        pad = PLATE["unsure_px"] + 8
        y0, y1 = max(0, int(rows[0]) - pad), min(h, int(rows[-1]) + pad + 1)
        x0, x1 = max(0, int(cols[0]) - pad), min(w, int(cols[-1]) + pad + 1)
        sl = (slice(y0, y1), slice(x0, x1))
        img = cv2.GaussianBlur(np.ascontiguousarray(frame[sl]).astype(np.float32), (0, 0), PLATE["blur"])
        a = alpha[sl]
        room_P = self.P[sl]
        tol = self.tol[sl]
        win = PLATE["win"]
        d1 = cv2.absdiff(img, room_P).max(axis=2)
        d = cv2.blur(d1, (win, win))
        # a dark room must look unlike the body next to it (a dark shirt blurred over a dark desk is left alone)
        core = (a >= hi).astype(np.float32)
        sig = PLATE["near"]
        body = cv2.GaussianBlur(img * core[..., None], (0, 0), sig) / np.maximum(
            cv2.GaussianBlur(core, (0, 0), sig), 1e-3)[..., None]
        unlike = np.maximum(_ss(cv2.absdiff(body, room_P).max(axis=2), *PLATE["unlike"]),
                            _ss(room_P.mean(axis=2), *PLATE["bright"]))
        base = self.rel[sl] * (1.0 - _ss(a, lo, hi)) * cand[sl]
        # a light halo of the room around a darker body goes pixel by pixel (only the pixels that are that room)
        halo = (base * _ss(room_P.mean(axis=2) - body.mean(axis=2), *PLATE["halo"])
                * (1.0 - _ss(d1, tol, tol + PLATE["width"])))
        halo = np.where(halo > 0.05, halo, 0.0).astype(np.float32)
        e = base * unlike * (1.0 - _ss(d, tol, tol + PLATE["width"]))
        m = (e > PLATE["on"]).astype(np.uint8)
        # only pieces wider than the soft edge of a body, rounded, and only when they reach the room around the
        # person or the frame edge (a piece inside the body would leave a hole)
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (PLATE["unsure_px"], PLATE["unsure_px"]))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
        out_sl = halo
        if m.any():
            round_ = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (PLATE["round_px"], PLATE["round_px"]))
            m = cv2.morphologyEx(cv2.morphologyEx(m, cv2.MORPH_CLOSE, round_), cv2.MORPH_OPEN, round_)
            n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
            room = cv2.dilate((a < 0.5).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
            keep = np.zeros(n, bool)
            keep[np.unique(lab[room & (m > 0)])] = True
            bx, by, bw, bh = st.T[:4]
            keep |= (bx + x0 == 0) | (by + y0 == 0) | (bx + bw + x0 == w) | (by + bh + y0 == h)
            keep[0] = False
            k = keep[lab]
            if k.any():
                whole = cv2.GaussianBlur(cv2.dilate(k.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(np.float32),
                                         (0, 0), PLATE["soft"])
                out_sl = np.maximum(out_sl, whole)
        if not out_sl.any():
            return None
        out = np.zeros((h, w), np.float32)
        out[sl] = out_sl
        return out

    def clean_edge(self, frame: np.ndarray, alpha: np.ndarray) -> np.ndarray:
        """The colors of the soft edge of the matte without the known room mixed into them (a light or blue
        fringe from the room behind): each partly transparent pixel is solved for the body color, where the plate
        knows the room. frame: RGB uint8, alpha: uint8, both at the layer size. -> a new frame (or frame)."""
        lo, hi = PLATE["edge"]
        h, w = alpha.shape
        pw, ph = self.size
        ys, xs = np.nonzero((alpha > lo) & (alpha < hi))
        if ys.size == 0:
            return frame
        py = np.minimum(ph - 1, (ys * ph) // h)
        px = np.minimum(pw - 1, (xs * pw) // w)
        rel = self.rel[py, px]
        use = rel > 0.3
        if not use.any():
            return frame
        ys, xs, py, px, rel = ys[use], xs[use], py[use], px[use], rel[use]
        a = (alpha[ys, xs].astype(np.float32) / 255.0)[:, None]
        img = frame[ys, xs].astype(np.float32)
        B = self.P[py, px]
        solved = (img - (1.0 - a) * B) / np.maximum(a, PLATE["edge_floor"])
        out = frame.copy()
        out[ys, xs] = np.clip(img + rel[:, None] * (solved - img), 0, 255).astype(np.uint8)
        return out

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".tmp-" + path.name)
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, P=self.P, rel=self.rel, tol=self.tol,
                                meta=np.array(json.dumps({"key": self.key, "size": list(self.size),
                                                          "known": self.known, "spread": self.spread})))
        os.replace(tmp, path)

    @staticmethod
    def load(path: Path, key: str) -> RoomPlate | None:
        try:
            with np.load(path) as z:
                meta = json.loads(str(z["meta"]))
                if meta.get("key") != key:
                    return None
                return RoomPlate(key=key, size=tuple(meta["size"]), P=z["P"], rel=z["rel"], tol=z["tol"],
                                 known=float(meta.get("known", 0.0)), spread=float(meta.get("spread", 0.0)))
        except (OSError, ValueError, KeyError):
            return None


class _PlateAcc:
    def __init__(self, size: tuple[int, int]):
        w, h = size
        self.size = size
        self.sum = np.zeros((h, w, 3), np.float32)
        self.sq = np.zeros((h, w, 3), np.float32)
        self.n = np.zeros((h, w), np.float32)
        cv2 = _cv2()
        r = PLATE["fringe"]
        self.ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))

    def add(self, frame: np.ndarray, alpha: np.ndarray) -> None:
        cv2 = _cv2()
        room = (cv2.dilate((alpha >= PLATE["bg"]).astype(np.uint8), self.ker) == 0).astype(np.uint8)
        x = frame.astype(np.float32)
        cv2.accumulate(x, self.sum, room)
        cv2.accumulateSquare(x, self.sq, room)
        cv2.accumulate(room.astype(np.float32), self.n)

    def finish(self, key: str) -> tuple[RoomPlate | None, str]:
        cv2 = _cv2()
        n = self.n
        nn = np.maximum(n, 1.0)[..., None]
        mean = self.sum / nn
        std = np.sqrt(np.maximum(self.sq / nn - mean * mean, 0.0)).max(axis=2)
        well = n >= PLATE["n"][1]
        known = float(well.mean())
        if known < PLATE["known"]:
            return None, 'little background is visible in these intervals'
        spread = float(np.median(std[well]))
        if spread > PLATE["static"]:
            return None, 'camera movement in these intervals'
        P = cv2.GaussianBlur(mean, (0, 0), PLATE["blur"]).astype(np.float32)
        rel = (_ss(n, *PLATE["n"]) * (1.0 - _ss(std, *PLATE["std"]))).astype(np.float32)
        tol = (PLATE["tol"] + np.minimum(std, PLATE["std"][0] + 2.0)).astype(np.float32)
        return RoomPlate(key=key, size=self.size, P=P, rel=rel, tol=tol, known=round(known, 3),
                         spread=round(spread, 2)), ""


def room_plates(base: Path, base_pre: list[str], base_fps: float, fps: int, full_size: tuple[int, int],
                groups: dict[str, list[tuple[int, int, M.RangeJob]]], cache_dir: Path,
                progress: Callable[[str], None] | None = None) -> tuple[dict[str, RoomPlate], dict[str, str]]:
    """One plate per take (groups: take -> [(f0, f1, matte job)]), read from the cut at the plate size in one
    pass (only when a plate is not cached). -> ({take: plate}, {take: why there is none})."""
    size = plate_size(full_size)
    sig = M.file_signature(base)
    keys, plates, notes = {}, {}, {}
    for g, parts in groups.items():
        keys[g] = hashlib.sha1(json.dumps([PLATE_VERSION, PLATE, str(base), sig, base_pre, fps, list(full_size),
                                           sorted([a, b, j.key] for a, b, j in parts)],
                                          sort_keys=True).encode("utf-8")).hexdigest()[:16]
    all_value = {}
    for g, key in keys.items():
        path = cache_dir / f'plate_{key}.npz'
        note_path = cache_dir / f'plate_{key}.json'
        if path.exists():
            plate = RoomPlate.load(path, key)
            if plate is not None:
                plates[g] = plate
                continue
        if note_path.exists():
            data = M.load_json(note_path) or {}
            if data.get("key") == key:
                notes[g] = str(data.get("note") or "")
                continue
        all_value[g] = key
    if not all_value:
        return plates, notes
    started = time.time()
    spans = sorted((a, b, g, j) for g in all_value for a, b, j in groups[g])
    last = max(b for a, b, _, _ in spans)
    accs = {g: _PlateAcc(size) for g in all_value}
    reader = M.FrameReader(M.base_cmd(base, base_pre, base_fps, fps, 0, last, size=size, full_size=full_size),
                           size, 3, "read the cut", queue_len=8)
    try:
        f = 0
        for a, b, g, job in spans:
            while f < a:
                if reader.read() is None:
                    break
                f += 1
            mreader = M.FrameReader(M.matte_cmd(job.path, size, full_size), size, 1, "read the cutout", queue_len=8)
            try:
                for k in range(b - a):
                    frame = reader.read()
                    alpha = mreader.read()
                    f += 1
                    if frame is None or alpha is None:
                        break
                    if k % PLATE["step"] == 0:
                        accs[g].add(frame, alpha)
            finally:
                mreader.close()
    finally:
        reader.close()
    for g, key in all_value.items():
        plate, note = accs[g].finish(key)
        if plate is not None:
            plate.save(cache_dir / f'plate_{key}.npz')
            plates[g] = plate
        else:
            notes[g] = note
            M.save_json(cache_dir / f'plate_{key}.json', {"key": key, "note": note})
    if progress:
        progress(f'green screen: background learned from {time.time() - started:.1f}s')
    return plates, notes


# ================================================================== the person layer

@dataclass
class Look:
    W: int
    H: int
    outline: tuple[np.ndarray, float] | None      # (rgb float, width px at this size)
    shadow: bool
    fade: float                                   # px at this size (the top cut)
    side_fade: float | None = None                # px at this size (a side cut; None = fade)


def _edge_profile(edge: np.ndarray) -> np.ndarray:
    """0..1 along one edge of the camera frame: where the body touches it (widened and softened, so the fade
    has no steps and covers the whole cut)."""
    cv2 = _cv2()
    n = edge.size
    t = (edge > 127).astype(np.float32).reshape(-1, 1)
    if not t.any():
        return np.zeros(n, np.float32)
    k = max(3, n // 40) | 1
    t = cv2.dilate(t, np.ones((k, 1), np.uint8))
    if n > 4:
        t = cv2.GaussianBlur(t, (1, 0), sigmaX=0.1, sigmaY=k / 6.0)
    return np.clip(t[:, 0], 0, 1)


def enclosed_gaps(inside: np.ndarray, wall: np.ndarray) -> np.ndarray | None:
    """0/1 map of the background pockets of `inside` (0/1) that do not reach the open border, or None. wall marks
    the edges where the body goes on past the camera frame (the bottom, a cut side): a pocket that only reaches
    the border through them is closed (the gap between an arm and the body, a hole in a hand)."""
    cv2 = _cv2()
    free = ((inside == 0) & (wall == 0)).astype(np.uint8)
    if not free.any():
        return None
    n, lab = cv2.connectedComponents(free, connectivity=4)
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    outside = np.zeros(n, bool)
    outside[np.unique(border)] = True
    outside[0] = False
    if not outside.any():
        return None                   # nothing is open (a body filling the frame): leave it as it is
    holes = (~outside[lab]) & (free > 0)
    if not holes.any():
        return None
    holes = holes.astype(np.uint8)
    return holes | (wall & (inside == 0) & cv2.dilate(holes, np.ones((3, 3), np.uint8)))


def compose_person(bg: np.ndarray, person: np.ndarray, alpha: np.ndarray, s: float, x0: float, y0: float,
                   look: Look) -> np.ndarray:
    """Draw the cut-out person (scaled by s, camera frame corner at x0, y0) with outline and shadow onto bg."""
    cv2 = _cv2()
    W, H = look.W, look.H
    ow = look.outline[1] if look.outline else 0.0
    sh = SHADOW
    k = W / 1080.0
    pad = int(math.ceil(ow + (3 * sh["blur"] + max(sh["dx"], sh["dy"])) * k if look.shadow else ow)) + 2
    lw, lh = int(round(W * s)), int(round(H * s))
    lx, ly = int(round(x0)), int(round(y0))
    if lw < 4 or lh < 4:
        return bg
    X0, Y0 = max(0, lx - pad), max(0, ly - pad)
    X1, Y1 = min(W, lx + lw + pad), min(H, ly + lh + pad)
    if X1 - X0 < 2 or Y1 - Y0 < 2:
        return bg
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    # only the part of the camera frame that lands on the screen
    sx0, sx1 = max(0, X0 - lx), min(lw, X1 - lx)
    sy0, sy1 = max(0, Y0 - ly), min(lh, Y1 - ly)
    P = cv2.resize(person, (lw, lh), interpolation=interp)[sy0:sy1, sx0:sx1]
    A = cv2.resize(alpha, (lw, lh), interpolation=interp)[sy0:sy1, sx0:sx1]
    rw, rh = X1 - X0, Y1 - Y0
    ox, oy = lx + sx0 - X0, ly + sy0 - Y0            # where P starts inside the region
    A_reg = np.zeros((rh, rw), np.uint8)
    A_reg[oy:oy + A.shape[0], ox:ox + A.shape[1]] = A
    P_reg = np.zeros((rh, rw, 3), np.uint8)
    P_reg[oy:oy + P.shape[0], ox:ox + P.shape[1]] = P

    # edges where the camera frame cuts the body inside the screen: left, right, top
    cuts = {}
    e = max(2, alpha.shape[1] // 270)             # a body within this many px of the edge counts as cut by it
    if lx > 0 and sx0 == 0:
        cuts["left"] = _edge_profile(alpha[:, :e].max(axis=1))
    if lx + lw < W and sx1 == lw:
        cuts["right"] = _edge_profile(alpha[:, -e:].max(axis=1))
    if ly > 0 and sy0 == 0:
        cuts["top"] = _edge_profile(alpha[:e, :].max(axis=0))
    cuts = {name: prof for name, prof in cuts.items() if prof.any()}
    for name in list(cuts):     # the rows/columns of the edge profile map to the layer, then to the region
        prof = cuts[name]
        if name in ("left", "right"):
            full = np.zeros(rh, np.float32)
            rows = cv2.resize(prof.reshape(-1, 1), (1, lh), interpolation=cv2.INTER_LINEAR)[:, 0][sy0:sy1]
            full[oy:oy + rows.size] = rows
        else:
            full = np.zeros(rw, np.float32)
            cols = cv2.resize(prof.reshape(1, -1), (lw, 1), interpolation=cv2.INTER_LINEAR)[0][sx0:sx1]
            full[ox:ox + cols.size] = cols
        cuts[name] = full

    # outline and shadow at half size; the matte is carried past the bottom edge, so neither draws a line along it
    # (past a side or top cut the keep mask below removes them)
    hw, hh = max(2, rw // 2), max(2, rh // 2)
    Ah = cv2.resize(A_reg, (hw, hh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    hox = int(ox // 2)
    hx1, hy1 = min(hw, int(math.ceil((ox + A.shape[1]) / 2))), min(hh, int(math.ceil((oy + A.shape[0]) / 2)))
    if hy1 < hh and ly + lh >= H - 1:
        Ah[hy1:, hox:hx1] = Ah[hy1 - 1:hy1, hox:hx1]
    D = None
    gaps = None
    if look.outline:
        oh = ow / 2.0
        sm = cv2.GaussianBlur(Ah, (0, 0), max(0.6, oh / 2.5))
        inside = ((sm > 0.5) | (Ah > 0.5)).astype(np.uint8)
        # the outline goes around the body only: a gap it encloses (with the edges the body goes on past) stays
        # see-through but gets no outline
        wall = np.zeros((hh, hw), np.uint8)
        hoy = int(oy // 2)
        if hy1 > hoy and hx1 > hox:
            if ly + lh >= H - 1:
                wall[hy1 - 1, hox:hx1] = 1
            if "top" in cuts:
                wall[hoy, hox:hx1] = 1
            if "left" in cuts:
                wall[hoy:hy1, hox] = 1
            elif lx <= 0:
                wall[hoy:hy1, 0] = 1
            if "right" in cuts:
                wall[hoy:hy1, hx1 - 1] = 1
            elif lx + lw >= W:
                wall[hoy:hy1, hw - 1] = 1
        gaps = enclosed_gaps(inside, wall)
        if gaps is not None:
            inside = inside | gaps
        dist = cv2.distanceTransform(1 - inside, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        Dh = np.clip(oh + 0.5 - dist, 0, 1).astype(np.float32)
        Dh = cv2.GaussianBlur(Dh, (0, 0), 0.6)
        D = cv2.resize(Dh, (rw, rh), interpolation=cv2.INTER_LINEAR)
    S = None
    if look.shadow:
        src = Dh if D is not None else Ah
        q = cv2.resize(src, (max(2, hw // 2), max(2, hh // 2)), interpolation=cv2.INTER_AREA)
        q = cv2.GaussianBlur(q, (0, 0), max(0.5, sh["blur"] * k / 4.0))
        S = cv2.resize(q, (rw, rh), interpolation=cv2.INTER_LINEAR)
        mshift = np.float32([[1, 0, sh["dx"] * k], [0, 1, sh["dy"] * k]])
        S = cv2.warpAffine(S, mshift, (rw, rh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        S *= sh["alpha"]
    if gaps is not None:
        # an enclosed gap shows the background as it is: no outline color, no shadow in it (2 px wider, so the
        # soft edge of the body around it blends with the background, not with the outline)
        g = cv2.resize(cv2.dilate(gaps, np.ones((5, 5), np.uint8)).astype(np.float32), (rw, rh),
                       interpolation=cv2.INTER_LINEAR)
        gap_keep = 1.0 - g
        if S is not None:
            S = S * gap_keep

    # keep mask: short fade on the cut rows near a cut edge, nothing past it
    K = None
    if cuts:
        top_fade = max(2.0, look.fade)
        side_fade = max(2.0, look.side_fade if look.side_fade is not None else look.fade)
        K = np.ones((rh, rw), np.float32)
        xs = np.arange(rw, dtype=np.float32)
        ys = np.arange(rh, dtype=np.float32)

        def ramp(d: np.ndarray, fade: float) -> np.ndarray:
            r = np.clip(d / fade, 0, 1)
            return r * r * (3 - 2 * r)
        if "left" in cuts:
            K *= 1.0 - cuts["left"][:, None] * (1.0 - ramp(xs - float(ox), side_fade)[None, :])
        if "right" in cuts:
            K *= 1.0 - cuts["right"][:, None] * (1.0 - ramp(float(ox + A.shape[1]) - xs, side_fade)[None, :])
        if "top" in cuts:
            K *= 1.0 - cuts["top"][None, :] * (1.0 - ramp(ys - float(oy), top_fade)[:, None])

    region0 = bg[Y0:Y1, X0:X1]
    region = region0
    Af = A_reg.astype(np.float32) / 255.0
    if D is not None:
        D = np.maximum(D, Af)
        if gaps is not None:
            D *= gap_keep
    if S is not None:
        region = cv2.blendLinear(region, np.zeros_like(region), 1.0 - S, S)
    if D is not None:
        color = np.empty_like(region)
        color[:] = look.outline[0].astype(np.uint8)
        region = cv2.blendLinear(region, color, 1.0 - D, D)
    region = cv2.blendLinear(region, P_reg, 1.0 - Af, Af)
    if K is not None:
        # shadow, outline and person fade together (the outline never shows through a fading arm)
        region = cv2.blendLinear(region0, region, 1.0 - K, K)
    bg[Y0:Y1, X0:X1] = region
    return bg


def without_room(plate: RoomPlate, frame: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(frame, matte) of a camera frame with the room its take's plate recognizes taken out of the matte and out
    of the colors of its soft edge (uint8, camera frame size)."""
    cv2 = _cv2()
    h, w = alpha.shape
    pw, ph = plate.size
    if (w, h) != (pw, ph):
        small = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)
        a = cv2.resize(alpha, (pw, ph), interpolation=cv2.INTER_AREA)
    else:
        small, a = frame, alpha
    e = plate.evidence(small, a.astype(np.float32) / 255.0)
    if e is not None:
        if (w, h) != (pw, ph):
            e = cv2.resize(e, (w, h), interpolation=cv2.INTER_LINEAR)
        alpha = (alpha.astype(np.float32) * (1.0 - e) + 0.5).astype(np.uint8)
    return plate.clean_edge(frame, alpha), alpha


def zoom_original(frame: np.ndarray, z: float, focus_y: float) -> np.ndarray:
    """The camera zoom of compose_final (crop centered on x, eye line kept) on a full frame."""
    if abs(z - 1.0) < 1e-4:
        return frame
    cv2 = _cv2()
    H, W = frame.shape[:2]
    top = H * focus_y * (1 - 1 / z)
    left = W * 0.5 * (1 - 1 / z)
    m = np.array([[z, 0.0, -left * z], [0.0, z, -top * z]], np.float32)
    return cv2.warpAffine(frame, m, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# ================================================================== the clip

@dataclass
class Job:
    cfg: Config
    segments: list[Segment]
    mattes: dict[int, "M.RangeJob"]              # segment index -> matte job
    base: Path
    base_pre: list[str]
    base_fps: float
    fps: int
    size: tuple[int, int]                          # render size
    full_size: tuple[int, int]                     # output size (the mattes are stored at it)
    nf: int
    out_dir: Path
    extra_key: list = field(default_factory=list)
    plates: dict[int, RoomPlate] = field(default_factory=dict)     # segment index -> the room plate of its take

    def key(self) -> str:
        blob = [TV_VERSION, M.engine_version(), str(self.base), M.file_signature(self.base), self.base_pre,
                self.fps, self.size, self.full_size, self.nf, self.cfg.position, round(self.cfg.height, 5),
                self.cfg.outline and [list(self.cfg.outline[0]), self.cfg.outline[1]], self.cfg.shadow,
                self.cfg.blurred_original, self.cfg.in_face, self.cfg.mirror, self.cfg.runs, EDGE_FADE, SIDE_FADE,
                [b.signature() for b in self.cfg.backgrounds], LOOP_FADE, KB_HOLD_AFTER, EXTEND, BACKDROP,
                [[s.f0, s.f1, s.mode, round(s.zoom, 5), round(s.push, 5), round(s.scale, 6), s.flush_left,
                  s.flush_right, round(s.focus_y, 5), s.stats, s.side, s.fill, s.drop] for s in self.segments],
                {k: j.key for k, j in self.mattes.items()}, {k: p.key for k, p in self.plates.items()},
                self.extra_key]
        return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _encoder(tmp: Path, W: int, H: int, fps: int, n: int) -> subprocess.Popen:
    return subprocess.Popen(
        M._ff() + ["-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f'{W}x{H}', "-framerate", str(fps), "-i", "-",
                   "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(fps),
                   "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                   "-frames:v", str(n), "-f", "mp4", str(tmp)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def render(job: Job, progress: Callable[[str], None] | None = None, workers: int | None = None) -> dict:
    """Write the green screen clip (cached). -> {file, cached, seconds, frames, key}."""
    started = time.time()
    W, H = job.size
    key = job.key()
    job.out_dir.mkdir(parents=True, exist_ok=True)
    out = job.out_dir / f'tv_{key}.mp4'
    meta_path = out.with_suffix(".json")
    if out.exists() and meta_path.exists():
        meta = M.load_json(meta_path) or {}
        if meta.get("key") == key and out.stat().st_size > 0:
            return {**meta, "file": str(out), "cached": True, "render_seconds": round(time.time() - started, 2)}
    cfg = job.cfg
    k = W / 1080.0
    look = Look(W=W, H=H, shadow=cfg.shadow, fade=EDGE_FADE * k, side_fade=SIDE_FADE * k,
                outline=(np.array(cfg.outline[0], np.float32), cfg.outline[1] * k) if cfg.outline else None)
    base_reader = M.FrameReader(M.base_cmd(job.base, job.base_pre, job.base_fps, job.fps, 0, job.nf, size=(W, H),
                                           full_size=job.full_size), (W, H), 3, "read the cut", queue_len=8)
    bgs = BackgroundTrack(cfg, W, H, job.fps)
    tmp = out.with_name(".tmp-" + out.name)
    enc = _encoder(tmp, W, H, job.fps, job.nf)
    workers = workers or max(2, min(6, (os.cpu_count() or 4) // 2))
    pool = ThreadPoolExecutor(max_workers=workers)
    pending: deque = deque()
    written = 0
    last_frame = None
    last_print = time.time()

    def flush(block: bool) -> None:
        nonlocal written
        while pending and (block or pending[0].done()):
            fr = pending.popleft().result()
            enc.stdin.write(fr.tobytes())
            written += 1

    def person_frame(make_bg, person, alpha, seg, f, plate):
        s, x0, y0 = layer_rect(cfg, seg, seg.zoom_at(f), W, H)
        if plate is not None:
            person, alpha = without_room(plate, person, alpha)
        if cfg.mirror:
            cv2 = _cv2()
            person, alpha = cv2.flip(person, 1), cv2.flip(alpha, 1)
        return compose_person(make_bg(), person, alpha, s, x0, y0, look)

    try:
        f = 0
        for si, seg in enumerate(job.segments):
            mreader = None
            mjob = job.mattes.get(si)
            plate = job.plates.get(si)
            if seg.mode == "person" and mjob is not None:
                mreader = M.FrameReader(M.matte_cmd(mjob.path, (W, H), job.full_size), (W, H), 1, "read the cutout",
                                        queue_len=8)
            last_alpha = None
            try:
                for f in range(seg.f0, seg.f1):
                    frame = base_reader.read()
                    if frame is None:
                        frame = last_frame
                    if frame is None:
                        raise CutoutError('could not read the cut for green-screen composition', 1)
                    last_frame = frame
                    if seg.mode == "original":
                        z, fy = seg.zoom_at(f), seg.focus_y
                        pending.append(pool.submit(zoom_original, frame, z, fy))
                    else:
                        make_bg = bgs.fetch(f, frame)
                        alpha = mreader.read() if mreader is not None else None
                        if alpha is None:
                            alpha = last_alpha
                        if alpha is None:
                            pending.append(pool.submit(make_bg))
                        else:
                            last_alpha = alpha
                            pending.append(pool.submit(person_frame, make_bg, frame, alpha, seg, f, plate))
                    flush(len(pending) >= 2 * workers)
                    now = time.time()
                    if progress and now - last_print >= M.PROGRESS_EVERY:
                        last_print = now
                        progress(f'green screen: composing {100.0 * (f + 1) / job.nf:.0f}%')
            finally:
                if mreader is not None:
                    mreader.close()
        flush(True)
    except BrokenPipeError:
        pass
    finally:
        pool.shutdown(wait=True)
        base_reader.close()
        bgs.close()
        try:
            enc.stdin.close()
        except OSError:
            pass
        err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        code = enc.wait()
    if code != 0 or written != job.nf:
        tmp.unlink(missing_ok=True)
        raise CutoutError(f'failed to write green screen ({written} of {job.nf} frames): {err.strip()[-300:]}', 1)
    os.replace(tmp, out)
    seconds = time.time() - started
    meta = {"key": key, "frames": job.nf, "size": [W, H], "fps": job.fps, "seconds": round(seconds, 2),
            "created_at": M.now_iso()}
    M.save_json(meta_path, meta)
    return {**meta, "file": str(out), "cached": False, "render_seconds": round(seconds, 2)}


def plan_stages(cfg: Config, segs: list[Segment], W: int, H: int,
                reserve: Callable[[list[tuple[int, int]]], float] | None = None) -> None:
    """Place every background shown whole (contain fit). Its stage is the band from STAGE_TOP to a little
    above the highest head top of the person segments it is shown with; when auto captions play over it and
    the picture keeps at least RESERVE_KEEP of the size it would have without them, the stage leaves room for
    them right above the head. Needs cfg.runs; sets b.place and b.stage (render px)."""
    gap = STAGE_GAP * H
    top = STAGE_TOP * H
    for k, b in enumerate(cfg.backgrounds):
        b.place, b.stage = None, None
        if b.fit != "contain" or not b.size:
            continue
        spans = [(f0, f1) for f0, f1, kk in cfg.runs if kk == k]
        if not spans:
            continue
        heads = []
        for s in segs:
            if s.mode != "person" or not s.stats or not s.stats.get("person_frames"):
                continue
            if not any(s.f0 < b1 and a0 < s.f1 for a0, b1 in spans):
                continue
            z = screen_zones(cfg, s, W, H)
            if z["person"]:
                heads.append(z["person"]["y0"] * H)
        head = min(heads) if heads else H * (1.0 - cfg.height)
        bottom = max(top + 0.1 * H, head - gap)
        iw, ih = b.size
        need = (b.content[3] - b.content[1]) * ih * W / iw          # main area height at the full width
        room = reserve(spans) if reserve else 0.0
        # a video with a plain border melts into the screen: it stays wide (down to QUIET_MIN, to leave the
        # captions their band; what still does not fit above the head goes behind the captions and the person)
        # instead of becoming a small box
        least = QUIET_MIN if (b.kind == "video" and b.quiet) else WHOLE_MIN

        def size(h: float) -> float:
            return min(need, max(h, least * need))
        if room > 0 and size(bottom - room - top) >= RESERVE_KEEP * size(bottom - top):
            bottom -= room
        b.stage = (round(top, 1), round(bottom, 1))
        b.place = whole_place(b.size, b.content, b.stage, W, H, least)


def image_zones(cfg: Config, segs: list[Segment], W: int, H: int) -> list[tuple[int, int, tuple]]:
    """(f0, f1, rect) where the main area of a whole picture is on the screen (render px), outside the segments
    shown as recorded (the background is not seen there)."""
    out = []
    shown_as_recorded = [(s.f0, s.f1) for s in segs if s.mode == "original"]
    for f0, f1, k in cfg.runs:
        if k is None:
            continue
        rect = cfg.backgrounds[k].content_rect(W, H)
        if rect is None:
            continue
        pieces = [(f0, f1)]
        for a, b in shown_as_recorded:
            nxt = []
            for p0, p1 in pieces:
                if b <= p0 or a >= p1:
                    nxt.append((p0, p1))
                    continue
                if p0 < a:
                    nxt.append((p0, a))
                if b < p1:
                    nxt.append((b, p1))
            pieces = nxt
        out += [(p0, p1, rect) for p0, p1 in pieces if p1 > p0]
    return out


def report_backgrounds(cfg: Config, fps: int, W: int, H: int, scale_to_out: float,
                       rel: Callable[[Path], str]) -> list[dict]:
    """The background runs for the report (what shows when, from background or from an insert, and where)."""
    out = []
    for f0, f1, k in cfg.runs:
        b = cfg.backgrounds[k] if k is not None else None
        row = {"start": round(f0 / fps, 3), "end": round(f1 / fps, 3),
               "src": (rel(b.src) if b and b.src else (b.asked if b else
                       ("blurred original" if cfg.blurred_original else DARK_DEFAULT))),
               "item": (b.idx if b and not b.insert else None), "insert": (b.insert if b else None),
               "kind": b.kind if b else ("original" if cfg.blurred_original else "color"),
               "fit": b.fit if b else None,
               "ken_burns": b.ken_burns if b else False, "darken": b.darken if b else None,
               "blur": b.blur if b else None}
        if b and b.place:
            row["rect"] = [int(round(v * scale_to_out)) for v in b.place]
            row["content"] = [int(round(v * scale_to_out)) for v in b.content_rect(W, H)]
        if b and b.kind == "video":
            row["smooth_edge"] = b.quiet
            row['empty_frames'] = {"start": b.lead, "end": b.tail}
        out.append(row)
    return out


def report_segments(cfg: Config, segs: list[Segment], fps: int, W: int, H: int, scale_to_out: float) -> list[dict]:
    out = []
    for s in segs:
        row = {"start": round(s.f0 / fps, 3), "end": round(s.f1 / fps, 3), "mode": s.mode}
        if s.mode == "person" and cfg.mirror:
            row["mirrored"] = True
        if s.side:
            row["position"] = s.side
        if s.fill:
            row['full_width'] = True
            row['bottom_crop'] = s.drop
        if s.mode == "person" and s.stats and s.stats.get("person_frames"):
            sc, x0, y0 = layer_rect(cfg, s, s.zoom, W, H)
            lw = W * sc
            row.update({"scale": round(sc, 4), "x": int(round(x0 * scale_to_out)), "y": int(round(y0 * scale_to_out)),
                        "zoom": s.zoom, "push_in": s.push,
                        "on_screen_edge": [n for n, on in (("left", s.flush_left), ("right", s.flush_right)) if on],
                        "height_share": round((H - (y0 + float(s.stats.get("top", 0)) * H * sc)) / H, 3),
                        "width_share": round((float(s.stats.get("right", 1)) - float(s.stats.get("left", 0))) * lw / W, 3),
                        "cut_edge_inside": [side for side, cond in (("left", s.stats.get("touch_left", 0) > 0.05 and x0 > 0.5),
                                                                    ("right", s.stats.get("touch_right", 0) > 0.05 and x0 + lw < W - 0.5))
                                            if cond]})
        if s.note:
            row["note"] = s.note
        out.append(row)
    return out

