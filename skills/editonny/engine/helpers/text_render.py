'Render captions, hooks, calls to action, and disclaimers into transparent RGBA text layers.\n\nPillow draws output-sized states later composited through FFmpeg overlay. Public operations include hook/caption/CTA/disclaimer rendering, caption tracks, sample frames, ffconcat output, text sizing, canvas layout, and unsupported-character diagnostics.\n\nHook markup uses paired asterisks for accent words. Markup never splits a word; an escaped asterisk is literal and an unmatched marker is reported. CTA and disclaimer text do not use accent markup. Unsupported font characters are substituted where a clear equivalent exists or removed with a diagnostic rather than rendered as missing-glyph boxes.\n\nCLI operations: list, hook, caption, cta, disclaimer, and track. Supply --style and an output path; optional accent, background, active-word, duration, frame-rate, and canvas-size controls appear in --help. User text and Unicode remain data; the English interface does not authorize rewriting the recorded words.'

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SAFE_ZONE_VERTICAL, hex_to_rgb, load_font, load_style_catalog  # noqa: E402

REF_W, REF_H = 1080, 1920
SIDE_MARGIN = 90
DEFAULT_ACCENT = "#FFC400"

# Preferred text positions and the platform-safe band per canvas shape, as fractions of the
# canvas height. The vertical row is the validated 1080x1920 layout (SAFE_ZONE_VERTICAL). Type
# keeps its size on a horizontal or square canvas (it scales with the short side), so scaling the
# vertical y values down would squeeze full-size layers into each other: those shapes get their
# own rows.
LAYOUT_FRACTIONS: dict[str, dict[str, float]] = {
    "vertical": {"top": SAFE_ZONE_VERTICAL["top"] / REF_H, "bottom": SAFE_ZONE_VERTICAL["bottom"] / REF_H,
                 "hook": 430 / REF_H, "caption": SAFE_ZONE_VERTICAL["caption_center_y"] / REF_H,
                 "cta": 1110 / REF_H, "cta_alone": 1180 / REF_H, "disclaimer": SAFE_ZONE_VERTICAL["disclaimer_y"] / REF_H},
    "horizontal": {"top": 0.06, "bottom": 0.965, "hook": 0.2, "caption": 0.78, "cta": 0.45, "cta_alone": 0.5,
                   "disclaimer": 0.935},
    "square": {"top": 0.07, "bottom": 0.955, "hook": 0.2, "caption": 0.76, "cta": 0.46, "cta_alone": 0.52,
               "disclaimer": 0.93},
}


def canvas_shape(canvas) -> str:
    w, h = int(canvas[0]), int(canvas[1])
    if h > w * 1.05:
        return "vertical"
    if w > h * 1.05:
        return "horizontal"
    return "square"


def layout_defaults(canvas) -> dict[str, float]:
    """Preferred centers and the safe band in pixels of `canvas`: top, bottom, hook, caption, cta
    (with captions), cta_alone (without captions), disclaimer."""
    h = float(canvas[1])
    return {k: v * h for k, v in LAYOUT_FRACTIONS[canvas_shape(canvas)].items()}
# Hash of this file: part of every cached PNG name, so a code change never reuses stale art.
_SRC_HASH = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10]

WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
INK = (17, 17, 17, 255)

# Portuguese function words that read badly at the end of a line or cue.
WEAK_WORDS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos", "em", "no",
    "na", "nos", "nas", "e", "ou", "que", "pra", "para", "pro", "pros", "pras", "por", "pelo",
    "pela", "pelos", "pelas", "com", "sem", "se", "mas", "meu", "minha", "meus", "minhas", "seu",
    "sua", "seus", "suas", "te", "me", "lhe", "não", "nao", "é", "ao", "aos", "à", "às", "num",
    "numa", "tipo", "cada", "esse", "essa", "este", "esta", "isso", "the",
}
WEAK_WORDS |= {"the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "with", "at", "by"}



# ============================================================== small utilities

_tls = threading.local()


def _font(key: str, size: float):
    """Per-thread cached bundled font (FreeType faces are not shared across threads)."""
    cache = getattr(_tls, "fonts", None)
    if cache is None:
        cache = _tls.fonts = {}
    px = max(6, int(round(size)))
    f = cache.get((key, px))
    if f is None:
        f = cache[(key, px)] = load_font(key, px)
    return f


def _cap_height(font) -> float:
    cache = getattr(_tls, "caps", None)
    if cache is None:
        cache = _tls.caps = {}
    key = (font.path, font.size, id(font))
    v = cache.get(key)
    if v is None:
        _, top, _, _ = font.getbbox("H", anchor="ls")
        v = cache[key] = float(-top)
    return v


def _rgba(color, alpha: int = 255) -> tuple[int, int, int, int]:
    if isinstance(color, str):
        r, g, b = hex_to_rgb(color)
        return (r, g, b, alpha)
    if len(color) == 3:
        return (color[0], color[1], color[2], alpha)
    return tuple(color)  # type: ignore[return-value]


def _luminance(rgb) -> float:
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _on_color(bg) -> tuple[int, int, int, int]:
    """Readable text color on top of a solid background color."""
    lum = _luminance(bg)
    contrast_black = (lum + 0.05) / 0.05
    contrast_white = 1.05 / (lum + 0.05)
    return INK if contrast_black >= contrast_white else WHITE


def _new(w: float, h: float) -> Image.Image:
    return Image.new("RGBA", (max(1, int(math.ceil(w))), max(1, int(math.ceil(h)))), (0, 0, 0, 0))


def _mul_alpha(img: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * factor)))
    return out


def _rotate(img: Image.Image, angle: float) -> tuple[Image.Image, float, float]:
    """Rotate around the center with premultiplied alpha (no dark fringes).

    Returns (image, dx, dy): offset of the new center relative to the old one is zero, so an
    anchor (ax, ay) maps to (ax' , ay') = rotate((ax-cx, ay-cy)) + new center.
    """
    if abs(angle) < 0.01:
        return img, 0.0, 0.0
    pm = img.convert("RGBa")
    rot = pm.rotate(angle, resample=Image.BICUBIC, expand=True).convert("RGBA")
    return rot, (rot.width - img.width) / 2.0, (rot.height - img.height) / 2.0


def _rotate_point(x: float, y: float, cx: float, cy: float, angle: float) -> tuple[float, float]:
    a = math.radians(angle)
    dx, dy = x - cx, y - cy
    # PIL rotates counter-clockwise for positive angles (y axis pointing down).
    return cx + dx * math.cos(a) + dy * math.sin(a), cy - dx * math.sin(a) + dy * math.cos(a)


def _shape(w: int, h: int, draw_fn, color, ss: int = 4) -> Image.Image:
    """Anti-aliased filled shape: draw_fn(draw, scale) on a supersampled L mask."""
    w, h = max(1, int(w)), max(1, int(h))
    mask = Image.new("L", (w * ss, h * ss), 0)
    draw_fn(ImageDraw.Draw(mask), ss)
    mask = mask.resize((w, h), Image.LANCZOS)
    rgba = _rgba(color)
    solid = Image.new("RGBA", (w, h), rgba)
    if rgba[3] < 255:
        mask = mask.point(lambda v, a=rgba[3]: v * a // 255)
    solid.putalpha(mask)
    return solid


def _rounded_box(layer: Image.Image, box, radius: float, color) -> None:
    x0, y0, x1, y1 = box
    ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
    w, h = int(math.ceil(x1 - ix0)), int(math.ceil(y1 - iy0))
    fx0, fy0 = x0 - ix0, y0 - iy0
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))

    def fn(d, s):
        d.rounded_rectangle((fx0 * s, fy0 * s, (x1 - ix0) * s - 1, (y1 - iy0) * s - 1), radius=r * s, fill=255)

    _alpha_paste(layer, _shape(w, h, fn, color), ix0, iy0)


def _polygon(layer: Image.Image, points, color) -> None:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ix0, iy0 = int(math.floor(min(xs))), int(math.floor(min(ys)))
    w, h = int(math.ceil(max(xs) - ix0)) + 1, int(math.ceil(max(ys) - iy0)) + 1

    def fn(d, s):
        d.polygon([((x - ix0) * s, (y - iy0) * s) for x, y in points], fill=255)

    _alpha_paste(layer, _shape(w, h, fn, color), ix0, iy0)


def _alpha_paste(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    """alpha_composite that accepts negative / overflowing positions."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dst.width, x + src.width), min(dst.height, y + src.height)
    if x1 <= x0 or y1 <= y0:
        return
    crop = src.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    dst.alpha_composite(crop, (x0, y0))


def _shadow_of(layer: Image.Image, blur: float, alpha: float, color=(0, 0, 0)) -> Image.Image:
    """Soft shadow image (same size) from the alpha of `layer`."""
    a = layer.getchannel("A")
    if blur > 0.5:
        a = a.filter(ImageFilter.GaussianBlur(blur))
    if alpha < 0.999:
        a = a.point(lambda v: int(v * alpha))
    sh = Image.new("RGBA", layer.size, _rgba(color))
    sh.putalpha(a)
    return sh


# ============================================================== geometry

@dataclass(frozen=True)
class _Geo:
    w: int
    h: int

    @property
    def k(self) -> float:          # type scale (1.0 on 1080 wide vertical)
        return min(self.w, self.h) / REF_W

    @property
    def sy(self) -> float:
        return self.h / REF_H

    @property
    def sx(self) -> float:
        return self.w / REF_W

    @property
    def cx(self) -> float:
        return self.w / 2.0

    @property
    def caption_max_w(self) -> float:
        rail = SAFE_ZONE_VERTICAL["right_rail"] * self.sx
        return min(self.w - 2 * SIDE_MARGIN * self.sx, 2 * (self.w / 2.0 - rail))

    @property
    def safe_top(self) -> float:
        return LAYOUT_FRACTIONS[canvas_shape((self.w, self.h))]["top"] * self.h

    @property
    def safe_bottom(self) -> float:
        return LAYOUT_FRACTIONS[canvas_shape((self.w, self.h))]["bottom"] * self.h


# ============================================================== text helpers

_TRAIL_STRIP = ",.;:…"
_QUOTES = "\"“”«»'‘’()[]{}"


def _clean_word(text: str, keep_punct: bool) -> str:
    t = str(text).strip()
    if keep_punct:
        return t
    t = t.strip(_QUOTES)
    t = t.rstrip(_TRAIL_STRIP).lstrip("¿¡")
    t = t.strip(_QUOTES)
    if t in ("-", "–", "—"):
        return ""
    return t


def _case(text: str, upper: bool) -> str:
    return text.upper() if upper else text


# ---------------------------------------------------------------- glyph coverage

_GLYPHS: dict[tuple[str, str], bool] = {}
_NOTDEF = ""          # private use code point: no bundled font has it, so it shows the .notdef glyph
# look-alikes for symbols some fonts lack (first one the font has wins; nothing fits -> removed)
_SYMBOL_FALLBACKS = {
    "→": ("›", ">"), "←": ("‹", "<"), "➜": ("›", ">"), "№": ("Nº", "No"), "…": ("...",),
    "“": ('"',), "”": ('"',), "„": ('"',), "‘": ("'",), "’": ("'",), "—": ("-",), "–": ("-",),
    "•": ("·", "-"), "×": ("x",),
}


def _has_glyph(font_key: str, ch: str) -> bool:
    """True when the bundled font draws `ch` (its mask and advance differ from the .notdef glyph)."""
    key = (font_key, ch)
    v = _GLYPHS.get(key)
    if v is None:
        f = _font(font_key, 48)
        nd, m = f.getmask(_NOTDEF), f.getmask(ch)
        v = not (m.size == nd.size and bytes(m) == bytes(nd) and abs(f.getlength(ch) - f.getlength(_NOTDEF)) < 0.01)
        _GLYPHS[key] = v
    return v


def sanitize_text(text: str, font_key: str) -> tuple[str, list[str]]:
    """Text the font can draw: look-alikes for simple symbols, unknown ones removed.

    Returns (clean text, removed characters in order, no repeats). Line breaks are kept, other
    spaces become plain spaces and invisible format characters (emoji joiners, variation
    selectors) are dropped silently.
    """
    out: list[str] = []
    removed: list[str] = []
    for ch in unicodedata.normalize("NFC", str(text)):
        if ch == "\n":
            out.append(ch)
            continue
        if ch.isspace():
            out.append(" ")
            continue
        cp = ord(ch)
        if (unicodedata.category(ch) == "Cf" or 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF
                or 0xE0020 <= cp <= 0xE007F):
            continue   # invisible parts of an emoji (joiner, variation selector, skin tone, tag)
        if _has_glyph(font_key, ch):
            out.append(ch)
            continue
        alt = next((a for a in _SYMBOL_FALLBACKS.get(ch, ()) if all(_has_glyph(font_key, c) for c in a)), None)
        if alt is not None:
            out.append(alt)
        elif ch not in removed:
            removed.append(ch)
    clean = "\n".join(re.sub(r" {2,}", " ", ln).strip() for ln in "".join(out).split("\n"))
    return clean.strip("\n"), removed


# ---------------------------------------------------------------- *accent* markup

def _markup_chars(text: str) -> tuple[list[tuple[str, bool]], bool, list[str]]:
    """Characters with their accent flag. `\\*` is a literal asterisk; an unpaired `*` is ignored.
    Returns (chars, has_accent_markup, issues)."""
    s = str(text)
    marks = [i for i, c in enumerate(s) if c == "*" and not (i and s[i - 1] == "\\")]
    issues = []
    if len(marks) % 2:
        issues.append('unpaired asterisk: emphasis was ignored. Use *word* for emphasis or \\\\* for a literal asterisk')
        marks = marks[:-1]
    mark_set = set(marks)
    chars: list[tuple[str, bool]] = []
    accent = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "*":
            chars.append(("*", accent))
            i += 2
            continue
        if i in mark_set:
            accent = not accent
        elif c != "*":
            chars.append((c, accent))
        i += 1
    return chars, bool(marks), issues


def strip_markup(text: str) -> tuple[str, bool]:
    """Text without accent markers (for the CTA and the disclaimer). -> (text, had markers)."""
    s = str(text)
    had = bool(re.search(r"(?<!\\)\*", s))
    return re.sub(r"(?<!\\)\*", "", s).replace("\\*", "*"), had


def is_weak_word(word: str) -> bool:
    'Return whether a recorded-language function word should stay with the following word.'
    return re.sub(r"[^\wÀ-ÿ]", "", word.lower()) in WEAK_WORDS


_is_weak = is_weak_word


def _lerp_keys(keys: list[tuple[float, dict]], p: float) -> dict:
    """Linear interpolation of numeric params between keyframes [(p, {..}), ...]."""
    keys = sorted(keys, key=lambda kv: kv[0])
    if p <= keys[0][0]:
        return dict(keys[0][1])
    for (p0, v0), (p1, v1) in zip(keys, keys[1:]):
        if p0 <= p <= p1:
            t = 0.0 if p1 == p0 else (p - p0) / (p1 - p0)
            return {k: v0[k] + (v1.get(k, v0[k]) - v0[k]) * t for k in v0}
    return dict(keys[-1][1])


def _anim_frames(keys: list[tuple[float, dict]], dur: float, fps: float) -> list[dict]:
    """Per-frame params for an entrance lasting `dur` seconds (last frame = steady state)."""
    if not keys or dur <= 0:
        return []
    n = max(0, int(round(dur * fps)) - 1)
    frames = []
    for j in range(n):
        vals = _lerp_keys(keys, (j + 1) / (n + 1))
        frames.append({k: round(v, 3) for k, v in vals.items()})
    return frames


# ============================================================== caption styles

@dataclass(frozen=True)
class CaptionStyle:
    id: str
    font: str
    size: float                 # px on the 1080-wide reference canvas
    upper: bool
    max_words: int
    lines: int
    animated: bool
    one_word: bool = False
    keep_punct: bool = False
    hpad: float = 0.0           # extra horizontal room per side, ratio of size
    gap: float = 0.3            # extra word gap, ratio of size (added to a space)
    active_scale: float = 1.0   # the spoken word is drawn this much bigger (grows into the gaps)
    fit_margin: float = 0.0     # extra width counted when fitting (rotation, glow), ratio of size
    enter: tuple = ()           # ((p, {param: value}), ...) keyframes
    enter_dur: float = 0.0
    change: tuple = ()          # keyframes when the active word changes
    change_dur: float = 0.0
    draw: str = ""


CAPTION_STYLES: dict[str, CaptionStyle] = {}


def _reg(style: CaptionStyle) -> None:
    CAPTION_STYLES[style.id] = style


_reg(CaptionStyle(
    id="impact", font="display_condensed", size=134, upper=True, max_words=3, lines=1, animated=True,
    hpad=0.09, gap=0.14,
    enter=((0.0, {"scale": 0.70}), (0.34, {"scale": 0.86}), (0.67, {"scale": 1.07}), (1.0, {"scale": 1.0})),
    enter_dur=0.10, draw="impact"))

_reg(CaptionStyle(
    id="word_box", font="sans_black", size=86, upper=True, max_words=3, lines=1, animated=True,
    hpad=0.26, gap=0.36,
    enter=((0.0, {"dy": 34.0, "alpha": 0.0}), (0.34, {"dy": 20.0, "alpha": 0.45}),
           (0.67, {"dy": 6.0, "alpha": 0.9}), (1.0, {"dy": 0.0, "alpha": 1.0})),
    enter_dur=0.10,
    change=((0.0, {"slide": 0.0}), (0.5, {"slide": 0.62}), (1.0, {"slide": 1.0})),
    change_dur=0.10, draw="word_box"))

_reg(CaptionStyle(
    id="giant_word", font="display_condensed", size=196, upper=True, max_words=1, lines=1,
    animated=True, one_word=True, hpad=0.12, gap=0.2,
    enter=((0.0, {"scale": 1.45}), (0.34, {"scale": 1.26}), (0.67, {"scale": 1.07}), (1.0, {"scale": 1.0})),
    enter_dur=0.10, draw="giant_word"))

_reg(CaptionStyle(
    id="pulse", font="sans_black", size=78, upper=True, max_words=3, lines=1, animated=True,
    hpad=0.2, gap=0.42, active_scale=1.12,
    enter=((0.0, {"dy": 40.0, "alpha": 0.0}), (0.34, {"dy": 22.0, "alpha": 0.5}),
           (0.67, {"dy": 7.0, "alpha": 0.9}), (1.0, {"dy": 0.0, "alpha": 1.0})),
    enter_dur=0.10, draw="pulse"))

_reg(CaptionStyle(
    id="tape", font="sans_black", size=78, upper=True, max_words=3, lines=1, animated=True,
    hpad=0.42, gap=0.3, fit_margin=0.08,
    enter=((0.0, {"wipe": 0.15}), (0.34, {"wipe": 0.45}), (0.67, {"wipe": 0.82}), (1.0, {"wipe": 1.0})),
    enter_dur=0.10, draw="tape"))

_reg(CaptionStyle(
    id="clean_caption", font="text_bold", size=60, upper=False, max_words=8, lines=2, animated=False,
    keep_punct=True, hpad=0.45, gap=0.0, draw="clean_caption"))

# Handwritten captions appear word by word with a thick dark outline; the spoken word
# uses the accent color and grows slightly. The paper variant uses dark letters with
# a white halo, while the quotation style identifies another speaker.
_reg(CaptionStyle(
    id="paper", font="sans_black", size=88, upper=False, max_words=3, lines=1, animated=True,
    hpad=0.18, gap=0.34, active_scale=1.08,
    enter=((0.0, {"dy": 26.0, "alpha": 0.0}), (0.4, {"dy": 10.0, "alpha": 0.7}),
           (1.0, {"dy": 0.0, "alpha": 1.0})),
    enter_dur=0.09,
    change=((0.0, {"pop": 0.0}), (0.45, {"pop": 1.0}), (1.0, {"pop": 0.0})),
    change_dur=0.16, draw="paper"))

_reg(CaptionStyle(
    id="paper_light", font="sans_black", size=88, upper=False, max_words=3, lines=1, animated=True,
    hpad=0.18, gap=0.34, active_scale=1.08,
    enter=((0.0, {"dy": 26.0, "alpha": 0.0}), (0.4, {"dy": 10.0, "alpha": 0.7}),
           (1.0, {"dy": 0.0, "alpha": 1.0})),
    enter_dur=0.09,
    change=((0.0, {"pop": 0.0}), (0.45, {"pop": 1.0}), (1.0, {"pop": 0.0})),
    change_dur=0.16, draw="paper_light"))

_reg(CaptionStyle(
    id="testimonial", font="sans_black", size=82, upper=False, max_words=3, lines=1, animated=True,
    hpad=0.18, gap=0.34, active_scale=1.06,
    enter=((0.0, {"dy": 22.0, "alpha": 0.0}), (0.5, {"dy": 8.0, "alpha": 0.8}),
           (1.0, {"dy": 0.0, "alpha": 1.0})),
    enter_dur=0.09, draw="testimonial"))


# ---------------------------------------------------------------- measuring

def caption_style_ids() -> list[str]:
    return list(CAPTION_STYLES)


def _caption_spec(style_id: str) -> CaptionStyle:
    spec = CAPTION_STYLES.get(style_id)
    if spec is None:
        raise ValueError(f"caption style '{style_id}' does not exist. Options: {', '.join(CAPTION_STYLES)}")
    return spec


def caption_style_info(style_id: str) -> dict:
    s = _caption_spec(style_id)
    return {"id": s.id, "max_words": s.max_words, "lines": s.lines, "animated": s.animated,
            "upper": s.upper, "one_word": s.one_word, "keep_punct": s.keep_punct}


def display_words(words: list[str], style_id: str) -> list[str]:
    """Words as a style shows them (casing + punctuation cleanup). Empty words are dropped."""
    s = _caption_spec(style_id)
    out = []
    for w in words:
        t = _display_word(w, s)
        if t:
            out.append(t)
    return _trim_cue_end(out, s)


def _display_word(word: str, spec: CaptionStyle) -> str:
    t = _case(_clean_word(word, spec.keep_punct), spec.upper)
    return " ".join(sanitize_text(t, spec.font)[0].split()) if t else t


def _trim_cue_end(words: list[str], spec: CaptionStyle) -> list[str]:
    """Subtitle-like styles keep punctuation, but a cue should not end on a dangling comma."""
    if spec.keep_punct and words:
        last = words[-1].rstrip(",;:")
        words = words[:-1] + [last] if last else words[:-1]
    return words


def _line_width(font, words: list[str], size: float, spec: CaptionStyle) -> float:
    if not words:
        return 0.0
    space = font.getlength(" ")
    widths = [font.getlength(w) for w in words]
    grow = (spec.active_scale - 1.0) * max(widths)
    return (sum(widths) + (len(words) - 1) * (space + spec.gap * size) + 2 * spec.hpad * size
            + grow + 2 * spec.fit_margin * size)


def caption_width_limit(style_id: str, canvas=(REF_W, REF_H)) -> float:
    return _Geo(*canvas).caption_max_w


def measure_caption_width(words: list[str], style_id: str, canvas=(REF_W, REF_H),
                          font_scale: float = 1.0, already_display: bool = False) -> float:
    """Single-line width in px of `words` at the style's base size (before any shrink-to-fit)."""
    spec = _caption_spec(style_id)
    geo = _Geo(*canvas)
    disp = words if already_display else display_words(words, style_id)
    size = spec.size * geo.k * font_scale
    return _line_width(_font(spec.font, size), disp, size, spec)


def _fit_size(spec: CaptionStyle, lines: list[list[str]], base: float, max_w: float) -> float:
    size = base
    for _ in range(3):
        f = _font(spec.font, size)
        widest = max(_line_width(f, ln, size, spec) for ln in lines)
        if widest <= max_w:
            break
        size = max(8.0, math.floor(size * max_w / widest * 0.995))
    return size


def _balanced_split(words: list[str], width_fn) -> list[list[str]]:
    if len(words) < 2:
        return [words]
    best, best_cost = None, None
    for s in range(1, len(words)):
        a, b = words[:s], words[s:]
        wa, wb = width_fn(a), width_fn(b)
        cost = max(wa, wb)
        if _is_weak(a[-1]):
            cost *= 1.12
        if wa > wb:
            cost *= 1.01
        if best_cost is None or cost < best_cost:
            best, best_cost = [a, b], cost
    return best  # type: ignore[return-value]


# ---------------------------------------------------------------- block drawing

@dataclass
class _Block:
    layer: Image.Image
    ax: float          # anchor inside the layer that goes to (cx, cy)
    ay: float
    half_h: float      # nominal half height (for safe-zone clamping)
    half_w: float = 0.0  # nominal half width of the ink (for scale clamping)


def _text_layer_1line(font, words, xs, size, cap_h, pad, colors, stroke, stroke_fill, total_w):
    above = 0.36 * size
    below = 0.34 * size
    w = total_w + 2 * pad
    h = cap_h + above + below + 2 * pad
    layer = _new(w, h)
    base_y = pad + above + cap_h
    d = ImageDraw.Draw(layer)
    for x, word, col in zip(xs, words, colors):
        d.text((pad + x, base_y), word, font=font, fill=col, anchor="ls",
               stroke_width=stroke, stroke_fill=stroke_fill)
    return layer, base_y


def _word_xs(font, words, size, spec):
    space = font.getlength(" ") + spec.gap * size
    xs, x = [], spec.hpad * size
    widths = []
    for w in words:
        xs.append(x)
        wl = font.getlength(w)
        widths.append(wl)
        x += wl + space
    total = x - space + spec.hpad * size
    return xs, widths, total


def _draw_impacto(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    cap_h = _cap_height(f)
    stroke = max(2, int(round(size * 0.085)))
    xs, widths, total = _word_xs(f, words, size, spec)
    pad = stroke + int(size * 0.16)
    colors = [_rgba(accent) if i == active else WHITE for i in range(len(words))]
    layer, base_y = _text_layer_1line(f, words, xs, size, cap_h, pad, colors, stroke, BLACK, total)
    sh = _shadow_of(layer, blur=size * 0.07, alpha=0.55)
    out = _new(layer.width, layer.height)
    _alpha_paste(out, sh, 0, int(size * 0.05))
    out.alpha_composite(layer)
    return _Block(out, pad + total / 2.0, base_y - cap_h / 2.0, cap_h / 2.0 + stroke + size * 0.12, total / 2.0 + stroke)


def _draw_box_live(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    cap_h = _cap_height(f)
    stroke = max(2, int(round(size * 0.075)))
    xs, widths, total = _word_xs(f, words, size, spec)
    pad = stroke + int(size * 0.2)
    above, below = 0.36 * size, 0.34 * size
    layer = _new(total + 2 * pad, cap_h + above + below + 2 * pad)
    base_y = pad + above + cap_h
    acc = _rgba(accent)
    padx, pad_top, pad_bot = 0.2 * size, 0.3 * size, 0.24 * size

    def box_for(i):
        return (pad + xs[i] - padx, base_y - cap_h - pad_top, pad + xs[i] + widths[i] + padx, base_y + pad_bot)

    slide = anim.get("slide", 1.0)
    prev = variant.get("prev", -1)
    settled = slide >= 0.999 or prev < 0
    # text ink (white + stroke) for non-active words, shadow from it
    ink = _new(layer.width, layer.height)
    d = ImageDraw.Draw(ink)
    for i, word in enumerate(words):
        if i == active and settled:
            continue
        d.text((pad + xs[i], base_y), word, font=f, fill=WHITE, anchor="ls", stroke_width=stroke, stroke_fill=BLACK)
    box_layer = _new(layer.width, layer.height)
    if 0 <= active < len(words):
        b1 = box_for(active)
        if not settled and 0 <= prev < len(words):
            b0 = box_for(prev)
            t = slide
            b1 = tuple(b0[j] + (b1[j] - b0[j]) * t for j in range(4))
        _rounded_box(box_layer, b1, radius=size * 0.16, color=acc)
    shadow_src = _new(layer.width, layer.height)
    shadow_src.alpha_composite(box_layer)
    shadow_src.alpha_composite(ink)
    sh = _shadow_of(shadow_src, blur=size * 0.09, alpha=0.5)
    _alpha_paste(layer, sh, 0, int(size * 0.06))
    layer.alpha_composite(box_layer)
    layer.alpha_composite(ink)
    if 0 <= active < len(words) and settled:
        ImageDraw.Draw(layer).text((pad + xs[active], base_y), words[active], font=f, fill=_on_color(acc), anchor="ls")
    return _Block(layer, pad + total / 2.0, base_y - cap_h / 2.0, cap_h / 2.0 + pad_top + size * 0.05)


def _draw_word_gigante(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    cap_h = _cap_height(f)
    stroke = max(2, int(round(size * 0.07)))
    xs, widths, total = _word_xs(f, words, size, spec)
    off = max(3, int(round(size * 0.055)))
    pad = stroke + off + int(size * 0.14)
    above, below = 0.36 * size, 0.34 * size
    layer = _new(total + 2 * pad, cap_h + above + below + 2 * pad)
    base_y = pad + above + cap_h
    acc = _rgba(accent)
    d = ImageDraw.Draw(layer)
    for x, word in zip(xs, words):
        # hard offset "sticker" shadow in the accent color, then the white word on top
        for step in range(off, 0, -max(1, off // 4)):
            d.text((pad + x + step, base_y + step), word, font=f, fill=acc, anchor="ls",
                   stroke_width=stroke, stroke_fill=BLACK if step == off else acc)
        d.text((pad + x, base_y), word, font=f, fill=WHITE, anchor="ls", stroke_width=stroke, stroke_fill=BLACK)
    sh = _shadow_of(layer, blur=size * 0.08, alpha=0.45)
    out = _new(layer.width, layer.height)
    _alpha_paste(out, sh, 0, int(size * 0.04))
    out.alpha_composite(layer)
    return _Block(out, pad + total / 2.0, base_y - cap_h / 2.0, cap_h / 2.0 + stroke + off + size * 0.1,
                  total / 2.0 + stroke + off)


def _draw_pulse(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    big = _font(spec.font, size * spec.active_scale)
    cap_h, cap_big = _cap_height(f), _cap_height(big)
    stroke = max(2, int(round(size * 0.075)))
    stroke_big = max(2, int(round(size * spec.active_scale * 0.075)))
    xs, widths, total = _word_xs(f, words, size, spec)
    pad = stroke_big + int(size * 0.3)
    above, below = 0.36 * size * spec.active_scale, 0.34 * size * spec.active_scale
    layer = _new(total + 2 * pad, cap_big + above + below + 2 * pad)
    center_y = pad + above + cap_big / 2.0
    acc = _rgba(accent)
    d = ImageDraw.Draw(layer)
    for i, (word, x, wl) in enumerate(zip(words, xs, widths)):
        if i == active:
            continue
        d.text((pad + x, center_y + cap_h / 2.0), word, font=f, fill=WHITE, anchor="ls",
               stroke_width=stroke, stroke_fill=BLACK)
    if 0 <= active < len(words):
        # the spoken word grows around its own center and may eat into the word gaps
        wl_big = big.getlength(words[active])
        cx_word = pad + xs[active] + widths[active] / 2.0
        d.text((cx_word - wl_big / 2.0, center_y + cap_big / 2.0), words[active], font=big, fill=acc, anchor="ls",
               stroke_width=stroke_big, stroke_fill=BLACK)
    out = _new(layer.width, layer.height)
    _alpha_paste(out, _shadow_of(layer, blur=size * 0.1, alpha=0.8), 0, int(size * 0.07))
    out.alpha_composite(layer)
    return _Block(out, pad + total / 2.0, center_y, cap_big / 2.0 + stroke_big + size * 0.14)


def _paper_block(spec, geo, words, active, accent, size, variant, anim, *,
                     base_color, halo, futuro_alpha: float, active_color=None) -> _Block:
    'Reveal a phrase word by word, withholding words until they are spoken.'
    f = _font(spec.font, size)
    pop = float(anim.get("pop", 0.0) or 0.0)
    scale_value = spec.active_scale + 0.05 * pop
    big = _font(spec.font, size * scale_value)
    cap_h, cap_big = _cap_height(f), _cap_height(big)
    stroke = max(2, int(round(size * 0.085)))
    stroke_big = max(2, int(round(size * scale_value * 0.085)))
    xs, widths, total = _word_xs(f, words, size, spec)
    pad = stroke_big + int(size * 0.3)
    above, below = 0.36 * size * scale_value, 0.34 * size * scale_value
    layer = _new(total + 2 * pad, cap_big + above + below + 2 * pad)
    center_y = pad + above + cap_big / 2.0
    acc = _rgba(active_color or accent)
    d = ImageDraw.Draw(layer)
    for i, (word, x, wl) in enumerate(zip(words, xs, widths)):
        if i == active:
            continue
        if i > active >= 0 and futuro_alpha <= 0.0:
            continue                      # Hide words that have not been spoken yet.
        color_value = base_color if i < active or active < 0 else _fade(base_color, futuro_alpha)
        d.text((pad + x, center_y + cap_h / 2.0), word, font=f, fill=color_value, anchor="ls",
               stroke_width=stroke, stroke_fill=halo)
    if 0 <= active < len(words):
        wl_big = big.getlength(words[active])
        cx_word = pad + xs[active] + widths[active] / 2.0
        d.text((cx_word - wl_big / 2.0, center_y + cap_big / 2.0), words[active], font=big, fill=acc,
               anchor="ls", stroke_width=stroke_big, stroke_fill=halo)
    out = _new(layer.width, layer.height)
    _alpha_paste(out, _shadow_of(layer, blur=size * 0.09, alpha=0.75 if halo == BLACK else 0.35),
                 0, int(size * 0.06))
    out.alpha_composite(layer)
    return _Block(out, pad + total / 2.0, center_y, cap_big / 2.0 + stroke_big + size * 0.14,
                  total / 2.0 + stroke_big)


def _fade(color, alpha: float):
    r, g, b, a = _rgba(color)
    return (r, g, b, int(round(a * max(0.0, min(1.0, alpha)))))


def _draw_paper(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    return _paper_block(spec, geo, words, active, accent, size, variant, anim,
                            base_color=WHITE, halo=BLACK, futuro_alpha=0.0)


def _draw_paper_paper(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    return _paper_block(spec, geo, words, active, accent, size, variant, anim,
                            base_color=(53, 44, 64, 255), halo=(255, 253, 253, 255), futuro_alpha=0.0)


def _draw_testimonial(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    # For quotations, use the accent color for the phrase and white for the spoken word.
    return _paper_block(spec, geo, words, active, accent, size, variant, anim,
                            base_color=_rgba(accent), halo=BLACK, futuro_alpha=0.0, active_color="#FFFFFF")


def _draw_fita(spec, geo, words, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    cap_h = _cap_height(f)
    xs, widths, total = _word_xs(f, words, size, spec)
    pad_top, pad_bot = 0.42 * size, 0.36 * size
    tape_h = cap_h + pad_top + pad_bot
    margin = int(size * 0.3)
    layer = _new(total + 2 * margin, tape_h + 2 * margin)
    x0, y0 = margin, margin
    base_y = y0 + pad_top + cap_h
    acc = _rgba(accent)
    wipe = anim.get("wipe", 1.0)
    tape = _new(layer.width, layer.height)
    _rounded_box(tape, (x0, y0, x0 + total, y0 + tape_h), radius=size * 0.1, color=(12, 12, 12, 242))
    d = ImageDraw.Draw(tape)
    for i, (x, word) in enumerate(zip(xs, words)):
        d.text((x0 + x, base_y), word, font=f, fill=acc if i == active else WHITE, anchor="ls")
    # accent edge on the left, clipped to the tape shape
    edge = Image.new("RGBA", tape.size, acc)
    edge_a = tape.getchannel("A").point(lambda v: 255 if v > 0 else 0)
    ImageDraw.Draw(edge_a).rectangle((int(x0 + size * 0.16), 0, tape.width, tape.height), fill=0)
    edge.putalpha(Image.composite(tape.getchannel("A"), Image.new("L", tape.size, 0), edge_a))
    tape.alpha_composite(edge)
    if wipe < 0.999:
        cut = int(x0 + total * max(0.0, wipe))
        a = tape.getchannel("A")
        ImageDraw.Draw(a).rectangle((cut, 0, tape.width, tape.height), fill=0)
        tape.putalpha(a)
    sh = _shadow_of(tape, blur=size * 0.14, alpha=0.45)
    _alpha_paste(layer, sh, 0, int(size * 0.08))
    layer.alpha_composite(tape)
    ax, ay = x0 + total / 2.0, y0 + tape_h / 2.0
    angle = variant.get("angle", 0.0)
    rot, dx, dy = _rotate(layer, angle)
    cx, cy = layer.width / 2.0, layer.height / 2.0
    rax, ray = _rotate_point(ax, ay, cx, cy, angle)
    return _Block(rot, rax + dx, ray + dy, tape_h / 2.0 + abs(math.sin(math.radians(angle))) * total / 2.0)


def _draw_caption_limpa(spec, geo, lines, active, accent, size, variant, anim) -> _Block:
    f = _font(spec.font, size)
    asc, desc = f.getmetrics()
    pitch = size * 1.22
    widths = [f.getlength(" ".join(ln)) for ln in lines]
    padx, pady = spec.hpad * size, 0.3 * size
    box_w = max(widths) + 2 * padx
    box_h = pitch * (len(lines) - 1) + asc + desc + 2 * pady - 0.12 * size
    margin = int(size * 0.3)
    layer = _new(box_w + 2 * margin, box_h + 2 * margin)
    bx0, by0 = margin, margin
    _rounded_box(layer, (bx0, by0, bx0 + box_w, by0 + box_h), radius=size * 0.32, color=(0, 0, 0, 160))
    d = ImageDraw.Draw(layer)
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        y = by0 + pady + asc - 0.06 * size + pitch * i
        d.text((bx0 + (box_w - wl) / 2.0, y), " ".join(ln), font=f, fill=WHITE, anchor="ls")
    return _Block(layer, bx0 + box_w / 2.0, by0 + box_h / 2.0, box_h / 2.0)


_DRAWERS = {
    "impact": _draw_impacto,
    "word_box": _draw_box_live,
    "giant_word": _draw_word_gigante,
    "pulse": _draw_pulse,
    "tape": _draw_fita,
    "clean_caption": _draw_caption_limpa,
    "paper": _draw_paper,
    "paper_light": _draw_paper_paper,
    "testimonial": _draw_testimonial,
}


def _caption_block(spec: CaptionStyle, geo: _Geo, words: tuple, active: int, accent: str,
                   font_scale: float, variant: dict, anim: dict) -> _Block:
    base = spec.size * geo.k * font_scale
    max_w = geo.caption_max_w
    wl = list(words)
    if spec.lines >= 2:
        f0 = _font(spec.font, base)
        if _line_width(f0, wl, base, spec) <= max_w:
            lines = [wl]
        else:
            lines = _balanced_split(wl, lambda ws: _line_width(f0, ws, base, spec))
        size = _fit_size(spec, lines, base, max_w)
        return _DRAWERS[spec.draw](spec, geo, lines, active, accent, size, variant, anim)
    size = _fit_size(spec, [wl], base, max_w)
    return _DRAWERS[spec.draw](spec, geo, wl, active, accent, size, variant, anim)


def _place(canvas, block: _Block, cx: float, cy: float, anim: dict, geo: _Geo, clamp=True) -> Image.Image:
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    if clamp:
        lo = geo.safe_top + block.half_h
        hi = min(geo.h, geo.safe_bottom) - block.half_h
        if hi >= lo:
            cy = min(max(cy, lo), hi)
    layer, ax, ay = block.layer, block.ax, block.ay
    s = anim.get("scale", 1.0)
    if s > 1.0 and block.half_w > 0:
        # a scaled-up entrance frame must stay inside the caption safe width (right rail too)
        room = geo.caption_max_w / 2.0
        s = max(1.0, min(s, room / block.half_w))
    if abs(s - 1.0) > 1e-3:
        nw, nh = max(1, int(round(layer.width * s))), max(1, int(round(layer.height * s)))
        layer = layer.resize((nw, nh), Image.BICUBIC)
        ax, ay = ax * nw / block.layer.width, ay * nh / block.layer.height
    a = anim.get("alpha", 1.0)
    if a < 0.999:
        layer = _mul_alpha(layer, max(0.0, a))
    dy = anim.get("dy", 0.0) * geo.k
    _alpha_paste(img, layer, int(round(cx - ax)), int(round(cy - ay + dy)))
    return img


# ---------------------------------------------------------------- caption track

def _cue_words(cue: dict) -> list[dict]:
    ws = cue.get("words") or []
    if ws:
        return [{"text": str(w.get("text", "")), "start": float(w["start"]), "end": float(w["end"])} for w in ws]
    toks = str(cue.get("text", "")).split()
    if not toks:
        return []
    s, e = float(cue["start"]), float(cue["end"])
    total = sum(len(t) for t in toks) or 1
    out, t = [], s
    for tok in toks:
        d = (e - s) * len(tok) / total
        out.append({"text": tok, "start": t, "end": t + d})
        t += d
    return out


HOLD_GAP = 0.6     # a cue stays up until the next one when the silence between them is shorter
TAIL_HOLD = 0.3    # otherwise it lingers this long after the last word
INK_ALPHA = 48          # alpha above this counts as drawn when measuring boxes (soft shadow edges do not)
INK_LUT = [255 if v > INK_ALPHA else 0 for v in range(256)]
ONE_WORD_MIN_S = 0.16   # one-word styles: shortest time a word state stays on screen (5 frames at 30 fps)
ONE_WORD_MAX_MERGE = 3  # at most this many words share one state when they are spoken too fast


def _track_states(cues: list[dict], spec: CaptionStyle, fps: float, boundaries: list[float]) -> list[tuple]:
    """Frame-level states: (f0, f1, words_tuple, active, variant_items, anim_items)."""
    def fr(t: float) -> int:
        return int(round(t * fps + 1e-6))

    prepared = []
    for cue in cues:
        raw = _cue_words(cue)
        words = []
        for w in raw:
            disp = _display_word(w["text"], spec)
            if not disp:
                if words:
                    words[-1]["end"] = max(words[-1]["end"], w["end"])
                continue
            words.append({"text": disp, "start": w["start"], "end": max(w["end"], w["start"])})
        if not words:
            continue
        start = float(cue.get("start", words[0]["start"]))
        end = float(cue.get("end", words[-1]["end"]))
        prepared.append({"start": min(start, words[0]["start"]), "end": max(end, words[-1]["end"]), "words": words})
    prepared.sort(key=lambda c: c["start"])

    states: list[tuple] = []
    enter = _anim_frames(list(spec.enter), spec.enter_dur, fps)
    change = _anim_frames(list(spec.change), spec.change_dur, fps)
    for ci, cue in enumerate(prepared):
        nxt = prepared[ci + 1]["start"] if ci + 1 < len(prepared) else None
        disp_end = cue["end"] + TAIL_HOLD
        if nxt is not None:
            if nxt - cue["end"] <= HOLD_GAP:
                disp_end = nxt
            disp_end = min(disp_end, nxt)
        for b in boundaries:
            if cue["end"] - 1e-3 <= b < disp_end:
                disp_end = max(cue["end"], b)
                break
        cs, ce = fr(cue["start"]), fr(disp_end)
        if nxt is not None:
            ce = min(ce, fr(nxt))
        if ce <= cs:
            continue
        words = cue["words"]
        variant: dict = {}
        if spec.draw == "tape":
            variant["angle"] = -2.6 if ci % 2 == 0 else 1.8

        def emit(f0, f1, wt, act, var, entry_frames, entry_start):
            """Split [f0, f1) into animated entrance frames + one steady state."""
            if f1 <= f0:
                return
            g = f0
            while g < f1 and entry_frames and g - entry_start < len(entry_frames):
                j = g - entry_start
                if j >= 0:
                    states.append((g, g + 1, wt, act, tuple(sorted(var.items())), tuple(sorted(entry_frames[j].items()))))
                g += 1
            if g < f1:
                states.append((g, f1, wt, act, tuple(sorted(var.items())), ()))

        if not spec.animated:
            wt = tuple(_trim_cue_end([w["text"] for w in words], spec))
            emit(cs, ce, wt, -1, variant, [], cs)
            continue

        starts = [cs] + [min(max(fr(w["start"]), cs), ce) for w in words[1:]]
        starts = [max(starts[i], starts[i - 1]) if i else starts[i] for i in range(len(starts))]
        if spec.one_word:
            # a word shorter than ONE_WORD_MIN_S flickers (1-3 frames, never seen at full size):
            # it shares the screen with the next word (or the one before, at the end of a cue)
            min_frames = max(3, int(round(ONE_WORD_MIN_S * fps)))
            groups: list[list] = []
            for i, w in enumerate(words):
                f0, f1 = starts[i], (starts[i + 1] if i + 1 < len(words) else ce)
                if groups and groups[-1][1] - groups[-1][0] < min_frames and len(groups[-1][2]) < ONE_WORD_MAX_MERGE:
                    groups[-1][1] = max(groups[-1][1], f1)
                    groups[-1][2].append(w["text"])
                else:
                    groups.append([f0, f1, [w["text"]]])
            if (len(groups) >= 2 and groups[-1][1] - groups[-1][0] < min_frames
                    and len(groups[-2][2]) + len(groups[-1][2]) <= ONE_WORD_MAX_MERGE):
                last = groups.pop()
                groups[-1][1] = last[1]
                groups[-1][2].extend(last[2])
            for f0, f1, texts in groups:
                if f1 <= f0:
                    continue
                # no entrance bounce on a state too short to settle at its final size
                entry = enter if f1 - f0 >= len(enter) + 3 else []
                emit(f0, f1, tuple(texts), 0, variant, entry, f0)
            continue
        for i, w in enumerate(words):
            f0 = starts[i]
            f1 = starts[i + 1] if i + 1 < len(words) else ce
            if f1 <= f0:
                continue
            wt = tuple(x["text"] for x in words)
            if f0 < cs + len(enter):
                # cue entrance frames (take priority over the word-change slide)
                head_end = min(f1, cs + len(enter))
                emit(f0, head_end, wt, i, variant, enter, cs)
                f0 = head_end
                if f0 >= f1:
                    continue
                emit(f0, f1, wt, i, variant, [], f0)
            elif change and i > 0:
                var = dict(variant, prev=i - 1)
                g = f0
                for j, params in enumerate(change):
                    if g >= f1:
                        break
                    states.append((g, g + 1, wt, i, tuple(sorted(var.items())), tuple(sorted(params.items()))))
                    g += 1
                if g < f1:
                    states.append((g, f1, wt, i, tuple(sorted(variant.items())), ()))
            else:
                emit(f0, f1, wt, i, variant, [], f0)
    states.sort(key=lambda s: s[0])
    # safety: never overlap
    clean: list[tuple] = []
    for st in states:
        if clean and st[0] < clean[-1][1]:
            st = (clean[-1][1],) + st[1:]
        if st[1] > st[0]:
            clean.append(st)
    return clean


def _apply_windows(states: list[tuple], fps: float, default_cy: float,
                   layout: list[dict] | None, hide: list[list[float]] | None) -> list[tuple]:
    """-> (f0, f1, words, active, variant, anim, center_y) with hide windows removed."""
    def fr(t: float) -> int:
        return int(round(float(t) * fps + 1e-6))

    lay = sorted(((fr(w["start"]), fr(w["end"]), float(w["center_y"])) for w in (layout or [])), key=lambda x: x[0])
    hid = sorted((fr(a), fr(b)) for a, b in (hide or []))
    cuts = sorted({p for a, b, _ in lay for p in (a, b)} | {p for a, b in hid for p in (a, b)})
    out = []
    for f0, f1, *rest in states:
        pts = [f0] + [c for c in cuts if f0 < c < f1] + [f1]
        for a, b in zip(pts, pts[1:]):
            if any(h0 <= a and b <= h1 for h0, h1 in hid):
                continue
            cy = default_cy
            for l0, l1, lcy in lay:
                if l0 <= a and b <= l1:
                    cy = lcy
            out.append((a, b, *rest, cy))
    return out


def render_caption_track(cues: dict, style_id: str, accent: str, out_dir: str,
                         canvas=(1080, 1920), fps: int = 30,
                         layout: list[dict] | None = None, hide: list[list[float]] | None = None,
                         font_scale: float = 1.0) -> list[dict]:
    """Render every distinct visual state of a caption track to PNGs.

    Returns [{"file": "/abs/png", "start": s, "end": e, "bbox": [x0, y0, x1, y1]}] sorted,
    non-overlapping, only where a caption is visible. `bbox` is the drawn area of that PNG (alpha
    above INK_ALPHA; null when empty). Times are on the frame grid of `fps`. Identical states share one PNG (named by a
    content hash, so re-running with the same input reuses the files; their boxes are kept in
    `bboxes.json` next to them). Default center: layout_defaults(canvas)["caption"].
    """
    spec = _caption_spec(style_id)
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cue_list = cues.get("cues", []) if isinstance(cues, dict) else list(cues)
    boundaries = sorted(float(b) for b in (cues.get("cut_boundaries") or [])) if isinstance(cues, dict) else []
    default_cy = layout_defaults((geo.w, geo.h))["caption"]
    hex_accent = accent or DEFAULT_ACCENT
    _rgba(hex_accent)  # validate early

    states = _track_states(cue_list, spec, float(fps), boundaries)
    placed = _apply_windows(states, float(fps), default_cy, layout, hide)

    # merge consecutive identical states
    merged: list[list] = []
    for st in placed:
        if merged and merged[-1][1] == st[0] and tuple(merged[-1][2:]) == tuple(st[2:]):
            merged[-1][1] = st[1]
        else:
            merged.append(list(st))

    jobs: dict[str, tuple] = {}
    entries = []
    for f0, f1, words, active, variant, anim, cy in merged:
        key_src = repr((_SRC_HASH, style_id, hex_accent.upper(), geo.w, geo.h, round(font_scale, 4),
                        words, active, variant, anim, round(cy, 1)))
        name = "cap_" + hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:16] + ".png"
        path = out / name
        jobs.setdefault(name, (path, words, active, dict(variant), dict(anim), cy))
        entries.append({"file": str(path), "start": round(f0 / fps, 6), "end": round(f1 / fps, 6)})

    bbox_path = out / "bboxes.json"
    try:
        known = json.loads(bbox_path.read_text(encoding="utf-8")) if bbox_path.exists() else {}
    except (OSError, ValueError):
        known = {}
    boxes: dict[str, list | None] = {}

    def work(job):
        path, words, active, variant, anim, cy = job
        if path.exists():
            if path.name in known:
                boxes[path.name] = known[path.name]
                return
            with Image.open(path) as im:
                bb = ink_bbox(im)
            boxes[path.name] = list(bb) if bb else None
            return
        block = _caption_block(spec, geo, words, active, hex_accent, font_scale, variant, anim)
        img = _place((geo.w, geo.h), block, geo.cx, cy, anim, geo)
        bb = ink_bbox(img)
        boxes[path.name] = list(bb) if bb else None
        tmp = path.with_name(".tmp-" + path.name)
        img.save(tmp, "PNG", compress_level=3)
        os.replace(tmp, path)

    workers = max(1, min(8, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs.values()))
    if any(known.get(n, 0) != b for n, b in boxes.items()):
        known.update(boxes)
        tmp = bbox_path.with_name(".tmp-" + bbox_path.name)
        tmp.write_text(json.dumps(known), encoding="utf-8")
        os.replace(tmp, bbox_path)
    for e in entries:
        e["bbox"] = boxes.get(Path(e["file"]).name)
    return entries


def ink_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Box of what is really drawn: alpha above INK_ALPHA (the faint edge of soft shadows is left out)."""
    return img.getchannel("A").point(INK_LUT).getbbox()


_REACH_ABOVE = set("ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÑbdfhklt")
_REACH_BELOW = set("ÇQJgjpqyç,;")


def caption_extent(cues: dict | list, style_id: str, canvas=(1080, 1920), font_scale: float = 1.0,
                   fps: float = 30.0) -> tuple[float, float]:
    """(above, below): how many px a caption track reaches above and below its center line.

    Measured on drawn blocks of the real cues (the widest one, plus one with accents on top and one
    with a cedilla or descender), times the biggest entrance scale and slide of the style, so a
    track can be placed before it is rendered. Empty cues: a sample phrase with accents.
    """
    spec = _caption_spec(style_id)
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    cue_list = cues.get("cues", []) if isinstance(cues, dict) else list(cues or [])
    groups: list[list[str]] = []
    for cue in cue_list:
        words = display_words([w["text"] for w in _cue_words(cue)], style_id)
        groups += [[w] for w in words] if spec.one_word else ([words] if words else [])
    if not groups:
        groups = [display_words(["ÉÇ", "SÓ"], style_id)]
    picks: dict[tuple, list[str]] = {}
    widest, best = groups[0], -1.0
    for g in groups:
        chars = set("".join(g))
        sig = (bool(chars & _REACH_ABOVE), bool(chars & _REACH_BELOW))
        picks.setdefault(sig, g)
        width = measure_caption_width(g, style_id, canvas, font_scale, already_display=True)
        if width > best:
            best, widest = width, g
    picks[("widest",)] = widest
    variant = {"angle": -2.6} if spec.draw == "tape" else {}
    above = below = 0.0
    for words in picks.values():
        block = _caption_block(spec, geo, tuple(words), 0, DEFAULT_ACCENT, font_scale, variant, {})
        bb = ink_bbox(block.layer) or (0, 0, block.layer.width, block.layer.height)
        above, below = max(above, block.ay - bb[1]), max(below, bb[3] - block.ay)
    frames = _anim_frames(list(spec.enter), spec.enter_dur, fps)
    scale = max([1.0] + [float(f.get("scale", 1.0)) for f in frames])
    slide = max([0.0] + [float(f.get("dy", 0.0)) for f in frames if f.get("alpha", 1.0) >= 0.5]) * geo.k
    return above * scale, below * scale + slide


def write_blank_png(path: str | Path, canvas=(1080, 1920)) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (int(canvas[0]), int(canvas[1])), (0, 0, 0, 0)).save(p, "PNG")
    return p


def write_ffconcat(states: list[dict], path: str, total_duration: float, blank_png: str,
                   fps: float | None = None) -> Path:
    """Write an ffconcat playlist that plays the caption states over [0, total_duration].

    Gaps use `blank_png` (created, fully transparent, when it does not exist yet, with the
    size of the first state PNG). The last entry is repeated without duration, as the concat
    demuxer requires. Each entry carries `option framerate` so timestamps are not rounded to
    the image demuxer's default 1/25 time base: pass the output fps when known (exact frame
    grid); otherwise a 1 microsecond time base is used. Durations are derived from
    microsecond-floored cumulative positions, so rounding never drifts.

    Use in ffmpeg (frame-accurate, verified):
        ffmpeg -i base.mp4 -f concat -safe 0 -i captions.ffconcat \\
          -filter_complex "[1:v]format=rgba[cap];[0:v][cap]overlay=0:0:eof_action=pass[v]" \\
          -map "[v]" -map 0:a? ...
    """
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    blank = Path(blank_png).resolve()
    if not blank.exists():
        size = (REF_W, REF_H)
        if states:
            with Image.open(states[0]["file"]) as im:
                size = im.size
        write_blank_png(blank, size)
    total = max(0.0, float(total_duration))
    rate = f'{int(fps)}' if fps and float(fps).is_integer() else (f'{fps}' if fps else "1000000")

    def us(t: float) -> int:
        return int(math.floor(float(t) * 1_000_000 + 1e-3))

    items: list[tuple[str, int, int]] = []   # (file, start_us, end_us)
    cursor = 0
    for st in sorted(states, key=lambda s: s["start"]):
        a, b = us(st["start"]), us(min(st["end"], total) if total else st["end"])
        a = max(a, cursor)
        if b <= a:
            continue
        if a > cursor:
            items.append((str(blank), cursor, a))
        items.append((str(Path(st["file"]).resolve()), a, b))
        cursor = b
    end_us = us(total) if total else cursor
    if end_us > cursor:
        items.append((str(blank), cursor, end_us))
    if not items:
        items.append((str(blank), 0, max(end_us, 1)))

    def q(f: str) -> str:
        return "'" + f.replace("'", "'\\''") + "'"

    lines = ["ffconcat version 1.0"]
    for f, a, b in items:
        lines += [f'file {q(f)}', f'option framerate {rate}', f'duration {(b - a) / 1000000:.6f}']
    last = items[-1][0]
    lines += [f'file {q(last)}', f'option framerate {rate}']
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ============================================================== hooks

@dataclass
class _Tok:
    text: str
    accent: bool


def _parse_marked(text: str, upper: bool, font_key: str | None = None) -> tuple[list[_Tok], bool]:
    """Words with their accent flag. A word touched by the markup is accented as a whole, so
    "*Kiwi*fy" stays one word. With `font_key`, symbols the font cannot draw are removed."""
    chars, marked, _ = _markup_chars(text)
    words: list[list] = []          # [text, accent]
    cur: list[str] = []
    cur_acc = False
    for c, acc in chars + [(" ", False)]:
        if c.isspace():
            if cur:
                words.append(["".join(cur), cur_acc])
            cur, cur_acc = [], False
            continue
        cur.append(c)
        cur_acc = cur_acc or acc
    toks: list[_Tok] = []
    for w, acc in words:
        if font_key:
            w, _ = sanitize_text(w, font_key)
            w = w.replace("\n", " ").strip()
            if not w:
                continue
        w = _case(w, upper)
        if toks and re.fullmatch(r"[,.;:!?…]+", w):
            toks[-1].text += w
        else:
            toks.append(_Tok(w, acc))
    return toks, marked and any(t.accent for t in toks)


_TEXT_FONTS = {"cta": "sans_black", "cta_sub": "text_bold", "disclaimer": "text_semibold"}
_TEXT_LABELS = {"hook": "hook", "cta": "callout", "cta_sub": "callout supporting text",
                "disclaimer": 'disclaimer text', "caption": "captions"}


def text_issues(kind: str, text: str, style_id: str | None = None) -> list[str]:
    """PT-BR warnings for text that will not show up exactly as typed.

    kind: "hook" (needs style_id), "caption" (needs style_id), "cta", "cta_sub", "disclaimer".
    Lists symbols the font cannot draw (they are removed when rendering) and markup problems.
    """
    if not str(text or "").strip():
        return []
    label = _TEXT_LABELS.get(kind, kind)
    if kind == "hook":
        if style_id not in HOOK_STYLES:
            raise ValueError(f"hook style '{style_id}' does not exist. Options: {', '.join(HOOK_STYLES)}")
        font_key = HOOK_STYLES[style_id]["font"]
    elif kind == "caption":
        font_key = _caption_spec(str(style_id)).font
    else:
        font_key = _TEXT_FONTS[kind]
    issues = []
    if kind == "hook":
        chars, _, problems = _markup_chars(text)
        plain = "".join(c for c, _ in chars)
        issues += [f'{label}: {p}' for p in problems]
    elif kind in ("cta", "cta_sub", "disclaimer"):
        plain, had = strip_markup(text)
        if had:
            issues.append(f'{label}: this element does not support emphasis; asterisks were removed')
    else:
        plain = str(text)
    probe = plain.upper() if kind == "cta" else plain
    _, removed = sanitize_text(probe, font_key)
    if removed:
        issues.append(f"{label}: removed glyphs unsupported by the selected font ({' '.join(removed)}). Choose supported glyphs or another font")
    return issues


HOOK_STYLES: dict[str, dict] = {
    "headline": {"font": "display_condensed", "upper": True, "max": 158, "min": 84, "side": 0.08},
    "banner": {"font": "sans_black", "upper": True, "max": 94, "min": 50, "side": 0.34},
    "bubble": {"font": "sans_extrabold", "upper": False, "max": 82, "min": 46, "side": 0.62},
    "marker": {"font": "marker", "upper": True, "max": 104, "min": 56, "side": 0.34},
    "glass": {"font": "sans_extrabold", "upper": False, "max": 78, "min": 44, "side": 0.66},
}


def hook_style_ids() -> list[str]:
    return list(HOOK_STYLES)


def _hook_lines(toks: list[_Tok], font_key: str, content_w: float, max_size: float, min_size: float,
                side_ratio: float) -> tuple[list[list[_Tok]], float]:
    """Pick 1 or 2 lines (balanced by measured width) and the fitted font size."""
    ref = 100.0
    f = _font(font_key, ref)
    space = f.getlength(" ")

    def width(ts):
        return sum(f.getlength(t.text) for t in ts) + space * (len(ts) - 1)

    def size_for(lines):
        widest = max(width(ln) for ln in lines)
        per_px = widest / ref + 2 * side_ratio
        return content_w / per_px if per_px > 0 else max_size

    if not toks:
        return [[]], max_size
    one = [toks]
    s1 = min(max_size, size_for(one))
    if len(toks) == 1:
        return one, max(min(s1, max_size), 1.0)
    best, best_cost = None, None
    for s in range(1, len(toks)):
        a, b = toks[:s], toks[s:]
        wa, wb = width(a), width(b)
        cost = max(wa, wb)
        if _is_weak(a[-1].text):
            cost *= 1.1
        if len(a) == 1 and len(toks) > 3 and len(a[0].text) <= 3:
            cost *= 1.15
        if best_cost is None or cost < best_cost:
            best, best_cost = [a, b], cost
    two = best
    s2 = min(max_size, size_for(two))
    if s1 >= 0.86 * s2:
        lines, size = one, s1
    else:
        lines, size = two, s2
    return lines, max(1.0, size)


def _refit(font_key: str, lines: list[list[_Tok]], size: float, content_w: float, side_ratio: float) -> float:
    for _ in range(4):
        f = _font(font_key, size)
        space = f.getlength(" ")
        widest = max(sum(f.getlength(t.text) for t in ln) + space * (len(ln) - 1) for ln in lines)
        if widest + 2 * side_ratio * size <= content_w:
            return size
        size = math.floor(size * content_w / (widest + 2 * side_ratio * size) * 0.995)
    return size


def _line_colors(lines, marked, accent_rgba, default_rgba, rule: str):
    """Per token colors: marked words get the accent; otherwise a style rule."""
    cols = []
    for li, ln in enumerate(lines):
        row = []
        for ti, t in enumerate(ln):
            if marked:
                row.append(accent_rgba if t.accent else default_rgba)
            elif rule == "second_line" and len(lines) == 2:
                row.append(accent_rgba if li == 1 else default_rgba)
            elif rule in ("second_line", "last_word") and len(lines) == 1 and len(ln) >= 2:
                row.append(accent_rgba if ti == len(ln) - 1 else default_rgba)
            else:
                row.append(default_rgba)
        cols.append(row)
    return cols


def _draw_token_line(d, f, x, base_y, toks, colors, stroke=0, stroke_fill=BLACK):
    space = f.getlength(" ")
    for t, col in zip(toks, colors):
        d.text((x, base_y), t.text, font=f, fill=col, anchor="ls", stroke_width=stroke, stroke_fill=stroke_fill)
        x += f.getlength(t.text) + space


def _tok_width(f, toks) -> float:
    return sum(f.getlength(t.text) for t in toks) + f.getlength(" ") * (len(toks) - 1)


def _hook_manchete(toks, marked, accent, geo, max_width):
    st = HOOK_STYLES["headline"]
    k = geo.k
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], max_width, st["max"] * k, st["min"] * k, side)
    size = _refit(st["font"], lines, size, max_width, side)
    f = _font(st["font"], size)
    cap = _cap_height(f)
    stroke = max(2, int(round(size * 0.075)))
    pitch = cap + size * 0.38
    widths = [_tok_width(f, ln) for ln in lines]
    block_w = max(widths)
    block_h = cap + pitch * (len(lines) - 1)
    pad = int(stroke + size * 0.4)
    layer = _new(block_w + 2 * pad, block_h + 2 * pad)
    acc = _rgba(accent)
    cols = _line_colors(lines, marked, acc, WHITE, "second_line")
    d = ImageDraw.Draw(layer)
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        base_y = pad + cap + pitch * i
        _draw_token_line(d, f, pad + (block_w - wl) / 2.0, base_y, ln, cols[i], stroke, BLACK)
    out = _new(layer.width, layer.height)
    _alpha_paste(out, _shadow_of(layer, blur=size * 0.09, alpha=0.6), 0, int(size * 0.06))
    out.alpha_composite(layer)
    return out, pad + block_w / 2.0, pad + block_h / 2.0, block_h / 2.0 + stroke + size * 0.3


def _hook_banner(toks, marked, accent, geo, max_width):
    st = HOOK_STYLES["banner"]
    k = geo.k
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], max_width, st["max"] * k, st["min"] * k, side)
    size = _refit(st["font"], lines, size, max_width, side)
    f = _font(st["font"], size)
    cap = _cap_height(f)
    padx, pad_top, pad_bot = side * size, 0.36 * size, 0.3 * size
    box_h = cap + pad_top + pad_bot
    gap = 0.1 * size
    widths = [_tok_width(f, ln) for ln in lines]
    block_w = max(widths) + 2 * padx
    block_h = box_h * len(lines) + gap * (len(lines) - 1)
    margin = int(size * 0.35)
    layer = _new(block_w + 2 * margin, block_h + 2 * margin)
    acc = _rgba(accent)
    has_accent_line = [any(t.accent for t in ln) for ln in lines]
    boxes = _new(layer.width, layer.height)
    texts = []
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        if marked:
            bg = acc if has_accent_line[i] else WHITE
        else:
            bg = acc if i == len(lines) - 1 else WHITE
        bw = wl + 2 * padx
        x0 = margin + (block_w - bw) / 2.0
        y0 = margin + i * (box_h + gap)
        _rounded_box(boxes, (x0, y0, x0 + bw, y0 + box_h), radius=size * 0.14, color=bg)
        texts.append((x0 + padx, y0 + pad_top + cap, ln, _on_color(bg)))
    _alpha_paste(layer, _shadow_of(boxes, blur=size * 0.16, alpha=0.4), 0, int(size * 0.08))
    layer.alpha_composite(boxes)
    d = ImageDraw.Draw(layer)
    for x, base_y, ln, col in texts:
        _draw_token_line(d, f, x, base_y, ln, [col] * len(ln))
    angle = -2.0
    ax, ay = margin + block_w / 2.0, margin + block_h / 2.0
    rot, dx, dy = _rotate(layer, angle)
    rax, ray = _rotate_point(ax, ay, layer.width / 2.0, layer.height / 2.0, angle)
    half = block_h / 2.0 + abs(math.sin(math.radians(angle))) * block_w / 2.0
    return rot, rax + dx, ray + dy, half


def _hook_balao(toks, marked, accent, geo, max_width):
    st = HOOK_STYLES["bubble"]
    k = geo.k
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], max_width, st["max"] * k, st["min"] * k, side)
    size = _refit(st["font"], lines, size, max_width, side)
    f = _font(st["font"], size)
    asc, desc = f.getmetrics()
    cap = _cap_height(f)
    pitch = size * 1.24
    padx, pady = side * size, 0.5 * size
    widths = [_tok_width(f, ln) for ln in lines]
    card_w = max(widths) + 2 * padx
    text_h = cap + pitch * (len(lines) - 1)
    card_h = text_h + 2 * pady + 0.22 * size
    tail = size * 0.42
    margin = int(size * 0.5)
    layer = _new(card_w + 2 * margin, card_h + tail + 2 * margin)
    acc = _rgba(accent)
    x0, y0 = margin, margin
    card = _new(layer.width, layer.height)
    # speech-bubble tail (bottom left), then the card
    tx = x0 + card_w * 0.2
    _polygon(card, [(tx, y0 + card_h - 2), (tx + tail * 1.25, y0 + card_h - 2), (tx - tail * 0.1, y0 + card_h + tail)], WHITE)
    _rounded_box(card, (x0, y0, x0 + card_w, y0 + card_h), radius=size * 0.46, color=WHITE)
    _alpha_paste(layer, _shadow_of(card, blur=size * 0.22, alpha=0.38), 0, int(size * 0.1))
    layer.alpha_composite(card)
    d = ImageDraw.Draw(layer)
    top_text = y0 + pady + 0.16 * size
    space = f.getlength(" ")
    on_acc = _on_color(acc)
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        base_y = top_text + cap + pitch * i
        x = x0 + (card_w - wl) / 2.0
        for t in ln:
            tw = f.getlength(t.text)
            col = INK
            if marked and t.accent:
                _rounded_box(layer, (x - size * 0.12, base_y - cap - size * 0.2, x + tw + size * 0.12, base_y + size * 0.2),
                             radius=size * 0.12, color=acc)
                d = ImageDraw.Draw(layer)
                col = on_acc
            d.text((x, base_y), t.text, font=f, fill=col, anchor="ls")
            x += tw + space
    return layer, x0 + card_w / 2.0, y0 + card_h / 2.0, card_h / 2.0 + tail


def _hook_marker(toks, marked, accent, geo, max_width):
    st = HOOK_STYLES["marker"]
    k = geo.k
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], max_width, st["max"] * k, st["min"] * k, side)
    size = _refit(st["font"], lines, size, max_width, side)
    f = _font(st["font"], size)
    cap = _cap_height(f)
    pitch = cap + size * 0.46
    widths = [_tok_width(f, ln) for ln in lines]
    block_w = max(widths) + 2 * side * size
    block_h = cap + pitch * (len(lines) - 1) + 0.5 * size
    margin = int(size * 0.4)
    layer = _new(block_w + 2 * margin, block_h + 2 * margin)
    acc = _rgba(accent)
    strokes = _new(layer.width, layer.height)
    jit = [(0.03, -0.05, 0.06, 0.02), (-0.04, 0.04, -0.02, 0.05)]
    texts = []
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        base_y = margin + 0.25 * size + cap + pitch * i
        sx0 = margin + (block_w - wl) / 2.0 - side * size
        sx1 = sx0 + wl + 2 * side * size
        top, bot = base_y - cap - 0.2 * size, base_y + 0.2 * size
        j = jit[i % 2]
        pts = [(sx0 + j[0] * size, top + j[1] * size), (sx1 + j[2] * size, top + j[3] * size + 0.04 * size),
               (sx1 - 0.06 * size, bot + j[1] * size), (sx0 - 0.04 * size, bot - j[3] * size)]
        _polygon(strokes, pts, acc)
        texts.append((margin + (block_w - wl) / 2.0, base_y, ln))
    _alpha_paste(layer, _shadow_of(strokes, blur=size * 0.14, alpha=0.35), 0, int(size * 0.06))
    layer.alpha_composite(strokes)
    d = ImageDraw.Draw(layer)
    ink = _on_color(acc)
    for x, base_y, ln in texts:
        _draw_token_line(d, f, x, base_y, ln, [ink] * len(ln))
    angle = 2.2
    ax, ay = margin + block_w / 2.0, margin + block_h / 2.0
    rot, dx, dy = _rotate(layer, angle)
    rax, ray = _rotate_point(ax, ay, layer.width / 2.0, layer.height / 2.0, angle)
    return rot, rax + dx, ray + dy, block_h / 2.0 + abs(math.sin(math.radians(angle))) * block_w / 2.0


def _hook_vidro(toks, marked, accent, geo, max_width):
    st = HOOK_STYLES["glass"]
    k = geo.k
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], max_width, st["max"] * k, st["min"] * k, side)
    size = _refit(st["font"], lines, size, max_width, side)
    f = _font(st["font"], size)
    cap = _cap_height(f)
    pitch = size * 1.24
    padx, pady = side * size, 0.62 * size
    widths = [_tok_width(f, ln) for ln in lines]
    panel_w = max(widths) + 2 * padx
    text_h = cap + pitch * (len(lines) - 1) + 0.22 * size
    panel_h = text_h + 2 * pady
    margin = int(size * 0.4)
    layer = _new(panel_w + 2 * margin, panel_h + 2 * margin)
    acc = _rgba(accent)
    x0, y0 = margin, margin
    border = max(2, int(round(size * 0.035)))
    _rounded_box(layer, (x0, y0, x0 + panel_w, y0 + panel_h), radius=size * 0.42, color=(255, 255, 255, 58))
    _rounded_box(layer, (x0 + border, y0 + border, x0 + panel_w - border, y0 + panel_h - border),
                 radius=size * 0.42 - border, color=(14, 14, 16, 196))
    bar_w, bar_h = size * 1.1, size * 0.12
    _rounded_box(layer, (x0 + (panel_w - bar_w) / 2.0, y0 + pady * 0.42, x0 + (panel_w + bar_w) / 2.0, y0 + pady * 0.42 + bar_h),
                 radius=bar_h / 2.0, color=acc)
    cols = _line_colors(lines, marked, acc, WHITE, "none")
    d = ImageDraw.Draw(layer)
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        base_y = y0 + pady + 0.2 * size + cap + pitch * i
        _draw_token_line(d, f, x0 + (panel_w - wl) / 2.0, base_y, ln, cols[i])
    return layer, x0 + panel_w / 2.0, y0 + panel_h / 2.0, panel_h / 2.0


_HOOK_DRAWERS = {
    "headline": _hook_manchete,
    "banner": _hook_banner,
    "bubble": _hook_balao,
    "marker": _hook_marker,
    "glass": _hook_vidro,
}


def hook_fit(text: str, style_id: str, canvas=(1080, 1920), max_width: int = 900) -> dict:
    """How a hook would be laid out: {"lines": [...], "size": px, "min_size": px, "too_long": bool}.

    Hooks never wrap to 3 lines, so a long text only gets smaller; `too_long` tells the agent to
    shorten it.
    """
    if style_id not in HOOK_STYLES:
        raise ValueError(f"hook style '{style_id}' does not exist. Options: {', '.join(HOOK_STYLES)}")
    st = HOOK_STYLES[style_id]
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    width = min(float(max_width), geo.w - 2 * SIDE_MARGIN * geo.sx)
    toks, _ = _parse_marked(text, st["upper"], st["font"])
    side = st["side"]
    lines, size = _hook_lines(toks, st["font"], width, st["max"] * geo.k, st["min"] * geo.k, side)
    size = _refit(st["font"], lines, size, width, side) if toks else size
    return {"lines": [" ".join(t.text for t in ln) for ln in lines], "size": int(size),
            "min_size": int(st["min"] * geo.k), "too_long": size < st["min"] * geo.k}


def render_hook(text: str, style_id: str, accent: str, canvas=(1080, 1920), center_y: int = 430,
                max_width: int = 900) -> Image.Image:
    """Hook headline: always 1-2 lines, balanced by measured width, font size fitted."""
    if style_id not in HOOK_STYLES:
        raise ValueError(f"hook style '{style_id}' does not exist. Options: {', '.join(HOOK_STYLES)}")
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    max_width = min(float(max_width), geo.w - 2 * SIDE_MARGIN * geo.sx)
    toks, marked = _parse_marked(text, HOOK_STYLES[style_id]["upper"], HOOK_STYLES[style_id]["font"])
    if not toks:
        return Image.new("RGBA", (geo.w, geo.h), (0, 0, 0, 0))
    layer, ax, ay, half = _HOOK_DRAWERS[style_id](toks, marked, accent or DEFAULT_ACCENT, geo, max_width)
    return _place((geo.w, geo.h), _Block(layer, ax, ay, half), geo.cx, float(center_y), {}, geo)


# ============================================================== CTA and disclaimer

def render_cta(text: str, sub: str | None, accent: str, canvas=(1080, 1920),
               center_y: float | None = None) -> Image.Image:
    """Call-to-action button (accent pill + arrow) with an optional line below.

    Default center y: layout_defaults(canvas)["cta_alone"] (1180 on 1080x1920). Asterisks and
    symbols the font cannot draw are removed (text_issues reports them)."""
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    k = geo.k
    acc = _rgba(accent or DEFAULT_ACCENT)
    on = _on_color(acc)
    label, _ = sanitize_text(strip_markup(str(text or "").strip())[0].upper(), _TEXT_FONTS["cta"])
    label = " ".join(label.split())
    if sub is not None:
        sub = " ".join(sanitize_text(strip_markup(str(sub))[0], _TEXT_FONTS["cta_sub"])[0].split())
    max_w = min(860 * geo.sx, geo.w - 2 * SIDE_MARGIN * geo.sx)
    size = 66 * k
    for _ in range(6):
        f = _font("sans_black", size)
        h = size * 2.05
        icon = h * 0.64
        w = f.getlength(label) + size * 1.1 + icon + size * 0.5
        if w <= max_w or size <= 30 * k:
            break
        size = math.floor(size * max_w / w * 0.99)
    f = _font("sans_black", size)
    cap = _cap_height(f)
    h = size * 2.05
    icon = h * 0.64
    text_w = f.getlength(label)
    w = text_w + size * 1.1 + icon + size * 0.5
    margin = int(size * 0.6)
    layer = _new(w + 2 * margin, h + 2 * margin)
    x0, y0 = margin, margin
    pill = _new(layer.width, layer.height)
    _rounded_box(pill, (x0, y0, x0 + w, y0 + h), radius=h / 2.0, color=acc)
    _alpha_paste(layer, _shadow_of(pill, blur=size * 0.3, alpha=0.45), 0, int(size * 0.14))
    layer.alpha_composite(pill)
    # inner highlight line for a pressed-button feel
    _rounded_box(layer, (x0 + h * 0.3, y0 + h * 0.1, x0 + w - h * 0.3, y0 + h * 0.1 + max(2, size * 0.05)),
                 radius=size * 0.03, color=(255, 255, 255, 70))
    d = ImageDraw.Draw(layer)
    tx = x0 + size * 0.75
    d.text((tx, y0 + h / 2.0 + cap / 2.0), label, font=f, fill=on, anchor="ls")
    # round icon with an arrow
    icx = x0 + w - size * 0.32 - icon / 2.0
    icy = y0 + h / 2.0
    _rounded_box(layer, (icx - icon / 2, icy - icon / 2, icx + icon / 2, icy + icon / 2), radius=icon / 2, color=on)
    a = icon * 0.24
    shaft = icon * 0.09
    arrow = [(icx - a, icy - shaft), (icx + a * 0.1, icy - shaft), (icx + a * 0.1, icy - a * 0.75), (icx + a * 1.05, icy),
             (icx + a * 0.1, icy + a * 0.75), (icx + a * 0.1, icy + shaft), (icx - a, icy + shaft)]
    _polygon(layer, arrow, acc)
    img = Image.new("RGBA", (geo.w, geo.h), (0, 0, 0, 0))
    cy = layout_defaults((geo.w, geo.h))["cta_alone"] if center_y is None else float(center_y)
    _alpha_paste(img, layer, int(round(geo.cx - (x0 + w / 2.0))), int(round(cy - (y0 + h / 2.0))))
    if sub and str(sub).strip():
        s_size = 42 * k
        s_text = str(sub).strip()
        for _ in range(4):
            sf = _font("text_bold", s_size)
            if sf.getlength(s_text) <= max_w or s_size <= 24 * k:
                break
            s_size = math.floor(s_size * max_w / sf.getlength(s_text) * 0.99)
        sf = _font("text_bold", s_size)
        s_cap = _cap_height(sf)
        stroke = max(1, int(round(s_size * 0.06)))
        sw = sf.getlength(s_text)
        pad = int(s_size * 0.6)
        sl = _new(sw + 2 * pad, s_size * 1.6 + 2 * pad)
        ImageDraw.Draw(sl).text((pad, pad + s_size * 0.3 + s_cap), s_text, font=sf, fill=WHITE, anchor="ls",
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        out = _new(sl.width, sl.height)
        _alpha_paste(out, _shadow_of(sl, blur=s_size * 0.25, alpha=0.75), 0, int(s_size * 0.06))
        out.alpha_composite(sl)
        sy = cy + h / 2.0 + s_size * 0.55 + s_cap / 2.0
        _alpha_paste(img, out, int(round(geo.cx - (pad + sw / 2.0))), int(round(sy - (pad + s_size * 0.3 + s_cap / 2.0))))
    return img


def render_disclaimer(text: str, canvas=(1080, 1920), center_y: float | None = None) -> Image.Image:
    """Small dark pill with the result disclaimer (up to 2 lines).

    Default center y: layout_defaults(canvas)["disclaimer"] (1548 on 1080x1920)."""
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    k = geo.k
    msg = " ".join(sanitize_text(strip_markup(str(text or ""))[0], _TEXT_FONTS["disclaimer"])[0].split())
    if center_y is None:
        center_y = layout_defaults((geo.w, geo.h))["disclaimer"]
    img = Image.new("RGBA", (geo.w, geo.h), (0, 0, 0, 0))
    if not msg:
        return img
    max_w = geo.w - 2 * SIDE_MARGIN * geo.sx
    size = 31 * k
    padx, pady = size * 0.7, size * 0.36
    words = msg.split()
    for _ in range(6):
        f = _font("text_semibold", size)
        one_w = f.getlength(msg)
        if one_w + 2 * padx <= max_w:
            lines = [words]
        else:
            lines = _balanced_split(words, lambda ws: f.getlength(" ".join(ws)))
        widest = max(f.getlength(" ".join(ln)) for ln in lines)
        if widest + 2 * padx <= max_w or size <= 20 * k:
            break
        size = math.floor(size * (max_w - 2 * padx) / widest * 0.99)
        padx, pady = size * 0.7, size * 0.36
    f = _font("text_semibold", size)
    asc, desc = f.getmetrics()
    pitch = size * 1.25
    widths = [f.getlength(" ".join(ln)) for ln in lines]
    box_w = max(widths) + 2 * padx
    box_h = asc + desc + pitch * (len(lines) - 1) + 2 * pady
    layer = _new(box_w + 4, box_h + 4)
    _rounded_box(layer, (2, 2, 2 + box_w, 2 + box_h), radius=min(size * 0.6, box_h / 2), color=(0, 0, 0, 128))
    d = ImageDraw.Draw(layer)
    for i, (ln, wl) in enumerate(zip(lines, widths)):
        d.text((2 + (box_w - wl) / 2.0, 2 + pady + asc + pitch * i), " ".join(ln), font=f,
               fill=(255, 255, 255, 240), anchor="ls")
    cy = float(center_y)
    cy = min(cy, geo.h - box_h / 2.0 - 8 * geo.sy)
    _alpha_paste(img, layer, int(round(geo.cx - (2 + box_w / 2.0))), int(round(cy - (2 + box_h / 2.0))))
    return img


# ============================================================== samples

def cover_image(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fill `size` and center-crop (like CSS object-fit: cover)."""
    w, h = size
    s = max(w / img.width, h / img.height)
    nw, nh = max(1, int(round(img.width * s))), max(1, int(round(img.height * s)))
    r = img.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return r.crop((x, y, x + w, y + h))


def sample_caption_words(text: str, style_id: str) -> tuple[list[str], int]:
    """Words shown in a caption sample and which one is active."""
    spec = _caption_spec(style_id)
    words = display_words(str(text).split(), style_id)
    if not words:
        words = display_words('Take a look at this'.split(), style_id)
    limit = _Geo(REF_W, REF_H).caption_max_w
    chosen: list[str] = []
    for w in words:
        if len(chosen) >= spec.max_words:
            break
        trial = chosen + [w]
        if chosen and spec.lines == 1 and measure_caption_width(trial, style_id, already_display=True) > limit:
            break
        if chosen and spec.lines >= 2 and measure_caption_width(trial, style_id, already_display=True) > limit * spec.lines * 0.92:
            break
        chosen.append(w)
    active = -1 if not spec.animated else (0 if spec.one_word else min(1, len(chosen) - 1))
    return chosen, active


def render_caption_image(words: list[str], style_id: str, accent: str, canvas=(1080, 1920),
                         active: int = 0, center_y: float | None = None, font_scale: float = 1.0) -> Image.Image:
    """One steady caption state (display words already cased) on a transparent canvas."""
    spec = _caption_spec(style_id)
    geo = _Geo(int(canvas[0]), int(canvas[1]))
    cy = layout_defaults((geo.w, geo.h))["caption"] if center_y is None else center_y
    variant = {"angle": -2.6} if spec.draw == "tape" else {}
    block = _caption_block(spec, geo, tuple(words), active, accent or DEFAULT_ACCENT, font_scale, variant, {})
    return _place((geo.w, geo.h), block, geo.cx, cy, {}, geo)


def sample_canvas(background_size) -> tuple[int, int]:
    """Full-size canvas a style sample is composed on, for a frame of this size."""
    bw, bh = background_size
    if bw < bh:
        return (REF_W, REF_H)
    if bw > bh:
        return (REF_H, REF_W)
    return (REF_W, REF_W)


def render_sample_frame(background: Image.Image, kind: str, style_id: str, accent: str,
                        text: str, center_y: float | None = None) -> Image.Image:
    """Style preview over a real frame. Returns an RGB image with the background's size.

    `center_y` (pixels on sample_canvas(background.size)) moves the text, e.g. off the face;
    default: layout_defaults of that canvas."""
    bw, bh = background.size
    canvas = sample_canvas((bw, bh))
    base = cover_image(background.convert("RGB"), canvas).convert("RGBA")
    lay = layout_defaults(canvas)
    if kind == "caption":
        words, active = sample_caption_words(text, style_id)
        over = render_caption_image(words, style_id, accent, canvas, active=active,
                                    center_y=lay["caption"] if center_y is None else center_y)
    elif kind == "hook":
        over = render_hook(text, style_id, accent, canvas, center_y=lay["hook"] if center_y is None else center_y)
    else:
        raise ValueError("kind must be 'caption' or 'hook'")
    base.alpha_composite(over)
    out = base.convert("RGB")
    if out.size != (bw, bh):
        out = out.resize((bw, bh), Image.LANCZOS)
    return out


# ============================================================== CLI

def _catalog_check() -> list[str]:
    cat = load_style_catalog()
    problems = []
    for kind, ids in (("caption_styles", set(CAPTION_STYLES)), ("hook_styles", set(HOOK_STYLES))):
        cat_ids = {s.get("id") for s in cat.get(kind, [])}
        if cat_ids != ids:
            problems.append(f'{kind}: catalog {sorted(cat_ids)} versus code {sorted(ids)}')
    return problems


def _save_with_bg(img: Image.Image, out: str, bg: str | None) -> None:
    if bg:
        with Image.open(bg) as b:
            base = cover_image(b.convert("RGB"), img.size).convert("RGBA")
        base.alpha_composite(img)
        img = base.convert("RGB")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f'ok: {out}')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Render captions, hooks, calls to action, and disclaimers as transparent PNGs.')
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help='list available styles')

    def common_args(p):
        p.add_argument("--accent", default=DEFAULT_ACCENT, help='hex accent color (default #FFC400)', dest='accent')
        p.add_argument("--width", type=int, default=REF_W, dest='width')
        p.add_argument("--height", type=int, default=REF_H, dest='height')
        p.add_argument("--bg", help='optional background image for inspection', dest='bg')
        p.add_argument("-o", "--output", required=True, help='output PNG', dest='output')

    p = sub.add_parser("hook", help="hook (one or two lines)")
    p.add_argument('text')
    p.add_argument("--style", required=True, dest='style')
    p.add_argument("--center-y", type=float, help='vertical center in pixels (default: 430 on a vertical canvas)', dest='center_y')
    common_args(p)
    p = sub.add_parser("caption", help="one caption state")
    p.add_argument('text')
    p.add_argument("--style", required=True, dest='style')
    p.add_argument("--active", type=int, default=1, dest='active')
    common_args(p)
    p = sub.add_parser("cta", help='call-to-action button')
    p.add_argument('text')
    p.add_argument("--sub", dest='sub')
    p.add_argument("--center-y", type=float, help='vertical center in pixels (default: 1180 on a vertical canvas)', dest='center_y')
    common_args(p)
    p = sub.add_parser("disclaimer", help='results disclaimer')
    p.add_argument('text')
    p.add_argument("--center-y", type=float, help='vertical center in pixels (default: 1548 on a vertical canvas)', dest='center_y')
    common_args(p)
    p = sub.add_parser("track", help="caption track: PNGs and captions.ffconcat")
    p.add_argument('captions', help="captions.json")
    p.add_argument("--style", required=True, dest='style')
    p.add_argument("--accent", default=DEFAULT_ACCENT, dest='accent')
    p.add_argument("--fps", type=int, default=30, dest='fps')
    p.add_argument("--width", type=int, default=REF_W, dest='width')
    p.add_argument("--height", type=int, default=REF_H, dest='height')
    p.add_argument("--duration", type=float, help='total video duration (default: final caption ending)', dest='duration')
    p.add_argument("--font-scale", type=float, default=1.0, dest='font_scale')
    p.add_argument("-o", "--out-dir", required=True, dest='out_dir')
    args = ap.parse_args(argv)

    try:
        if args.cmd == "list":
            print("captions: " + ", ".join(CAPTION_STYLES))
            print("ganchos:  " + ", ".join(HOOK_STYLES))
            for msg in _catalog_check():
                print("WARNING: " + msg)
            return 0
        canvas = (args.width, args.height)
        issues: list[str] = []
        if args.cmd == "hook":
            fit = hook_fit(args.text, args.style, canvas)
            issues = text_issues("hook", args.text, args.style)
            cy = layout_defaults(canvas)["hook"] if args.center_y is None else args.center_y
            _save_with_bg(render_hook(args.text, args.style, args.accent, canvas, cy), args.output, args.bg)
            print(f"lines: {' / '.join(fit['lines'])} · font size {fit['size']}px")
            if fit["too_long"]:
                print(f"notice: the hook is long and required small lettering ({fit['size']}px, ideal from {fit['min_size']}px). Shorten the text.")
        elif args.cmd == "caption":
            spec = _caption_spec(args.style)
            words = display_words(args.text.split(), args.style)
            active = -1 if not spec.animated else max(-1, min(args.active, len(words) - 1))
            _save_with_bg(render_caption_image(words, args.style, args.accent, canvas, active=active), args.output, args.bg)
        elif args.cmd == "cta":
            issues = text_issues("cta", args.text) + text_issues("cta_sub", args.sub or "")
            _save_with_bg(render_cta(args.text, args.sub, args.accent, canvas, args.center_y), args.output, args.bg)
        elif args.cmd == "disclaimer":
            issues = text_issues("disclaimer", args.text)
            _save_with_bg(render_disclaimer(args.text, canvas, args.center_y), args.output, args.bg)
        elif args.cmd == "track":
            src = Path(args.captions)
            if not src.exists():
                print(f'error: caption file not found: {src}', file=sys.stderr)
                return 2
            data = json.loads(src.read_text(encoding="utf-8"))
            t0 = time.time()
            states = render_caption_track(data, args.style, args.accent, args.out_dir, canvas, args.fps,
                                          font_scale=args.font_scale)
            total = args.duration if args.duration else (states[-1]["end"] if states else 0.0)
            out_dir = Path(args.out_dir).resolve()
            ff = write_ffconcat(states, str(out_dir / "captions.ffconcat"), total, str(out_dir / "blank.png"), fps=args.fps)
            uniq = len({s["file"] for s in states})
            print(f'ok: {len(states)} estados, {uniq} PNGs, {time.time() - t0:.1f}s -> {ff}')
        for msg in issues:
            print(f'notice: {msg}')
    except (ValueError, FileNotFoundError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
