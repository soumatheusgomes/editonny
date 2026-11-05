'Compose edit_data.json into a frame-accurate final video.\n\nPipeline: apply per-segment camera movement to cut.mp4, prerender supporting images and videos, assemble text/visual layers, mix voice and synchronized effects with music, then export H.264/AAC and a verification report. Camera movement uses subpixel sampling to avoid slow-zoom jitter. Images use eased Ken Burns movement; video inserts loop from their start. Split layouts reserve a speaker band, fullscreen inserts cover the frame, and picture-in-picture inserts use a rounded card.\n\nText layers are transparent PNG states with frame-based enable windows. Priority is call to action, hook, captions, then disclaimer. Automatic positioning avoids detected faces and concurrent text; explicit positions remain explicit. Report repositioning, hidden layers, and remaining face collisions in verify/final_report.json. Refuse captions whose timeline signature or cut boundaries do not match the current cut. Later overlapping inserts appear above earlier ones.\n\nPlace effects by their measured anchor so the strongest sound lands on the event frame. Validate event time, volume, and anchor, warn when a sound starts before zero or is substantially truncated at the end, and preserve explicit file timing. Built-in names resolve through assets/sfx/sfx.json; personal library effects retain provenance.\n\nMusic can use an explicit file or a personal-library mood. Explicit files win. Reuse an intact recorded selection; otherwise choose a favorite or best-fitting track and copy it without overwriting unrelated assets. Match music to the voice, apply ducking and fades, then normalize final audio in two linear passes to the requested loudness and peak limits.\n\nOptional denoising rebuilds voice from source intervals with source-specific profiles, preserving cut sample positions and the original cut file. Optional person cutout uses cached mattes and face/body-aware composition; do not treat a failed cutout as a valid result.\n\nMaster encoding preserves source color interpretation and precision through the shared media policy. Effects require a validated 8-bit SDR 4:2:0 route; unsupported preservation blocks. Audio follows the confirmed policy. --preview uses a 540 px short side and CRF 26. Pin output duration to the video, write edit/verify/final_report.json, and update state.json. Missing optional sections remain disabled.\n\nUsage: uv run python helpers/compose_final.py edit/edit_data.json -o final.mp4\nUse --preview for a draft and --keep-temp only when inspecting intermediate artifacts.'

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from fractions import Fraction

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ffprobe_json,
    file_signature,
    fmt_time,
    hex_to_rgb,
    load_json,
    load_style_catalog,
    now_iso,
    probe_video,
    require_binary,
    save_json,
)
import captions as captions_mod  # noqa: E402
import denoise as denoise_mod  # noqa: E402
import face_layout  # noqa: E402
import make_sfx  # noqa: E402
import text_render as tr  # noqa: E402

RENDER_VERSION = "c2"          # bump to invalidate cached insert clips / overlay PNGs
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
HEIC_EXT = {".heic", ".heif", ".hif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".gif", ".mts"}
INSERT_TYPES = ("split_top", "split_bottom", "full", "pip")
SFX_FILE_EXT = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")
SFX_EVENT_MAX_VOLUME = 2.0
SFX_EVENT_MIN_KEPT = 0.9       # an sfx.events sound with less of its energy before the video end gets a warning
SFX_TRIM_FADE = 0.08           # s: fade at the end of a sound cut to its "length" (animation cues)
SR = 48000
MUSIC_VOLUME_DB = -15.0        # music.volume_db default: about 19 LU under continuous speech once ducked
KB_DRIFT = 0.06                # Ken Burns scale drift (at most 6%)
PREVIEW_SHORT_SIDE = 540

DEFAULT_DISCLAIMER = 'Individual results depend on execution. No earnings are guaranteed.'
DEFAULT_CTA = "Tap Learn More"


class ComposeError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(msg, flush=True)


def even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def ease_in_out(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 0.5 - 0.5 * math.cos(math.pi * p)


def short_hash(obj) -> str:
    return hashlib.sha1(repr(obj).encode("utf-8")).hexdigest()[:16]


def ffmpeg_cmd(args: list, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *[str(a) for a in args]],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise ComposeError(f'FFmpeg failed to {what}:\n{tail}')
    return proc


_FF_MAJOR: int | None = None


def ffmpeg_major() -> int:
    global _FF_MAJOR
    if _FF_MAJOR is None:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
        m = re.search(r"ffmpeg version n?(\d+)", out)
        _FF_MAJOR = int(m.group(1)) if m else 7
    return _FF_MAJOR


def graph_args(script: Path) -> list[str]:
    return ["-/filter_complex", str(script)] if ffmpeg_major() >= 7 else ["-filter_complex_script", str(script)]


# ============================================================== plan

@dataclass
class Insert:
    idx: int
    type: str
    src: Path
    kind: str                  # image | video
    f0: int
    f1: int
    ken_burns: bool
    split: float
    sfx: str | None
    content: tuple[int, int, int, int] = (0, 0, 0, 0)      # x, y, w, h of the picture
    speaker: tuple[int, int, int, int] | None = None       # x, y, w, h of the speaker band
    speaker_src_y: int = 0
    speaker_pieces: list[tuple[int, int, int]] = field(default_factory=list)   # (f0, f1, source row) per camera segment
    seam_y: int | None = None
    card: tuple[int, int, int, int] | None = None          # pip outer card
    clip: Path | None = None
    to_bg: bool = False        # Green screen: a split_top / split_bottom insert used as the background, without a panel.
    item: dict | None = None   # The raw edit_data item supplies darken, blur, fit, and corner settings.


@dataclass
class Plan:
    edit_dir: Path
    edit_data_path: Path
    data: dict
    base: Path
    out_path: Path
    preview: bool
    full_w: int
    full_h: int
    W: int
    H: int
    fps: int
    base_fps: float
    base_w: int
    base_h: int
    nf: int
    has_audio: bool
    segments: list[tuple[int, int]]
    accent: str
    renders: Path
    warnings: list[str] = field(default_factory=list)
    layers: list[dict] = field(default_factory=list)
    timeline: dict | None = None
    faces: dict | None = None                                  # face_layout.json
    cam_segs: list[dict] = field(default_factory=list)         # per segment: f0, f1, zoom, zoom_max, focus_y, face
    zones: list[tuple] = field(default_factory=list)           # (f0, f1, face zones, face cores) in render px
    obstacles: list[tuple] = field(default_factory=list)       # (f0, f1, (x0, y0, x1, y1)): pip cards texts avoid
    adjustments: list[dict] = field(default_factory=list)
    face_checks: list[dict] = field(default_factory=list)
    music_choice: dict | None = None                           # music.mood resolved through the library
    tv: dict | None = None                                     # edit_type "greenScreen" (prepare_tela_verde)
    person_zones: list[tuple] = field(default_factory=list)    # (f0, f1, body rectangles) in render pixels, for green screen only.
    image_zones: list[tuple] = field(default_factory=list)     # (f0, f1, rect) main area of a whole background picture
    media_plan: dict = field(default_factory=dict)
    fps_str: str = ''

    @property
    def duration(self) -> float:
        return self.nf / self.fps

    @property
    def s(self) -> float:          # render scale vs the full output canvas
        return self.W / self.full_w

    def fr(self, t, default: int = 0) -> int:
        if t is None:
            return default
        return max(0, min(self.nf, int(round(float(t) * self.fps + 1e-6))))

    def rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.edit_dir))
        except ValueError:
            return str(p.resolve())


def _section(data: dict, key: str) -> dict:
    v = data.get(key)
    return v if isinstance(v, dict) else {}


def _enabled(sec: dict) -> bool:
    return bool(sec) and bool(sec.get("enabled", True))


def _resolve(edit_dir: Path, value: str, what: str) -> Path:
    if not value or not str(value).strip():
        raise ComposeError(f'{what}: empty path', 2)
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = edit_dir / p
    p = p.resolve()
    if not p.exists():
        raise ComposeError(f'{what}: file not found: {p}', 2)
    return p


def _base_facts(base: Path) -> dict:
    try:
        info = ffprobe_json(base)
    except Exception:
        raise ComposeError(f'could not read base video: {base}', 2)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise ComposeError(f'the base video has no image: {base}', 2)
    from fractions import Fraction
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    if fps <= 0:
        fps = 30.0
    dur = float(v.get("duration") or (info.get("format") or {}).get("duration") or 0.0)
    nb = int(v.get("nb_frames") or 0)
    if nb <= 0:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(base)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        try:
            nb = int(proc.stdout.strip().split(",")[0])
        except ValueError:
            nb = int(round(dur * fps))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(round(float(sd["rotation"])))) % 180
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if rot == 90:
        w, h = h, w
    return {"fps": fps, "duration": dur, "frames": nb, "width": w, "height": h,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def build_plan(edit_data_path: Path, out: str | None, preview: bool) -> Plan:
    if not edit_data_path.exists():
        raise ComposeError(f'edit_data.json not found: {edit_data_path}', 2)
    try:
        data = json.loads(edit_data_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ComposeError(f'invalid edit_data.json ({e})', 2)
    if not isinstance(data, dict):
        raise ComposeError('edit_data.json must be a JSON object', 2)
    edit_dir = edit_data_path.parent.resolve()
    from project_contract import managed_config, settings_hash
    from common import probe_video
    from media_policy import plan_media, COLOR_FIELDS, UNKNOWN
    contract = managed_config(edit_dir)
    if contract:
        if data.get('contractHash') != settings_hash(contract):
            raise ComposeError('Composition settings do not match the confirmed project snapshot', 2)
        workflow = contract['workflow']
        if workflow['visuals']['scenes'] == 'off' and (data.get('inserts') or _motion_items(data) or
                any(_enabled(_section(data, key)) for key in ('hook', 'cta', 'disclaimer'))):
            raise ComposeError('Additional visual scenes were not confirmed', 2)
        if workflow['visuals']['transitions'] == 'off' and data.get('transitions'):
            raise ComposeError('Visual transitions were not confirmed', 2)
        if workflow['visuals'].get('backgroundTreatment', 'none') == 'none' and data.get('edit_type') == 'greenScreen':
            raise ComposeError('Background replacement was not confirmed', 2)
        if not workflow['visuals']['cameraMotion'] and any(_section(data, 'camera').get(key) for key in ('zoom_cuts', 'push_in')):
            raise ComposeError('Camera motion was not confirmed', 2)
        if not workflow['audio']['music'] and _enabled(_section(data, 'music')):
            raise ComposeError('Music was not confirmed', 2)
        if not workflow['audio']['effects'] and _enabled(_section(data, 'sfx')):
            raise ComposeError('Sound effects were not confirmed', 2)
        if _section(data, 'audio').get('denoise', 'off') != workflow['audio']['denoise']:
            raise ComposeError('Noise removal conflicts with the confirmed audio settings', 2)
        if _section(data, 'captions').get('enabled', contract.get('captions', {}).get('enabled')) != contract.get('captions', {}).get('enabled'):
            raise ComposeError('Caption visibility conflicts with the confirmed settings', 2)
        data['captions'] = {**contract.get('captions', {}), **_section(data, 'captions')}
        if data['captions'].get('enabled'):
            from project_contract import validate_caption_language
            validate_caption_language(contract, load_json(edit_dir / 'transcripts/cut.json') or {})
        data['loudness'] = {'i': workflow['audio']['targetLufs'], 'tp': workflow['audio']['truePeakLimit'], 'lra': 11}
        data['_audio_policy'] = workflow['audio']
    base = _resolve(edit_dir, data.get("base") or "cut.mp4", "base")
    facts = _base_facts(base)
    source_facts = probe_video(base)
    # The cut already carries the confirmed color treatment; only the confirmed encoding still applies.
    media_plan = plan_media([source_facts], {'workflow': {'encoding': contract['workflow']['encoding']}} if contract else None,
                            None, 'composition')

    timeline_path = edit_dir / (data.get("timeline") or "cut_timeline.json")
    timeline = load_json(timeline_path) if timeline_path.exists() else None

    outcfg = _section(data, "output")
    full_w = int(outcfg.get("width") or (timeline or {}).get("width") or facts["width"])
    full_h = int(outcfg.get("height") or (timeline or {}).get("height") or facts["height"])
    fps_val = outcfg.get("fps") or (timeline or {}).get('fps_str') or source_facts['fps_str']
    fps_str = str(Fraction(str(fps_val)))
    fps = float(Fraction(fps_str))
    if contract:
        expected_video = contract['workflow']['video']
        if (full_w, full_h) != (expected_video['width'], expected_video['height']) or Fraction(fps_str) != Fraction(expected_video['frameRate']):
            raise ComposeError('Composition geometry or cadence conflicts with confirmed settings', 2)
        from workspace import selected_sources
        manifest = selected_sources(load_json(edit_dir / 'source_manifest.json'), contract)
        expected = plan_media([s['facts'] for s in manifest['sources']], contract, None, 'master')
        if any(expected.get(k) not in UNKNOWN and source_facts.get(k) != expected[k] for k in COLOR_FIELDS):
            raise ComposeError('The cut master color profile does not match the confirmed media plan', 2)
    if fps <= 0 or full_w < 16 or full_h < 16:
        raise ComposeError('invalid output in edit_data.json (width/height/fps)', 2)
    if preview:
        k = min(1.0, PREVIEW_SHORT_SIDE / min(full_w, full_h))
        W, H = even(full_w * k), even(full_h * k)
    else:
        W, H = even(full_w), even(full_h)

    if abs(facts["fps"] - fps) < 0.01:
        nf = facts["frames"]
    else:
        nf = int(round(facts["duration"] * fps))
    if nf <= 0:
        raise ComposeError('the base video has no frames', 2)

    accent = str(data.get("accent") or (load_style_catalog().get("defaults") or {}).get("accent") or "#FFC400")
    try:
        hex_to_rgb(accent)
    except ValueError:
        raise ComposeError(f'invalid accent color: {accent!r} (use #RRGGBB)', 2)

    if out:
        out_path = Path(out).expanduser()
        if not out_path.is_absolute():
            out_path = (Path.cwd() / out_path)
    else:
        out_path = edit_dir / ("final_preview.mp4" if preview else "final.mp4")
    out_path = out_path.resolve()
    from common import ensure_distinct_output
    protected = [base]
    if contract:
        from project_contract import read_json
        protected += [s['path'] for s in read_json(edit_dir / 'source_manifest.json')['sources']]
    try:
        ensure_distinct_output(out_path, protected)
    except ValueError as error:
        raise ComposeError(str(error), 2) from error
    if contract and out_path.exists():
        raise ComposeError('Candidate already exists; use a new filename to preserve prior renders', 2)

    plan = Plan(edit_dir=edit_dir, edit_data_path=edit_data_path.resolve(), data=data, base=base,
                out_path=out_path, preview=preview, full_w=full_w, full_h=full_h, W=W, H=H, fps=fps,
                base_fps=facts["fps"], base_w=facts["width"], base_h=facts["height"], nf=nf,
                has_audio=facts["has_audio"], segments=[], accent=accent,
                renders=edit_dir / "renders" / ("preview" if preview else "full"), timeline=timeline,
                media_plan=media_plan, fps_str=fps_str)

    # timeline segments on the output frame grid
    segs = []
    for s in (timeline or {}).get("segments", []) or []:
        try:
            segs.append(plan.fr(float(s["out_start"])))
        except (KeyError, TypeError, ValueError):
            continue
    starts = sorted({f for f in segs if 0 < f < nf})
    bounds = [0] + starts + [nf]
    plan.segments = [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]
    music = _section(data, "music")
    if _enabled(music) and music.get("file"):
        _resolve(edit_dir, music.get("file"), "music.file")
    elif _enabled(music) and music.get("mood") not in (None, ""):
        resolve_music_mood(plan, music)
    elif _enabled(music):
        raise ComposeError("music is enabled without 'file' or 'mood'; provide a local file or a library mood", 2)
    if timeline is None:
        plan.warnings.append('cut_timeline.json missing: treating the entire video as one interval')
    if timeline and abs(float(timeline.get("duration") or 0) - nf / fps) > 1.5 / fps:
        plan.warnings.append(f"cut_timeline.json reports {timeline.get('duration')}s, but the base video has {nf / fps:.3f}s")
    return plan


def resolve_music_mood(plan: Plan, music: dict) -> None:
    """music.mood without music.file: pick the track from the personal library (helpers/music_library.py).

    The chosen file goes to assets/ and into plan.data["music"]["file"] (in memory only), so the audio graph is
    the one of a plain music.file; plan.music_choice keeps the track id and why for the report."""
    import music_library

    mood, track = music.get("mood"), music.get("track")
    if not isinstance(mood, str) or not mood.strip():
        raise ComposeError('music.mood must name a mood, for example "sad"', 2)
    if track is not None and (not isinstance(track, str) or not track.strip()):
        raise ComposeError('music.track must be a library track ID, for example "sad-2"', 2)
    try:
        choice = music_library.resolve_for_edit(plan.edit_dir, mood.strip(), plan.duration,
                                          track.strip() if track else None)
    except music_library.MusicError as e:
        raise ComposeError(f'music.mood: {e}', 2)
    _resolve(plan.edit_dir, choice["file"], "music.file")
    plan.data["music"] = {**music, "file": choice["file"]}
    plan.music_choice = choice
    if choice.get("warning"):
        plan.warnings.append(choice["warning"])


def _plain_number(value) -> float | None:
    """A finite number (or numeric text); None for bools, text that is not a number, NaN and inf."""
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _as_json(value) -> str:
    """A value the way it is written in edit_data.json (null, true, "text"), for error messages."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
    if isinstance(value, str) and "," in value and _plain_number(value.replace(",", ".")) is not None:
        text += '; use a decimal point instead of a comma'
    return text


def plan_sfx_events(plan: Plan) -> list[dict]:
    """sfx.events: effects at a chosen time on the cut, not tied to a hook, insert, flash or CTA.

    Checked before anything renders. Returns events in the same shape build_tracks gives run_audio
    ({name, frame, lead_frames 0, why "effect"}) plus at, event (index), and volume and anchor_s when given.
    """
    sec = _section(plan.data, "sfx")
    if not _enabled(sec) or sec.get("events") is None:
        return []
    raw = sec.get("events")
    example = '{"at": 5.07, "name": "cash"}'
    if not isinstance(raw, list):
        raise ComposeError(f'sfx.events must be a list, for example [{example}])', 2)
    out: list[dict] = []
    for i, item in enumerate(raw):
        where = f'sfx.events[{i}]'
        if not isinstance(item, dict):
            raise ComposeError(f'{where} must be an object, for example {example})', 2)
        at = _plain_number(item.get("at"))
        if at is None or at < 0:
            if "at" not in item:
                raise ComposeError(f'{where} requires "at": the cut time in seconds when the sound plays, for example {example})', 2)
            raise ComposeError(f"{where}.at must be nonnegative seconds (received {_as_json(item.get('at'))})", 2)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ComposeError(f"""{where} requires "name": an effect ({', '.join(make_sfx.NAMES)}) or a local audio file""", 2)
        name = name.strip()
        ev: dict = {"name": name, "frame": 0, "lead_frames": 0, "why": "effect", "at": round(at, 4), "event": i}
        if item.get("volume") is not None:
            vol = _plain_number(item.get("volume"))
            if vol is None or vol < 0:
                raise ComposeError(f"{where}.volume must be between 0.0 and {SFX_EVENT_MAX_VOLUME:.1f} (got {_as_json(item.get('volume'))})", 2)
            if vol > SFX_EVENT_MAX_VOLUME:
                plan.warnings.append(f'{where}.volume {vol:g} exceeded the maximum and was limited to {SFX_EVENT_MAX_VOLUME:.1f}')
                vol = SFX_EVENT_MAX_VOLUME
            ev["volume"] = vol
        if item.get("anchor_s") is not None:
            anchor = _plain_number(item.get("anchor_s"))
            if anchor is None or anchor < 0:
                raise ComposeError(f"{where}.anchor_s must be nonnegative seconds locating the sound's peak (received {_as_json(item.get('anchor_s'))})", 2)
            ev["anchor_s"] = round(anchor, 4)
        if Path(name).suffix.lower() in SFX_FILE_EXT:
            _resolve(plan.edit_dir, name, f'{where}.name')
        if at * plan.fps >= plan.nf - 1e-6:
            plan.warnings.append(f"{where} ('{name}' at {fmt_time(at)}) exceeds the video ending at {fmt_time(plan.duration)}; the last frame starts at {fmt_time((plan.nf - 1) / plan.fps)}) and was ignored")
            continue
        # same rounding as the other events (Plan.fr); a time in the last half frame stays on the last frame
        ev["frame"] = min(plan.fr(at), plan.nf - 1)
        out.append(ev)
    return out


def plan_denoise(plan: Plan) -> str:
    """audio.denoise: "off" | "light" | "strong" (a missing section or "enabled": false is off). Checked before rendering."""
    raw = plan.data.get("audio")
    if raw is None:
        return "off"
    if not isinstance(raw, dict):
        raise ComposeError('audio must be an object, for example "audio": {"denoise": "light"}', 2)
    if raw.get("enabled", True) is False:
        return "off"
    try:
        return denoise_mod.normalize_mode(raw.get("denoise"))
    except ValueError as e:
        raise ComposeError(str(e), 2)


def run_denoise(plan: Plan, mode: str, keep_temp: bool) -> tuple[Path | None, dict]:
    """The cleaned voice for run_audio (None keeps cut.mp4's audio). A failure never stops the render."""
    if mode == "off":
        return None, {"mode": "off", "applied": "off"}
    if not plan.has_audio:
        msg = 'the base video has no audio; there is no noise to remove'
        plan.warnings.append(msg)
        return None, {"mode": mode, "applied": "off", "warnings": [msg]}
    work = plan.edit_dir / "renders" / "denoise"
    try:
        res = denoise_mod.build_voice(plan.edit_dir, plan.base, plan.timeline, plan.fps, round(plan.nf * SR / plan.fps), mode,
                                      work, keep_temp=keep_temp)
    except Exception as e:  # noqa: BLE001  (the cleaning is optional: the render goes on with the cut's audio)
        msg = f'noise removal failed ({e}). Preserving original audio'
        plan.warnings.append(msg)
        return None, {"mode": mode, "applied": "off", "warnings": [msg]}
    plan.warnings.extend(res.warnings)
    return res.path, res.report


def denoise_line(rep: dict) -> str | None:
    """One PT-BR log line about the background-noise removal (None when it is off)."""
    mode = rep.get("mode") or "off"
    if mode == "off":
        return None
    applied = rep.get("applied") or "off"
    if applied == "off":
        return f'background noise: cleanup disabled (requested {denoise_mod.LABEL[mode]}; see the warning)'
    if applied == "mixed":
        lighter = [e["source"] for e in rep.get("sources") or [] if e.get("applied") == "light"]
        what = f"{denoise_mod.LABEL[mode]}, light on {', '.join(lighter)}"
    elif applied != mode:
        what = f'{denoise_mod.LABEL[applied]} instead of {denoise_mod.LABEL[mode]}'
    else:
        what = denoise_mod.LABEL[applied]
    if rep.get("method") == "cut":
        what += ", in the cut audio"
    before, after = rep.get("noise_floor_before_db"), rep.get("noise_floor_after_db")
    if before is not None and after is not None:
        return f'background noise: {what} (background of {before:.0f} dB to {after:.0f} dB)'
    return f'background noise: {what}'


# ============================================================== camera

def _is_auto(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() == "auto")


def load_faces(plan: Plan) -> None:
    """Face per timeline segment (face_layout.json, detected on the first compose and cached)."""
    try:
        plan.faces = face_layout.build(plan.edit_dir, plan.base, timeline=plan.timeline)
    except face_layout.FaceLayoutError as e:
        plan.warnings.append(f'could not detect the face ({e}); using default text and zoom positions')
    except Exception as e:  # noqa: BLE001  (a detector failure must not stop the render)
        plan.warnings.append(f'face detection failed ({e.__class__.__name__}); using default text and zoom positions')


def camera_segments(plan: Plan, levels: list[float], zoom_cuts: bool, push_in: bool, amount: float) -> list[dict]:
    cam = _section(plan.data, "camera")
    manual = None
    if not _is_auto(cam.get("focus_y")):
        try:
            manual = min(0.9, max(0.1, float(cam.get("focus_y"))))
        except (TypeError, ValueError):
            raise ComposeError('camera.focus_y must be between 0.1 and 0.9, or "auto"', 2)
    face_segs = [f for f in ((plan.faces or {}).get("segments") or []) if isinstance(f, dict)]
    found = any(f.get("face") for f in face_segs)
    fallback = ((plan.faces or {}).get("recommended") or {}).get("camera", {}).get("focus_y") if found else None
    fallback = float(fallback) if fallback is not None else 0.40
    out = []
    for i, (a, b) in enumerate(plan.segments):
        z = levels[i % len(levels)] if zoom_cuts else 1.0
        t0, t1 = a / plan.fps, b / plan.fps
        best, best_ov = None, 0.0
        for f in face_segs:
            ov = min(t1, float(f.get("end", 0))) - max(t0, float(f.get("start", 0)))
            if ov > best_ov:
                best, best_ov = f, ov
        face = best if best and best.get("face") else None
        fy = manual if manual is not None else (float(face["focus_y"]) if face and face.get("focus_y") is not None else fallback)
        out.append({"f0": a, "f1": b, "zoom": z, "zoom_max": z * (1 + amount) if push_in else z,
                    "focus_y": min(0.9, max(0.1, fy)), "face": face, "manual_focus": manual is not None})
    return out


def camera_filter(plan: Plan) -> str | None:
    cam = _section(plan.data, "camera")
    zoom_cuts = bool(cam.get("zoom_cuts", False))
    push_in = bool(cam.get("push_in", False))
    levels = cam.get("zoom_levels") or [1.0, 1.14]
    try:
        levels = [min(1.6, max(1.0, float(z))) for z in levels] or [1.0]
    except (TypeError, ValueError):
        raise ComposeError('camera.zoom_levels must be a numeric list, for example [1.0, 1.14]', 2)
    amount = min(0.25, max(0.0, float(cam.get("push_in_amount", 0.05) or 0.0)))
    plan.cam_segs = camera_segments(plan, levels, zoom_cuts, push_in and amount > 0, amount)
    segs_info = []
    parts = []
    for cs in plan.cam_segs:
        a, b, z = cs["f0"], cs["f1"], cs["zoom"]
        n = b - a
        if push_in and amount > 0 and n > 1:
            expr = f'{z:.5f}*(1+{amount:.5f}*(0.5-0.5*cos(PI*clip((in-1-{a})/{n - 1},0,1))))'
        else:
            expr = f'{z:.5f}'
        parts.append((b, expr, cs["focus_y"]))
        segs_info.append({"start": round(a / plan.fps, 3), "end": round(b / plan.fps, 3), "zoom": z,
                          "push_in": amount if push_in else 0.0, "focus_y": round(cs["focus_y"], 4),
                          "face": bool(cs["face"])})
    manual = next((cs["focus_y"] for cs in plan.cam_segs if cs["manual_focus"]), None)
    plan.layers.append({"type": "camera", "zoom_cuts": zoom_cuts, "push_in": push_in,
                        "focus_y": manual if manual is not None else "auto", "segments": segs_info})
    if not push_in and all(abs(info["zoom"] - 1.0) < 1e-6 for info in segs_info):
        return None

    def piecewise(fn) -> str:
        expr = fn(parts[-1])
        for part in reversed(parts[:-1]):
            expr = f'if(lt(in-1,{part[0]}),{fn(part)},{expr})'
        return f'({expr})'

    Z = piecewise(lambda p: p[1])
    # each segment's crop keeps its own eye line still (focus_y per segment)
    T = piecewise(lambda p: f'{p[2]:.5f}*(1-1/({p[1]}))')
    left = f'W*0.5*(1-1/{Z})'
    top = f'H*{T}'
    right = f'W*0.5*(1-1/{Z})+W/{Z}'
    bottom = f'H*{T}+H/{Z}'
    return (f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}':x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}':interpolation=cubic:eval=frame")


def segment_for(plan: Plan, f0: int, f1: int) -> dict | None:
    """Camera segment covering most of [f0, f1)."""
    best, best_ov = None, 0
    for cs in plan.cam_segs:
        ov = min(f1, cs["f1"]) - max(f0, cs["f0"])
        if ov > best_ov:
            best, best_ov = cs, ov
    return best


# ============================================================== inserts

def plan_inserts(plan: Plan, person_cutout: bool = False) -> list[Insert]:
    """greenScreen: split_top / split_bottom items become background items (to_bg, no panel, no seam)."""
    raw = plan.data.get("inserts") or []
    if not isinstance(raw, list):
        raise ComposeError('inserts must be a list', 2)
    sfx_on = _enabled(_section(plan.data, "sfx"))
    W, H, s = plan.W, plan.H, plan.s
    lay = tr.layout_defaults((W, H))
    safe_top, safe_bottom = lay["top"], lay["bottom"]
    out: list[Insert] = []
    seam_reach = (0.0, 0.0)          # (above, below) px the captions reach from a split seam
    cap = _section(plan.data, "captions")
    if _enabled(cap) and not person_cutout and any(isinstance(it, dict) and it.get("type") in ("split_top", "split_bottom")
                                                for it in raw):
        style = cap.get("style")
        if not style:   # the same fallback build_tracks uses: the style captions.json was grouped for
            try:
                cues = load_json(plan.edit_dir / str(cap.get("file") or "captions.json"))
            except (ValueError, OSError):
                cues = None
            style = (cues.get("style") if isinstance(cues, dict) else None)\
                or (load_style_catalog().get("defaults") or {}).get("caption_style") or "impact"
        try:
            seam_reach = tr.caption_extent([], style, (W, H), float(cap.get("font_scale", 1.0) or 1.0), plan.fps)
        except (ValueError, TypeError):
            seam_reach = (0.0, 0.0)   # unknown style: build_tracks reports it
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ComposeError(f'inserts[{i}] must be an object', 2)
        typ = item.get("type")
        if typ not in INSERT_TYPES:
            raise ComposeError(f"inserts[{i}].type is invalid: {typ!r}. Use {', '.join(INSERT_TYPES)}", 2)
        src = _resolve(plan.edit_dir, item.get("src"), f'inserts[{i}].src')
        ext = src.suffix.lower()
        if ext in HEIC_EXT:
            src, ext = heic_to_jpg(plan, src, i), ".jpg"
        kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
        if kind is None:
            try:
                Image.open(src).close()
                kind = "image"
            except Exception:
                kind = "video"
        try:
            t0, t1 = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            raise ComposeError(f'inserts[{i}] requires start and end in seconds', 2)
        f0, f1 = plan.fr(t0), plan.fr(t1)
        if f0 >= plan.nf:
            plan.warnings.append(f'insert {i + 1} starts after the video and was skipped')
            continue
        if t1 * plan.fps > plan.nf + 0.5:
            plan.warnings.append(f'insert {i + 1} exceeded the video duration and was trimmed at {fmt_time(plan.duration)}')
        if f1 - f0 < 2:
            plan.warnings.append(f'insert {i + 1} lasts fewer than two frames and was ignored')
            continue
        kb_default = kind == "image"
        split = item.get("split", 0.46)
        try:
            split = min(0.75, max(0.25, float(split if split is not None else 0.46)))
        except (TypeError, ValueError):
            raise ComposeError(f'inserts[{i}].split must be between 0.25 and 0.75', 2)
        sfx_name = item.get("sfx", "whoosh")
        if sfx_name in (None, False, "", "none"):
            sfx_name = None
        ins = Insert(idx=i, type=typ, src=src, kind=kind, f0=f0, f1=f1,
                     ken_burns=bool(item.get("ken_burns", kb_default)), split=split,
                     sfx=str(sfx_name) if (sfx_name and sfx_on) else None)
        if person_cutout:
            ins.item = item
        if person_cutout and typ in ("split_top", "split_bottom"):
            ins.to_bg = True            # its "split" does not apply: it is the background of [f0, f1)
            ins.content = (0, 0, W, H)
        elif typ in ("split_top", "split_bottom"):
            hb = even(split * H)
            hs = H - hb
            # eyes of the segment under this window, where the camera left them (focus_y is the eye line)
            seg = segment_for(plan, f0, min(f1, plan.nf))
            face = (seg["focus_y"] if seg else 0.40) * H
            if typ == "split_top":
                ins.content = (0, 0, W, hb)
                ins.speaker = (0, hb, W, hs)
                ins.seam_y = hb
            else:
                ins.content = (0, hs, W, hb)
                ins.speaker = (0, 0, W, hs)
                ins.seam_y = hs
            ins.speaker_src_y = face_layout.speaker_src_y(typ, hb / H, face, W, H, safe_top, safe_bottom)
            # the face can sit at another height in each take the window covers: re-frame per segment
            pieces: list[tuple[int, int, int]] = []
            for cs in plan.cam_segs:
                a, b = max(f0, cs["f0"]), min(f1, cs["f1"])
                if b <= a:
                    continue
                core = None
                if cs.get("face") and cs["face"].get("core"):
                    rects = [face_layout.zoom_rect(cs["face"]["core"], z, cs["focus_y"]) for z in {cs["zoom"], cs["zoom_max"]}]
                    core = (min(r["y0"] for r in rects) * H, max(r["y1"] for r in rects) * H)
                reach = seam_reach[0] if typ == "split_bottom" else seam_reach[1]
                y = face_layout.speaker_src_y(typ, hb / H, cs["focus_y"] * H, W, H, safe_top, safe_bottom,
                                              core=core, clear=reach + GAP_FACE * H if reach else 0.0)
                if pieces and pieces[-1][2] == y and pieces[-1][1] == a:
                    pieces[-1] = (pieces[-1][0], b, y)
                else:
                    pieces.append((a, b, y))
            ins.speaker_pieces = pieces or [(f0, f1, ins.speaker_src_y)]
            ins.speaker_src_y = max(ins.speaker_pieces, key=lambda p: p[1] - p[0])[2]
        elif typ == "full":
            ins.content = (0, 0, W, H)
        else:  # pip
            aspect = 1.0
            if kind == "image":
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im)
                    aspect = im.width / max(1, im.height)
            else:
                try:
                    info = ffprobe_json(src)
                    v = next(st for st in info.get("streams", []) if st.get("codec_type") == "video")
                    aspect = int(v["width"]) / max(1, int(v["height"]))
                except Exception:
                    aspect = 1.0
            aspect = min(1.25, max(0.8, aspect))
            border = max(2, int(round(10 * s)))
            cw = even(380 * s)
            ch = even(cw / aspect)
            ow, oh = cw + 2 * border, ch + 2 * border
            corner = str(item.get("corner") or "top_right")
            if corner not in ("top_right", "top_left"):
                raise ComposeError(f'inserts[{i}].corner is invalid: {corner!r}. Use top_right or top_left', 2)
            margin_x = int(round(48 * s))
            ox = W - margin_x - ow if corner == "top_right" else margin_x
            oy = int(round(safe_top + 26 * s))
            ins.card = (ox, oy, ow, oh)
            ins.content = (ox + border, oy + border, cw, ch)
        out.append(ins)
    for k, a in enumerate(out):
        for b in out[k + 1:]:
            if b.f0 < a.f1 and a.f0 < b.f1 and a.to_bg == b.to_bg:
                plan.warnings.append(f'inserts {a.idx + 1} and {b.idx + 1} overlap ({fmt_time(max(a.f0, b.f0) / plan.fps)}): insert {b.idx + 1} appears later in the list and is drawn on top')
    return out   # list order = drawing order (a later insert is drawn over an earlier one)


def heic_to_jpg(plan: Plan, src: Path, i: int, where: str | None = None) -> Path:
    """iPhone photos (HEIC/HEIF) -> JPG once, with macOS `sips` (cached by the file signature)."""
    where = where or f'inserts[{i}].src'
    sips = shutil.which("sips") if sys.platform == "darwin" else None
    if not sips:
        raise ComposeError(f'{where} is an iPhone HEIC photo ({src.name}), which cannot be opened here. Export as JPG and use the .jpg file', 2)
    out_dir = plan.edit_dir / "renders" / "heic"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{src.stem}_{short_hash((str(src), file_signature(src)))[:10]}.jpg'
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_name(".tmp-" + out.name)
    proc = subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "93", str(src), "--out", str(tmp)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0 or not tmp.exists():
        raise ComposeError(f'could not convert HEIC photo {where} ({src.name}) to JPG. Export the photo as JPG and use the .jpg file', 2)
    os.replace(tmp, out)
    return out


# ============================================================== green-screen composition

EDIT_TYPES = ("clean", "split_top", "split_bottom", "greenScreen")


def resolve_average(plan: Plan, value, where: str) -> tuple[Path, str]:
    """An image or video file of the edit -> (path, "image" | "video"), HEIC converted like the inserts."""
    if not isinstance(value, str):
        raise ComposeError(f'{where} must be a file path, for example "assets/photo.jpg"', 2)
    src = _resolve(plan.edit_dir, value, where)
    ext = src.suffix.lower()
    if ext in HEIC_EXT:
        src, ext = heic_to_jpg(plan, src, 0, where), ".jpg"
    kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
    if kind is None:
        try:
            Image.open(src).close()
            kind = "image"
        except Exception:
            kind = "video"
    return src, kind


def plan_screen_green(plan: Plan):
    """edit_type "greenScreen": check the section before anything renders (exit 2). None for the other types."""
    et = plan.data.get("edit_type")
    if et is None or et in EDIT_TYPES[:3]:
        if plan.data.get("greenScreen") is not None:
            plan.warnings.append('greenScreen settings are unused; enable them with "edit_type": "greenScreen"')
        return None
    if et != "greenScreen":
        raise ComposeError(f"edit_type {_as_json(et)} does not exist. Use {', '.join(EDIT_TYPES)}", 2)
    import matte as MT
    import person_cutout as TV

    try:
        cfg = TV.parse_config(plan.data.get("greenScreen"), plan.fps, plan.nf,
                              lambda value, where: resolve_average(plan, value, where), plan.fr,
                              screen=(plan.full_w, plan.full_h))
        MT.default_engine_ready()
    except TV.CutoutError as e:
        raise ComposeError(str(e), e.code)
    except MT.MatteError as e:
        raise ComposeError(str(e), e.code)
    return cfg      # its warnings go to the report after the split inserts join it (insert_backgrounds)


def insert_backgrounds(plan: Plan, cfg, inserts: list[Insert]) -> None:
    """Green screen: the split_top / split_bottom inserts become background items (they win over greenScreen.background
    during their own frames); then the warnings of the section go to the report."""
    import person_cutout as TV

    moved = [i for i in inserts if i.to_bg]
    try:
        TV.add_insert_backgrounds(cfg, [{"insert": i.idx + 1, "type": i.type, "src": i.src, "kind": i.kind,
                                         "f0": i.f0, "f1": i.f1, "ken_burns": i.ken_burns, "item": i.item}
                                        for i in moved], plan.nf, plan.fps)
    except TV.CutoutError as e:
        raise ComposeError(str(e), e.code)
    for ins in inserts:
        bad = [k for k in ("darken", "blur", "fit") if ins.item and k in ins.item and not ins.to_bg]
        if bad:
            plan.warnings.append(f'inserts[{ins.idx}].{bad[0]} applies only to upper or lower media used as a green-screen background: {ins.type} was excluded')
    plan.warnings.extend(cfg.warnings)
    if moved:
        names = ", ".join(str(i.idx + 1) for i in moved)
        kinds = sorted({TV.SPLIT_LABEL[i.type] for i in moved})
        log(f"green screen: {('insert' if len(moved) == 1 else 'inserts')} {names} ({' and '.join(kinds)}) {('moves' if len(moved) == 1 else 'move')} to the background behind the speaker")


def prepare_screen_green(plan: Plan, cfg) -> None:
    """Person segments, their mattes (computed now when not cached), placement, and the face and body of each
    segment where they land on the screen (cam_segs switch to screen coordinates without zoom)."""
    import matte as MT
    import person_cutout as TV

    t0 = time.time()
    segs = []
    for cs in plan.cam_segs:
        has_face = bool(cs.get("face"))
        mode = "person" if (has_face or cfg.in_face == "cutout" or plan.faces is None) else "original"
        push = cs["zoom_max"] / cs["zoom"] - 1.0 if cs["zoom_max"] > cs["zoom"] + 1e-9 else 0.0
        segs.append(TV.Segment(f0=cs["f0"], f1=cs["f1"], mode=mode, zoom=cs["zoom"], push=push,
                               focus_y=cs["focus_y"], face=cs.get("face") if has_face else None))
    people = [i for i, sg in enumerate(segs) if sg.mode == "person"]
    mset = None
    mattes: dict[int, object] = {}
    if people:
        try:
            mset = MT.ensure(plan.edit_dir, plan.base, [(segs[i].f0, segs[i].f1) for i in people], plan.fps,
                             (plan.full_w, plan.full_h), plan.timeline, progress=log,
                             base_fps=plan.base_fps, base_size=(plan.base_w, plan.base_h))
        except MT.MatteError as e:
            raise ComposeError(f'person cutout: {e}', e.code)
        plan.warnings.extend(mset.warnings)
        for i, job in zip(people, mset.jobs):
            mattes[i] = job
            segs[i].stats = job.meta.get("stats") or {}
            if cfg.mirror:           # the person segments are drawn flipped: place them as they will be seen
                segs[i].stats = TV.mirror_stats(segs[i].stats)
                segs[i].face = TV.mirror_face(segs[i].face)
    TV.plan_segments(cfg, segs)
    for sg in segs:
        if sg.note:
            plan.warnings.append(f'green screen, interval {fmt_time(sg.f0 / plan.fps)}-{fmt_time(sg.f1 / plan.fps)}: {sg.note}')
    filled = merge_windows([(sg.f0, sg.f1) for sg in segs if sg.fill])
    if filled:
        spans = ", ".join(f'{fmt_time(a / plan.fps)}-{fmt_time(b / plan.fps)}' for a, b in filled)
        plan.warnings.append(f'green screen: at {spans} the source crops both sides of the body; use the full canvas width at a lower position to hide those edges')
    inner = TV.inner_cut(cfg, segs, plan.W, plan.H)
    if inner:
        plan.warnings.append(inner['warning'])
    for cs, sg in zip(plan.cam_segs, segs):
        cs["tv_zoom"], cs["tv_zoom_max"], cs["tv_mode"] = cs["zoom"], cs["zoom_max"], sg.mode
        if sg.mode == "person":
            z = TV.screen_zones(cfg, sg, plan.W, plan.H)
            face = z["face"]
            cs["face"] = ({"zone": face["zone"], "core": face["core"], "face": {"eyes_y": face["eyes_y"]}}
                          if face else None)
            if face:
                cs["focus_y"] = min(0.9, max(0.1, face["eyes_y"]))
            cs["person"] = z["person"]
            cs["zoom"] = cs["zoom_max"] = 1.0      # the zoom is already in the person layer
    for b in cfg.backgrounds:        # the main area of a still picture shown whole (its empty margin may be covered)
        if b.fit == "contain" and b.kind == "image" and b.size:
            try:
                b.content = TV.content_box(TV.read_image(b.src))
            except Exception as e:  # noqa: BLE001
                raise ComposeError(f'could not open background image ({b.name}): {b.src} ({e})', 2)
        elif b.kind == "video" and b.src is not None:
            # empty frames at its start and end; shown whole: its main area over the clip and a plain border
            try:
                info = TV.scan_video(b.src, plan.fps, plan.renders / "telaverde")
            except Exception as e:  # noqa: BLE001
                raise ComposeError(f'could not read background video ({b.name}): {b.src} ({e})', 1)
            b.pass_frames, b.lead, b.tail = int(info["frames"]), int(info["lead"]), int(info["tail"])
            if b.fit == "contain" and b.size:
                b.content = tuple(float(v) for v in info["content"])
                b.quiet = bool(info["quiet"])
    TV.plan_stages(cfg, segs, plan.W, plan.H, caption_room(plan))
    plan.image_zones = TV.image_zones(cfg, segs, plan.W, plan.H)
    plates, plate_rows = room_plates(plan, mattes)
    plan.tv = {"cfg": cfg, "segments": segs, "mattes": mattes, "mset": mset, "inner_cut": inner,
               "plates": plates, "plate_rows": plate_rows,
               "matte_seconds": round(mset.seconds, 2) if mset else 0.0, "prepare_seconds": round(time.time() - t0, 2)}
    cam = next((layer for layer in plan.layers if layer["type"] == "camera"), None)
    if cam is not None:
        cam["applied_to"] = "greenScreen"


def room_plates(plan: Plan, mattes: dict) -> tuple[dict, list[dict]]:
    """Green screen: the room plate of each take (the ranges of one source video with the same framing), so the
    render can take out the room a matte keeps. -> ({segment index: plate}, report rows)."""
    import matte as MT
    import person_cutout as TV

    if not mattes or not plan.timeline or os.environ.get(TV.PLATE_ENV, "1") == "0":
        return {}, []
    groups: dict[str, list] = {}
    names: dict[str, str] = {}
    for i, job in mattes.items():
        tseg = MT._timeline_segment(plan.timeline, job.f0, job.f1, plan.fps)
        if not tseg or tseg.get("source") is None:
            continue
        cam = str(tseg.get("picture_source") or tseg.get("source"))   # the camera the picture came from
        g = json.dumps([cam, tseg.get("crop")], sort_keys=True, ensure_ascii=False)
        groups.setdefault(g, []).append((i, job))
        names[g] = cam
    if not groups:
        return {}, []
    try:
        found, notes = TV.room_plates(plan.base, _matte_pre(plan), plan.base_fps, plan.fps,
                                      (plan.full_w, plan.full_h),
                                      {g: [(j.f0, j.f1, j) for _, j in parts] for g, parts in groups.items()},
                                      plan.edit_dir / MT.CACHE_DIR, progress=log)
    except (OSError, ValueError, MT.MatteError) as e:
        plan.warnings.append(f"green screen: could not learn the background ({e}); retained the model's original segmentation")
        return {}, []
    plates, rows = {}, []
    for g, parts in groups.items():
        plate = found.get(g)
        for i, _ in parts:
            if plate is not None:
                plates[i] = plate
        rows.append({"shot": names[g], "intervals": len(parts), "used": plate is not None,
                     "known": plate.known if plate is not None else None,
                     "reason": None if plate is not None else (notes.get(g) or None)})
    return plates, rows


def compute_person_zones(plan: Plan, inserts: list[Insert]) -> None:
    """plan.person_zones: where the cut-out body is on the screen, per frame interval (green screen only)."""
    if not plan.tv:
        return
    W, H = plan.W, plan.H
    pieces = []
    for cs in plan.cam_segs:
        r = cs.get("person")
        rects = [(r["x0"] * W, r["y0"] * H, r["x1"] * W, r["y1"] * H)] if r else []
        pieces.append((cs["f0"], cs["f1"], rects))
    cuts = sorted({0, plan.nf} | {p[0] for p in pieces} | {p[1] for p in pieces}
                  | {min(plan.nf, i.f0) for i in inserts} | {min(plan.nf, i.f1) for i in inserts})
    out: list[tuple] = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        base = next((p for p in pieces if p[0] <= a < p[1]), None)
        rects = base[2] if base else []
        top = None
        for ins in inserts:
            if ins.type != "pip" and ins.f0 <= a and b <= ins.f1:
                top = ins
        if top is not None and top.type == "full":
            rects = []
        elif top is not None and top.speaker is not None:
            _, band_y, _, hs = top.speaker
            src_y = next((y for p0, p1, y in top.speaker_pieces if p0 <= a < p1), top.speaker_src_y)
            moved = []
            for r in rects:
                y0, y1 = max(band_y, r[1] - src_y + band_y), min(band_y + hs, r[3] - src_y + band_y)
                if y1 > y0:
                    moved.append((r[0], y0, r[2], y1))
            rects = moved
        if out and out[-1][1] == a and out[-1][2] == rects:
            out[-1] = (out[-1][0], b, rects)
        else:
            out.append((a, b, rects))
    plan.person_zones = out


def person_bands(plan: Plan, f0: int, f1: int, gap: float = 0.0,
                 skip: list[tuple[int, int]] | tuple = ()) -> list[tuple[float, float]]:
    """Vertical bands taken by the cut-out body during [f0, f1) (empty unless green screen)."""
    out = []
    for a, b, rects in plan.person_zones:
        if b <= f0 or a >= f1:
            continue
        lo_f, hi_f = max(a, f0), min(b, f1)
        if any(h0 <= lo_f and hi_f <= h1 for h0, h1 in skip):
            continue
        for r in rects:
            out.append((r[1] - gap, r[3] + gap))
    return out


def image_bands(plan: Plan, f0: int, f1: int, gap: float = 0.0,
                skip: list[tuple[int, int]] | tuple = ()) -> list[tuple[float, float]]:
    """Vertical bands taken by the main area of a whole background picture during [f0, f1) (green screen only)."""
    out = []
    for a, b, r in plan.image_zones:
        if b <= f0 or a >= f1:
            continue
        lo_f, hi_f = max(a, f0), min(b, f1)
        if any(h0 <= lo_f and hi_f <= h1 for h0, h1 in skip):
            continue
        out.append((r[1] - gap, r[3] + gap))
    return out


def caption_room(plan: Plan):
    """Green screen: the height auto captions need between a whole background picture and the head, for the frame
    spans of that picture (0 when the captions are off, manual or silent there)."""
    cap = _section(plan.data, "captions")
    if not _enabled(cap):
        return None
    try:
        manual = None if _is_auto(cap.get("center_y")) else float(cap.get("center_y")) * plan.s
        cues = load_json(plan.edit_dir / str(cap.get("file") or "captions.json")) or {}
        style = cap.get("style") or (cues.get("style") if isinstance(cues, dict) else None)\
            or (load_style_catalog().get("defaults") or {}).get("caption_style") or "impact"
        above, below = tr.caption_extent(cues, style, (plan.W, plan.H), float(cap.get("font_scale", 1.0) or 1.0),
                                         plan.fps)
    except (ValueError, TypeError, OSError, AttributeError):
        return None                 # build_tracks reports what is wrong with the captions
    if manual is not None:
        # a height written by hand keeps its place unless it lands on the face (then it is placed like auto)
        gap = GAP_FACE * plan.H
        faces = [(cs["face"]["zone"]["y0"] * plan.H - gap, cs["face"]["zone"]["y1"] * plan.H + gap)
                 for cs in plan.cam_segs if cs.get("tv_mode") == "person" and (cs.get("face") or {}).get("zone")]
        if not any(manual - above < b and a < manual + below for a, b in faces):
            return None
    spans = []
    for cue in (cues.get("cues") if isinstance(cues, dict) else cues) or []:
        try:
            spans.append((plan.fr(float(cue["start"])), plan.fr(float(cue["end"]))))
        except (KeyError, TypeError, ValueError):
            continue
    need = above + below + GAP_FACE * plan.H

    def room(item_spans: list[tuple[int, int]]) -> float:
        heard = any(a < f1 and f0 < b for f0, f1 in item_spans for a, b in spans)
        return need if heard else 0.0
    return room


def place_pips_screen_green(plan: Plan, inserts: list[Insert]) -> None:
    """Green screen: a pip card goes to the top corner away from the person (the one asked, when it is free of the
    face), and to the other corner when the face would be under it."""
    cfg = plan.tv["cfg"]
    W, s = plan.W, plan.s
    for ins in inserts:
        if ins.type != "pip" or not ins.card:
            continue
        ox, oy, ow, oh = ins.card
        margin_x = int(round(48 * s))
        corners = {"top_right": W - margin_x - ow, "top_left": margin_x}
        asked = (ins.item or {}).get("corner")
        away = "top_left" if cfg.position == "right" else "top_right"
        order = [asked, "top_right" if asked == "top_left" else "top_left"] if asked else\
            [away, "top_right" if away == "top_left" else "top_left"]
        faces = []
        for cs in plan.cam_segs:
            face = cs.get("face")
            if cs.get("tv_mode") != "person" or not face or not face.get("zone") or cs["f1"] <= ins.f0 or cs["f0"] >= ins.f1:
                continue
            z = face["zone"]
            faces.append((z["x0"] * W, z["y0"] * plan.H, z["x1"] * W, z["y1"] * plan.H))
        pad = GAP_FACE * plan.H
        chosen = None
        for corner in order:
            rect = (corners[corner] - pad, oy - pad, corners[corner] + ow + pad, oy + oh + pad)
            if not any(boxes_hit(rect, f) for f in faces):
                chosen = corner
                break
        if chosen is None:
            chosen = order[0]
            plan.warnings.append(f'insert card {ins.idx + 1} (picture-in-picture) overlaps the face in both upper corners; change its timing or the speaker position')
        elif asked and chosen != asked:
            plan.adjustments.append({"layer": f'insert {ins.idx + 1}', "action": "moved", "start": round(ins.f0 / plan.fps, 3),
                                     "end": round(ins.f1 / plan.fps, 3), "because": "face (green screen)",
                                     "corner_asked": asked, "corner": chosen})
            plan.warnings.append(f"insert card {ins.idx + 1} picture-in-picture moved to the {('left' if chosen == 'top_left' else 'right')} corner to avoid the face")
        x = corners[chosen]
        border = ins.content[0] - ox
        ins.card = (x, oy, ow, oh)
        ins.content = (x + border, ins.content[1], ins.content[2], ins.content[3])


def render_screen_green(plan: Plan) -> dict:
    import person_cutout as TV

    tv = plan.tv
    job = TV.Job(cfg=tv["cfg"], segments=tv["segments"], mattes=tv["mattes"], base=plan.base,
                 base_pre=_matte_pre(plan), base_fps=plan.base_fps, fps=plan.fps, size=(plan.W, plan.H),
                 full_size=(plan.full_w, plan.full_h), nf=plan.nf, out_dir=plan.renders / "telaverde",
                 plates=tv.get("plates") or {})
    try:
        return TV.render(job, progress=log)
    except TV.CutoutError as e:
        raise ComposeError(str(e), e.code)


def _matte_pre(plan: Plan) -> list[str]:
    import matte as MT
    return MT.base_pre_filters(plan.base_fps, (plan.base_w, plan.base_h), plan.fps, (plan.full_w, plan.full_h))


def screen_green_layer(plan: Plan, clip: dict) -> dict:
    import person_cutout as TV

    tv = plan.tv
    cfg = tv["cfg"]
    fps = plan.fps
    background_value = TV.report_backgrounds(cfg, fps, plan.W, plan.H, 1.0 / plan.s, plan.rel)
    moved = [{"insert": b.insert, "type": b.insert_type, "src": plan.rel(b.src), "kind": b.kind,
              "start": round(b.f0 / fps, 3), "end": round(b.f1 / fps, 3), "fit": b.fit,
              'requested_fit': b.fit_asked, "ken_burns": b.ken_burns, "darken": b.darken, "blur": b.blur,
              "rect": [int(round(v / plan.s)) for v in b.place] if b.place else None,
              "stage": [int(round(v / plan.s)) for v in b.stage] if b.stage else None}
             for b in cfg.from_inserts]
    mset = tv["mset"]
    return {"type": "greenScreen", "position": cfg.position, "height": cfg.height,
            "outline": ({"color": "#%02X%02X%02X" % tuple(cfg.outline[0]), "width": cfg.outline[1]}
                         if cfg.outline else None),
            "shadow": cfg.shadow, "blurOriginalBackground": cfg.blurred_original, "withoutFace": cfg.in_face,
            "mirror": cfg.mirror,
            'inner_cut': ({k: v for k, v in tv["inner_cut"].items() if k != 'warning'} if tv.get("inner_cut") else None),
            "background": background_value, "background_inserts": moved,
            "segments": TV.report_segments(cfg, tv["segments"], fps, plan.W, plan.H, 1.0 / plan.s),
            "matte": mset.report(plan.rel) if mset else None,
            'room_background': tv.get("plate_rows") or [],
            "clip": plan.rel(Path(clip["file"])), "cached": bool(clip.get("cached")),
            "seconds": {"matte": tv["matte_seconds"], "compose": clip.get("render_seconds", 0.0)}}


def _kb_path(ins: Insert, n: int, pw: float, ph: float, cw: int, ch: int):
    """Per-frame (window_w, window_h, center_x, center_y) in prescaled source pixels."""
    zoom_in = ins.idx % 2 == 0
    z0, z1 = (1.0, 1.0 + KB_DRIFT) if zoom_in else (1.0 + KB_DRIFT, 1.0)
    base_w, base_h = cw * (1.0 + KB_DRIFT), ch * (1.0 + KB_DRIFT)
    dirs = [(1, -0.5), (-1, 0.5), (0.6, 1), (-0.6, -1)]
    dx, dy = dirs[ins.idx % 4]
    frames = []
    for j in range(n):
        p = ease_in_out(j / max(1, n - 1))
        z = z0 + (z1 - z0) * p
        ww, wh = base_w / z, base_h / z
        # pan inside whatever room the window leaves (at most 3% of the window)
        room_x = max(0.0, (pw - base_w) / 2.0) + (base_w - ww) / 2.0
        room_y = max(0.0, (ph - base_h) / 2.0) + (base_h - wh) / 2.0
        span_x = min(room_x, 0.03 * ww) * dx
        span_y = min(room_y, 0.03 * wh) * dy
        cx = pw / 2.0 + span_x * (2 * p - 1)
        cy = ph / 2.0 + span_y * (2 * p - 1)
        cx = min(max(cx, ww / 2.0), pw - ww / 2.0)
        cy = min(max(cy, wh / 2.0), ph - wh / 2.0)
        frames.append((ww, wh, cx, cy))
    return frames


def _encoder(out_tmp: Path, w: int, h: int, fps: int, n: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f'{w}x{h}', "-framerate", str(fps), "-i", "-",
         "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(fps),
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
         "-frames:v", str(n), "-f", "mp4", str(out_tmp)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def render_insert_clip(plan: Plan, ins: Insert) -> Path:
    cw, ch = ins.content[2], ins.content[3]
    n = ins.f1 - ins.f0
    st = ins.src.stat()
    key = short_hash((RENDER_VERSION, str(ins.src), st.st_size, st.st_mtime_ns, ins.kind, cw, ch, n, plan.fps,
                      ins.ken_burns, ins.idx % 4))
    out_dir = plan.renders / "inserts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'ins_{key}.mp4'
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_name(".tmp-" + out.name)
    margin = 1.0 + KB_DRIFT if ins.ken_burns else 1.0

    if ins.kind == "video" and not ins.ken_burns:
        ffmpeg_cmd(["-v", "error", "-y", "-stream_loop", "-1", "-i", ins.src, "-an",
                    "-vf", (f'fps={plan.fps},scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch},setsar=1,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(plan.fps),
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                    "-frames:v", n, "-f", "mp4", tmp], f'prepare insert video {ins.idx + 1}')
        os.replace(tmp, out)
        return out

    pw_target, ph_target = cw * margin, ch * margin
    if ins.kind == "image":
        with Image.open(ins.src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (18, 18, 18))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                src_img = bg
            else:
                src_img = im.convert("RGB")
        sc = max(pw_target / src_img.width, ph_target / src_img.height)
        pw, ph = max(cw, int(math.ceil(src_img.width * sc))), max(ch, int(math.ceil(src_img.height * sc)))
        pre = src_img.resize((pw, ph), Image.LANCZOS)

        def frames_iter():
            while True:
                yield pre
    else:
        pw, ph = int(math.ceil(pw_target)), int(math.ceil(ph_target))
        dec = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-stream_loop", "-1", "-i", str(ins.src), "-an",
             "-vf", f'fps={plan.fps},scale={pw}:{ph}:force_original_aspect_ratio=increase:flags=lanczos,crop={pw}:{ph},format=rgb24',
             "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def frames_iter():
            size = pw * ph * 3
            last = None
            while True:
                buf = dec.stdout.read(size) if dec.stdout else b""
                if len(buf) == size:
                    last = Image.frombuffer("RGB", (pw, ph), buf, "raw", "RGB", 0, 1)
                if last is None:
                    raise ComposeError(f'could not read insert video {ins.idx + 1}: {ins.src}')
                yield last

    enc = _encoder(tmp, cw, ch, plan.fps, n)
    try:
        path = _kb_path(ins, n, pw, ph, cw, ch) if ins.ken_burns else None
        it = frames_iter()
        for j in range(n):
            frame = next(it)
            if path is None:
                x0, y0 = (frame.width - cw) // 2, (frame.height - ch) // 2
                img = frame.crop((x0, y0, x0 + cw, y0 + ch))
            else:
                ww, wh, cx, cy = path[j]
                qx, qy = ww / cw, wh / ch
                img = frame.transform((cw, ch), Image.AFFINE,
                                      (qx, 0.0, cx - ww / 2.0, 0.0, qy, cy - wh / 2.0),
                                      resample=Image.BICUBIC)
            enc.stdin.write(img.tobytes())
        enc.stdin.close()
        err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        if enc.wait() != 0:
            raise ComposeError(f'failed to encode insert {ins.idx + 1}: {err[-500:]}')
    except BrokenPipeError:
        err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        raise ComposeError(f'failed to encode insert {ins.idx + 1}: {err[-500:]}')
    finally:
        if ins.kind == "video":
            try:
                dec.kill()  # type: ignore[possibly-undefined]
            except Exception:
                pass
    os.replace(tmp, out)
    return out


# ============================================================== motion (animated texts and numbers)

MOTION_KEYS = ("template", "start", "end", "duration", "params", "region", "sfx", "enabled", "id", "note")
MOTION_REGIONS = {"auto": None, "top": "top", "middle": "center", "center": "center", "bottom": "bottom",
                  "top_band": "top_band", "screen": "screen"}
MOTION_EXAMPLE = '{"template": "checklist", "start": 12.3, "end": 15.0, "params": {"items": ["Instant access"]}}'
MOTION_CAPTION_SHIFT = 150.0   # design px: an auto item steps aside from the captions only when that moves it this little
MOTION_GAP = 23.0              # design px between an auto item and a face or a text above it in priority


def _of_names(names: list[str]) -> str:
    'Join unique labels into a readable list.'
    seen = list(dict.fromkeys(names))
    parts = ["the " + n for n in seen]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


@dataclass
class Visual:
    idx: int                   # 1-based position in the list (as the report and the warnings call it)
    template: str
    item: object               # motion.render.VisualItem
    f0: int
    f1: int
    auto: bool                 # region "auto": the template's place, slid off faces and texts
    sfx: bool
    region_asked: object
    clip: dict | None = None
    ink: tuple | None = None   # render px on the output canvas (what the clip really draws)
    shown: list = field(default_factory=list)
    cues: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return f'animation {self.idx} ({self.template})'


def _motion_items(data: dict) -> list | None:
    """The list under "motion": a list, or {"enabled": bool, "items": [...]}. None when it is off or absent."""
    raw = data.get("motion")
    if raw is None:
        return None
    if isinstance(raw, dict):
        if raw.get("enabled", True) is False:
            return None
        extra = sorted(set(raw) - {"enabled", "items", "theme", "count"})
        if extra:
            raise ComposeError(f"""motion has unknown fields: {', '.join(extra)} (use "items", "enabled", "theme", and "count")""", 2)
        raw = raw.get("items", [])
    if not isinstance(raw, list):
        raise ComposeError(f'motion must be a list of animations, for example "motion": [{MOTION_EXAMPLE}])', 2)
    return raw


def _motion_region(value, where: str):
    """(region spec for the engine or None for the template's own place, auto?)"""
    if value is None:
        return None, True
    if isinstance(value, str):
        key = value.strip().lower()
        if key not in MOTION_REGIONS:
            import difflib
            near = difflib.get_close_matches(key, list(MOTION_REGIONS), n=1, cutoff=0.6)
            hint = f' Did you mean "{near[0]}"?' if near else ""
            raise ComposeError(f'{where}.region "{value}" does not exist.{hint} Use "auto", "top", "middle", "bottom" or {{"x", "y", "w", "h"}} in pixels on the 1080x1920 canvas', 2)
        spec = MOTION_REGIONS[key]
        return spec, spec is None
    box = None
    if isinstance(value, dict) and set(value) == {"x", "y", "w", "h"}:
        box = [value["x"], value["y"], value["w"], value["h"]]
    elif isinstance(value, list) and len(value) == 4:
        box = value
    nums = [_plain_number(v) for v in box] if box is not None else None
    if not nums or any(v is None for v in nums) or nums[2] < 20 or nums[3] < 20:
        raise ComposeError(f'{where}.region must be "auto", "top", "middle", "bottom", or {{"x": 140, "y": 1000, "w": 800, "h": 520}}, in 1080x1920 pixels with width and height at least 20 (received {_as_json(value)})', 2)
    return nums, False


def _motion_theme(data: dict) -> str | None:
    """The look chosen for all the animations ("motion": {"theme": "paper"}), or None."""
    raw = data.get("motion")
    theme_value = raw.get("theme") if isinstance(raw, dict) else None
    if theme_value is None:
        return None
    from visuals import engine as ME
    if theme_value not in ME.THEME_NAMES:
        raise ComposeError(f'''motion.theme "{theme_value}" does not exist; use {', '.join(ME.THEME_NAMES)})''', 2)
    return str(theme_value)


def plan_motion(plan: Plan) -> list[Visual]:
    """edit_data "motion": check every item and decide its frames before anything renders (exit 2 on a mistake)."""
    raw = _motion_items(plan.data)
    if not raw:
        return []
    from visuals import engine as ME
    from visuals import registry as MRg
    from visuals import render as MR

    out: list[Visual] = []
    for i, entry in enumerate(raw):
        where = f'motion[{i}]'
        n = i + 1
        if not isinstance(entry, dict):
            raise ComposeError(f'{where} must be an object, for example {MOTION_EXAMPLE})', 2)
        if entry.get("enabled", True) is False:
            continue
        unknown = [k for k in entry if k not in MOTION_KEYS]
        if unknown:
            import difflib
            near = difflib.get_close_matches(str(unknown[0]), list(MOTION_KEYS), n=1, cutoff=0.6)
            hint = f' Did you mean "{near[0]}"?' if near else ""
            raise ComposeError(f"{where}.{unknown[0]} does not exist.{hint} Campos: {', '.join(MOTION_KEYS[:7])}", 2)
        tid = entry.get("template")
        if not isinstance(tid, str) or not tid.strip():
            names = ", ".join(sorted(MRg.templates()))
            raise ComposeError(f'{where} requires "template": an animation template ({names})', 2)
        tid = tid.strip()
        start = _plain_number(entry.get("start"))
        if start is None or start < 0:
            if "start" not in entry:
                raise ComposeError(f'{where} requires "start": the cut time in seconds when the animation begins, for example {MOTION_EXAMPLE})', 2)
            raise ComposeError(f"{where}.start must be nonnegative seconds (received {_as_json(entry.get('start'))})", 2)
        end = dur = None
        if entry.get("end") is not None and entry.get("duration") is not None:
            raise ComposeError(f'{where}: use either "end" or "duration", not both', 2)
        if entry.get("end") is not None:
            end = _plain_number(entry.get("end"))
            if end is None or end <= start:
                raise ComposeError(f"{where}.end must be seconds greater than start ({start:g}) (got {_as_json(entry.get('end'))})", 2)
        if entry.get("duration") is not None:
            dur = _plain_number(entry.get("duration"))
            if dur is None or dur <= 0:
                raise ComposeError(f"{where}.duration must be positive seconds (received {_as_json(entry.get('duration'))})", 2)
        params = entry.get("params")
        if params is None:
            params = {}
        theme_value = _motion_theme(plan.data)
        if theme_value and isinstance(params, dict) and "theme" not in params:
            params = {**params, "theme": theme_value}      # "motion": {"theme": ...} is the look of every item
        if not isinstance(params, dict):
            raise ComposeError(f'{where}.params must be an object of template parameters; see render_visuals.py --describe {tid})', 2)
        spec, auto = _motion_region(entry.get("region", "auto"), where)
        sfx_on = entry.get("sfx", True)
        if not isinstance(sfx_on, bool):
            raise ComposeError(f'{where}.sfx must be true or false (received {_as_json(sfx_on)})', 2)
        try:
            tpl = MRg.get(tid)
            f0 = plan.fr(start)
            if f0 >= plan.nf:
                plan.warnings.append(f"animation {n} ('{tid}' at {fmt_time(start)}) starts after the video ends ({fmt_time(plan.duration)}) and was ignored")
                continue
            if end is not None:
                f1 = plan.fr(end)
                if end * plan.fps > plan.nf + 0.5:
                    plan.warnings.append(f"animation {n} ('{tid}') exceeded the video ending and was trimmed at {fmt_time(plan.duration)}")
            else:
                secs = dur if dur is not None else tpl.suggested_duration(MRg.resolve_params(tpl, params))
                f1 = f0 + ME.frame_of(secs, plan.fps)
                if f1 > plan.nf:
                    plan.warnings.append(f"animation {n} ('{tid}') exceeded the video ending and was trimmed at {fmt_time(plan.duration)}: start earlier or shorten the duration")
            f1 = min(f1, plan.nf)
            if f1 - f0 < 2:
                plan.warnings.append(f"animation {n} ('{tid}' at {fmt_time(start)}) lasts fewer than two frames and was ignored")
                continue
            item = MR.plan_item(tid, params, fps=plan.fps, canvas=(plan.W, plan.H), region=spec, frames=f1 - f0,
                                accent=plan.accent)
            MR.sfx_cues(item)
            MR.render_frame(item, 0)     # a mistake only the drawing sees (a target off the screen) stops here too
        except ME.VisualError as e:
            raise ComposeError(f'{where}: {e}', 2 if e.code == 2 else 1)
        for w in item.warnings:
            plan.warnings.append(f'animation {n}: {w}')
        item.warnings.clear()
        settle = tpl.settle(item.params, plan.fps)
        if settle is not None:
            # an end earlier than the drawing: the last items would never show (or never get their check / cross)
            frames = f1 - f0
            exit_frames = min(MR.EXIT_FRAMES, frames // 3) if tpl.exit == "fade" else 0
            need = settle + exit_frames / plan.fps
            if need * plan.fps > frames + 1:
                plan.warnings.append(f"animation {n} ('{tid}' at {fmt_time(start)}) ends before the drawing completes; it needs {ME.format_number(need, 1)} seconds; retained {ME.format_number(frames / plan.fps, 1)} seconds. Increase end or omit it to use the suggested duration ({ME.format_number(tpl.suggested_duration(item.params), 1)} s)")
        out.append(Visual(idx=n, template=tid, item=item, f0=f0, f1=f1, auto=auto, sfx=sfx_on,
                          region_asked=entry.get("region", "auto")))
    for k, a in enumerate(out):
        for b in out[k + 1:]:
            if b.f0 < a.f1 and a.f0 < b.f1:
                plan.warnings.append(f'animations {a.idx} and {b.idx} appear together ({fmt_time(max(a.f0, b.f0) / plan.fps)}): show one animation at a time')
    return out


def place_motion(plan: Plan, motions: list[Visual], highs: list[tuple[str, int, int, tuple]],
                 caption_bands) -> None:
    """Auto items slide off the faces, the CTA, the hook and the animations before them (and off the captions
    when that costs little); the others stay where they were asked. Then every clip is rendered."""
    if not motions:
        return
    from visuals import render as MR

    k = min(plan.W, plan.H) / 1080.0
    fps = plan.fps
    for m in motions:
        item = m.item
        if not (m.auto and item.template.movable):
            continue
        faces: list[tuple[str, tuple[float, float]]] = []          # (why, band in design px)
        faces += [("face", (a / k, b / k)) for a, b in face_bands(plan, m.f0, m.f1)]
        texts = []
        for why, h0, h1, band in highs:
            if h0 < m.f1 and m.f0 < h1:
                texts.append((why, (band[0] / k, band[1] / k)))
        others = []
        for other in motions:
            if other.idx >= m.idx or other.item.ink is None or not (other.f0 < m.f1 and m.f0 < other.f1):
                continue
            ob = other.item.box
            others.append((f'animation {other.idx}', (ob.y + other.item.ink[1], ob.y + other.item.ink[3])))
        # no place free of everything: the face still wins. First give up the other animations (they only share a
        # moment), then the CTA and the hook (they win anyway: the animation is hidden where it touches them).
        # Place captions outside the entire cut-out body when there is room.
        levels = [faces + texts + others, faces + texts, faces]
        gave = [[], others, texts + others]
        body = [("person", (a / k, b / k)) for a, b in person_bands(plan, m.f0, m.f1)]
        if body:
            levels = [faces + body + texts + others] + levels
            gave = [[]] + gave
        dy, named, relaxed = None, levels[0], 0
        for lvl, cand in enumerate(levels):
            if lvl and len(cand) == len(levels[lvl - 1]):
                continue
            dy = MR.free_offset(item, [band for _, band in cand], MOTION_GAP)
            if dy is not None:
                named, relaxed = cand, lvl
                break
        bands = [band for _, band in named]
        caps = [(a / k, b / k) for a, b in caption_bands(m.f0, m.f1)]
        if caps:
            around = MR.free_offset(item, bands + caps, MOTION_GAP)
            if around is not None and (dy is None or abs(around - dy) <= MOTION_CAPTION_SHIFT):
                dy = around
                named += [("captions", band) for band in caps]
        if dy is None:
            plan.warnings.append(f'a {m.name} found no space clear of faces and text between {fmt_time(m.f0 / fps)} and {fmt_time(m.f1 / fps)}: retained its default position. Choose another region or time')
            continue
        given_up = [name for name, _ in gave[relaxed]]
        if given_up:
            plan.warnings.append(f'a {m.name} found no unobstructed space between {fmt_time(m.f0 / fps)} and {fmt_time(m.f1 / fps)}: kept clear of the face but close to {_of_names(given_up)}. Change its timing to avoid sharing the screen')
        if abs(dy) >= 0.5:
            ink = item.ink
            before = (item.box.y + (ink[1] + ink[3]) / 2.0) if ink else item.box.y
            top, bottom = (item.box.y + ink[1], item.box.y + ink[3]) if ink else (item.box.y, item.box.y + item.box.h)
            why = []
            for name, (b0, b1) in named:        # what the item touched where it was asked
                if b0 - MOTION_GAP < bottom and top < b1 + MOTION_GAP and name not in why:
                    why.append(name)
            MR.move_item(item, dy)
            ko = min(plan.full_w, plan.full_h) / 1080.0          # design px -> px of the full output
            plan.adjustments.append({"layer": f'motion {m.idx}', "action": "moved", "start": round(m.f0 / fps, 3),
                                     "end": round(m.f1 / fps, 3), "because": " and ".join(why) or "face or text",
                                     "from_center_y": int(round(before * ko)),
                                     "center_y": int(round((before + dy) * ko))})

    out_dir = plan.renders / "motion"
    workers = max(1, min(4, len(motions), os.cpu_count() or 2))

    def work(m: Visual) -> dict:
        return MR.render_clip(m.item, out_dir)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            clips = list(ex.map(work, motions))
    except Exception as e:  # noqa: BLE001
        from visuals.engine import VisualError
        if isinstance(e, VisualError):
            raise ComposeError(f'failed to draw an animation: {e}', 2 if e.code == 2 else 1)
        raise
    for m, clip in zip(motions, clips):
        m.clip = clip
        m.ink = tuple(clip["ink"]) if clip.get("ink") else None
        if m.ink is None:
            plan.warnings.append(f'a {m.name} rendered no visible content and was skipped')


# ============================================================== overlay tracks (PNG states)

class Track:
    """Frame-grid layer states composited in PIL, played through an ffconcat playlist."""

    def __init__(self, plan: Plan, name: str):
        self.plan = plan
        self.name = name
        self.images: dict[str, Image.Image] = {}
        self.bboxes: dict[str, tuple | None] = {}
        self.content: dict[str, str] = {}
        self.states: list[tuple[int, int, int, tuple]] = []   # f0, f1, z, recipe
        self.z = 0

    def add_image(self, key: str, img: Image.Image) -> None:
        self.images[key] = img
        self.bboxes[key] = img.getchannel("A").getbbox()
        # content hash: cached PNG names must change whenever the art changes
        self.content[key] = hashlib.sha1(img.tobytes()).hexdigest()[:16]

    def add(self, f0: int, f1: int, recipe: tuple, z: int | None = None) -> None:
        f0, f1 = max(0, f0), min(self.plan.nf, f1)
        if f1 > f0:
            self.states.append((f0, f1, self.z if z is None else z, recipe))

    def next_z(self) -> int:
        self.z += 1
        return self.z

    def _transformed(self, key: str, scale: float, alpha: float, dy: float) -> Image.Image | None:
        img = self.images[key]
        bbox = self.bboxes[key]
        if bbox is None or alpha <= 0.001:
            return None
        if abs(scale - 1.0) < 1e-4 and alpha >= 0.999 and abs(dy) < 0.5:
            return img
        W, H = img.size
        x0, y0, x1, y1 = bbox
        part = img.crop(bbox)
        if abs(scale - 1.0) >= 1e-4:
            nw, nh = max(1, int(round(part.width * scale))), max(1, int(round(part.height * scale)))
            part = part.resize((nw, nh), Image.BICUBIC)
        if alpha < 0.999:
            part.putalpha(part.getchannel("A").point(lambda v, a=alpha: int(v * a)))
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0 + dy
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        tr._alpha_paste(canvas, part, int(round(cx - part.width / 2.0)), int(round(cy - part.height / 2.0)))
        return canvas

    def build(self, out_dir: Path) -> tuple[Path | None, list[dict], list[tuple[int, int]]]:
        plan = self.plan
        if not self.states:
            return None, [], []
        cuts = sorted({f for a, b, _, _ in self.states for f in (a, b)})
        intervals = []
        for u, v in zip(cuts, cuts[1:]):
            active = sorted((z, r) for a, b, z, r in self.states if a <= u and v <= b)
            if not active:
                continue
            key = tuple(r for _, r in active)
            if intervals and intervals[-1][1] == u and intervals[-1][2] == key:
                intervals[-1][1] = v
            else:
                intervals.append([u, v, key])
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = {}
        entries = []
        for u, v, key in intervals:
            ids = tuple(self.content.get(r[1], "") if r[0] == "img" else "" for r in key)
            name = f'{self.name}_{short_hash((RENDER_VERSION, plan.W, plan.H, key, ids))}.png'
            jobs.setdefault(name, key)
            entries.append({"file": str(out_dir / name), "start": u / plan.fps, "end": v / plan.fps})

        def work(item):
            name, key = item
            path = out_dir / name
            if path.exists():
                return
            canvas = Image.new("RGBA", (plan.W, plan.H), (0, 0, 0, 0))
            for recipe in key:
                if recipe[0] == "flash":
                    canvas.alpha_composite(Image.new("RGBA", (plan.W, plan.H), (255, 255, 255, int(round(255 * recipe[1])))))
                    continue
                _, img_key, scale, alpha, dy = recipe
                layer = self._transformed(img_key, scale, alpha, dy)
                if layer is not None:
                    canvas.alpha_composite(layer)
            tmp = path.with_name(".tmp-" + path.name)
            canvas.save(tmp, "PNG", compress_level=1)
            os.replace(tmp, path)

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
            list(ex.map(work, jobs.items()))
        ff = out_dir / f'{self.name}.ffconcat'
        tr.write_ffconcat(entries, str(ff), total_duration=plan.duration,
                          blank_png=str(out_dir / f'blank_{plan.W}x{plan.H}.png'), fps=plan.fps)
        windows = merge_windows([(plan.fr(e["start"]), plan.fr(e["end"])) for e in entries])
        return ff, entries, windows


def merge_windows(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def enable_expr(windows: list[tuple[int, int]], nf: int) -> str | None:
    if not windows:
        return None
    if len(windows) == 1 and windows[0][0] <= 0 and windows[0][1] >= nf:
        return None
    if len(windows) > 120:
        return None
    return "+".join(f'between(n,{a},{b - 1})' for a, b in windows)


def seam_shadow(plan: Plan, ins: Insert) -> Image.Image:
    """Soft shadow the picture band casts on the speaker band, so the split reads as layers."""
    W, H, s = plan.W, plan.H, plan.s
    depth = max(4, int(round(34 * s)))
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ramp = Image.new("L", (1, depth))
    for y in range(depth):
        ramp.putpixel((0, y), int(round(255 * 0.42 * (1.0 - y / depth) ** 2)))
    ramp = ramp.resize((W, depth))
    band = Image.new("RGBA", (W, depth), (0, 0, 0, 255))
    seam = int(ins.seam_y or 0)
    if ins.type == "split_top":        # speaker below the seam
        band.putalpha(ramp)
        canvas.alpha_composite(band, (0, min(H - depth, seam)))
    else:                              # speaker above the seam
        band.putalpha(ramp.transpose(Image.FLIP_TOP_BOTTOM))
        canvas.alpha_composite(band, (0, max(0, seam - depth)))
    return canvas


def pip_decoration(plan: Plan, ins: Insert) -> Image.Image:
    W, H, s = plan.W, plan.H, plan.s
    ox, oy, ow, oh = ins.card  # type: ignore[misc]
    border = ins.content[0] - ox
    radius = border * 2.8
    ss = 3
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # soft shadow below the card (never inside it: the card content sits under this layer)
    pad = int(round(60 * s))
    sw, sh = ow + 2 * pad, oh + 2 * pad
    mask = Image.new("L", (sw, sh), 0)
    off = int(round(12 * s))
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad + off, pad + ow, pad + oh + off), radius=radius, fill=int(255 * 0.55))
    mask = mask.filter(ImageFilter.GaussianBlur(20 * s))
    card_mask = Image.new("L", (sw * ss, sh * ss), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle((pad * ss, pad * ss, (pad + ow) * ss - 1, (pad + oh) * ss - 1),
                                                radius=radius * ss, fill=255)
    card_mask = card_mask.resize((sw, sh), Image.LANCZOS)
    mask = Image.composite(Image.new("L", (sw, sh), 0), mask, card_mask)
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 255))
    shadow.putalpha(mask)
    tr._alpha_paste(canvas, shadow, ox - pad, oy - pad)
    # white ring: outer rounded rect minus inner rounded rect (covers the content corners)
    ring = Image.new("L", (ow * ss, oh * ss), 0)
    d = ImageDraw.Draw(ring)
    d.rounded_rectangle((0, 0, ow * ss - 1, oh * ss - 1), radius=radius * ss, fill=255)
    d.rounded_rectangle((border * ss, border * ss, (ow - border) * ss - 1, (oh - border) * ss - 1),
                        radius=max(1.0, (radius - border)) * ss, fill=0)
    ring = ring.resize((ow, oh), Image.LANCZOS)
    white = Image.new("RGBA", (ow, oh), (255, 255, 255, 255))
    white.putalpha(ring)
    tr._alpha_paste(canvas, white, ox, oy)
    return canvas


HOOK_IN = [(0.80, 0.55), (0.95, 0.92), (1.045, 1.0), (1.015, 1.0)]   # (scale, alpha) per frame
HOOK_OUT = [0.66, 0.33]
HOOK_MAX_SCALE = 1.05
CTA_IN = [(0.90, 0.3, 60), (0.94, 0.6, 34), (0.98, 0.85, 14), (1.015, 1.0, 4), (1.02, 1.0, 0), (1.005, 1.0, 0)]
CTA_MAX_SCALE = 1.03           # entrance and pulse
CTA_SLIDE = 14                 # px (1080 wide) the CTA still sits lower on its first solid entrance frame
SFX_CALIBRATION = make_sfx.SFX_CALIBRATION   # 1.4: gain at sfx.volume 1.0 (levels per effect in assets/sfx/sfx.json)
AAC_TP_HEADROOM = 1.0          # AAC encoding adds inter-sample peaks: normalize a bit under the target
FLASH = [(-1, 0.35), (0, 0.9), (1, 0.6), (2, 0.32), (3, 0.12)]

# text placement (fractions of the canvas height)
GAP_FACE = 0.012               # room between a text and a face
GAP_TEXT = 0.005               # room between two texts
SOFT_SHIFT = 60 / 1920         # captions may move this much so the disclaimer keeps its usual place
FACE_COVER_WARN = 0.05         # share of the face (brows to lower lip) a text may cover before a warning
PRIORITY = ("cta", "hook", "motion", "captions", "disclaimer")   # motion = the animations of edit_data "motion"
TEXT_LAYERS = ("cta", "hook", "captions", "disclaimer")
LABELS = {"cta": "the call to action", "hook": "the hook", "captions": "the captions", "disclaimer": 'the disclaimer',
          "motion": 'the animation'}
OF_LABELS = {"cta": "of the call to action", "hook": "of the hook", "captions": "of the captions", "disclaimer": 'of the disclaimer',
             "motion": 'of the animation'}
MIN_PIECE = {"hook": 0.3, "motion": 0.5, "captions": 0.13, "disclaimer": 1.0}   # s: shorter leftovers of a hidden layer are hidden too
DISCLAIMER_HIDDEN_WARN = 0.2   # share of its time the disclaimer may lose to other texts before a warning
CENTER_KEYS = {"cta": "cta.center_y", "hook": "hook.center_y", "captions": "captions.center_y",
               "disclaimer": "disclaimer.center_y"}


def clamp_cue_words(cues):
    """Copy of the cues with word times kept inside their cue (a cue clamped to a cut must not
    start its first word a frame before that cut)."""
    if not isinstance(cues, dict):
        return cues
    out = dict(cues)
    fixed = []
    for cue in cues.get("cues") or []:
        try:
            c0, c1 = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            fixed.append(cue)
            continue
        c = dict(cue)
        words = []
        for w in cue.get("words") or []:
            try:
                ws, we = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            ws = min(max(ws, c0), c1)
            words.append(dict(w, start=ws, end=min(max(we, ws), c1)))
        c["words"] = words
        fixed.append(c)
    out["cues"] = fixed
    return out


def check_captions_cut(plan: Plan, cues, cap_file: Path) -> None:
    """Refuse captions made for another cut: every caption after a changed cut would be out of sync."""
    if not isinstance(cues, dict):
        return
    name = plan.rel(cap_file)
    hint = 'Run captions.py again for this cut.'
    tl = plan.timeline
    if tl:
        key, current = cues.get("timeline_key"), captions_mod.timeline_key(tl)
        if key and current and key != current:
            raise ComposeError(f'{name} belongs to another cut: cut_timeline.json changed after captions were generated. {hint}', 2)
        bounds = cues.get("cut_boundaries")
        if isinstance(bounds, list):
            starts = []
            for seg in tl.get("segments") or []:
                try:
                    starts.append(float(seg["out_start"]))
                except (KeyError, TypeError, ValueError):
                    continue
            want = sorted(starts)[1:]
            try:
                got = sorted(float(b) for b in bounds)
            except (TypeError, ValueError):
                got = []
            if len(want) != len(got) or any(abs(a - b) > 0.02 for a, b in zip(want, got)):
                show = lambda xs: ", ".join(f'{x:.2f}' for x in xs[:5]) + (" ..." if len(xs) > 5 else "")  # noqa: E731
                raise ComposeError(f"{name} belongs to another cut: captions contain joins at {show(got) or 'none'} s; the current cut has them at {show(want) or 'none'} s. {hint}", 2)
    ends = []
    for cue in cues.get("cues") or []:
        try:
            ends.append(float(cue["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    if ends and max(ends) > plan.duration + 0.5:
        raise ComposeError(f'{name} belongs to another cut: its last caption ends at {fmt_time(max(ends))} and the video has {fmt_time(plan.duration)}. {hint}', 2)


def _center_value(sec: dict, key: str, what: str) -> float | None:
    v = sec.get(key)
    if _is_auto(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ComposeError(f'{what} must be a number in output pixels, or "auto"', 2)


def grow_box(bb, scale: float = 1.0, extra_below: float = 0.0):
    if not bb:
        return None
    cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
    hw, hh = (bb[2] - bb[0]) / 2.0 * scale, (bb[3] - bb[1]) / 2.0 * scale
    return (cx - hw, cy - hh, cx + hw, cy + hh + extra_below)


def boxes_hit(a, b) -> bool:
    return bool(a) and bool(b) and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def cover_share(box, zone) -> float:
    if not box or not zone:
        return 0.0
    w = min(box[2], zone[2]) - max(box[0], zone[0])
    h = min(box[3], zone[3]) - max(box[1], zone[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1e-6, (zone[2] - zone[0]) * (zone[3] - zone[1]))


def subtract_windows(spans: list[tuple], windows: list[tuple[int, int]]) -> list[tuple]:
    """(f0, f1, *payload) spans minus frame windows."""
    out = []
    for f0, f1, *rest in spans:
        pieces = [(f0, f1)]
        for a, b in windows:
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
        out += [(p0, p1, *rest) for p0, p1 in pieces if p1 > p0]
    return out


def compute_face_zones(plan: Plan, inserts: list[Insert]) -> None:
    """Face zones as drawn in the output (after the camera and the inserts), per frame interval."""
    W, H = plan.W, plan.H
    pieces = []
    for cs in plan.cam_segs:
        zones, colors_value = [], []
        face = cs.get("face")
        if face and face.get("zone"):
            for key, dst in (("zone", zones), ("core", colors_value)):
                if not face.get(key):
                    continue
                rects = [face_layout.zoom_rect(face[key], z, cs["focus_y"]) for z in {cs["zoom"], cs["zoom_max"]}]
                dst.append((min(r["x0"] for r in rects) * W, min(r["y0"] for r in rects) * H,
                            max(r["x1"] for r in rects) * W, max(r["y1"] for r in rects) * H))
        pieces.append((cs["f0"], cs["f1"], zones, colors_value))
    cuts = sorted({0, plan.nf} | {p[0] for p in pieces} | {p[1] for p in pieces}
                  | {min(plan.nf, i.f0) for i in inserts} | {min(plan.nf, i.f1) for i in inserts})
    out: list[tuple] = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        base = next((p for p in pieces if p[0] <= a < p[1]), None)
        zones, colors_value = (base[2], base[3]) if base else ([], [])
        top = None
        for ins in inserts:                      # list order: the last one covering is drawn on top
            if ins.type != "pip" and ins.f0 <= a and b <= ins.f1:
                top = ins
        if top is not None and top.type == "full":
            zones, colors_value = [], []
        elif top is not None and top.speaker is not None:
            _, band_y, _, hs = top.speaker
            src_y = next((y for p0, p1, y in top.speaker_pieces if p0 <= a < p1), top.speaker_src_y)

            def moved(r, src_y=src_y, band_y=band_y, hs=hs):
                y0 = max(band_y, r[1] - src_y + band_y)
                y1 = min(band_y + hs, r[3] - src_y + band_y)
                return (r[0], y0, r[2], y1) if y1 > y0 else None

            zones = [m for m in map(moved, zones) if m]
            colors_value = [m for m in map(moved, colors_value) if m]
        if out and out[-1][1] == a and out[-1][2] == zones and out[-1][3] == colors_value:
            out[-1] = (out[-1][0], b, zones, colors_value)
        else:
            out.append((a, b, zones, colors_value))
    plan.zones = out
    pad = 12 * plan.s
    plan.obstacles = [(i.f0, i.f1, (i.card[0] - pad, i.card[1] - pad, i.card[0] + i.card[2] + pad, i.card[1] + i.card[3] + pad))
                      for i in inserts if i.type == "pip" and i.card]
    plan.obstacles += screen_obstacles(plan)


def screen_obstacles(plan: Plan) -> list[tuple[int, int, tuple[float, float, float, float]]]:
    'Existing on-screen text in edit/screen_text.json becomes a layout obstacle.'
    doc = load_json(plan.edit_dir / "screen_text.json")
    if not isinstance(doc, dict) or not doc.get('samples'):
        return []
    step_value = 1.0 / float(doc.get("fps") or 2.0)
    pad = 10 * plan.s
    out: list[tuple[int, int, tuple[float, float, float, float]]] = []
    for am in doc['samples']:
        items_value = am.get("items") or []
        if not items_value:
            continue
        f0 = int(round(float(am["t"]) * plan.fps))
        f1 = int(round((float(am["t"]) + step_value) * plan.fps))
        for it in items_value:
            center_value = float(it.get("center_y", 0.5)) * plan.H
            half_value = max(6.0, float(it.get("height", 0.02)) * plan.H / 2)
            cx = float(it.get("center_x", 0.5)) * plan.W
            half_x = max(20.0, float(it.get("width", 0.4)) * plan.W / 2)
            out.append((f0, f1, (cx - half_x - pad, center_value - half_value - pad, cx + half_x + pad, center_value + half_value + pad)))
    # Merge consecutive samples with identical boxes to keep the list bounded.
    out.sort(key=lambda o: (round(o[2][1]), round(o[2][3]), o[0]))
    together: list[tuple[int, int, tuple[float, float, float, float]]] = []
    for f0, f1, r in out:
        if together and together[-1][2] == r and f0 - together[-1][1] <= plan.fps * 0.6:
            together[-1] = (together[-1][0], f1, r)
        else:
            together.append((f0, f1, r))
    return together


def face_bands(plan: Plan, f0: int, f1: int, core: bool = False, gap: float = 0.0,
               skip: list[tuple[int, int]] | tuple = ()) -> list[tuple[float, float]]:
    """Vertical bands taken by faces (and pip cards) during [f0, f1), minus intervals fully inside `skip` windows."""
    out = []
    for a, b, zones, colors_value in plan.zones:
        if b <= f0 or a >= f1:
            continue
        lo_f, hi_f = max(a, f0), min(b, f1)
        if any(h0 <= lo_f and hi_f <= h1 for h0, h1 in skip):
            continue
        for r in (colors_value if core else zones):
            out.append((r[1] - gap, r[3] + gap))
    for a, b, r in plan.obstacles:
        if a < f1 and f0 < b:
            out.append((r[1], r[3]))
    return out


def layout_spans(plan: Plan) -> list[tuple[int, int]]:
    """The frame spans the captions are placed in when one height does not fit the whole video: the face zone
    pieces, split where a whole background picture comes or goes (green screen)."""
    spans = [(a, b) for a, b, _, _ in plan.zones]
    if not plan.image_zones:
        return spans
    cuts = sorted({p for a, b, _ in plan.image_zones for p in (a, b)})
    out = []
    for a, b in spans:
        pts = [a] + [c for c in cuts if a < c < b] + [b]
        out += list(zip(pts, pts[1:]))
    return out


def manual_off_face(plan: Plan, layer: str, manual: float, above: float, below: float, f0: int, f1: int,
                    lo: float, hi: float, to_out) -> float:
    """Green screen: a hook or CTA height written by hand stays where it is free of the (moved) face, else it goes to
    the free height closest to it, with a warning."""
    c = pick_center([manual], above, below, face_bands(plan, f0, f1, gap=GAP_FACE * plan.H), lo, hi, manual)
    if c is None or abs(c - manual) <= 0.5:
        return manual
    plan.adjustments.append({"layer": layer, "action": "moved", "start": round(f0 / plan.fps, 3),
                             "end": round(f1 / plan.fps, 3), "because": "face (green screen)",
                             "from_center_y": to_out(manual), "center_y": to_out(c)})
    plan.warnings.append(f'{CENTER_KEYS[layer]} {to_out(manual)} would cover the face in green-screen mode after the position changed: {LABELS[layer]} moved to {to_out(c)}. Remove center_y to allow automatic placement')
    return c


def pick_center(prefs: list, above: float, below: float, blocked: list[tuple[float, float]], lo: float, hi: float,
                fallback: float | None = None) -> float | None:
    """First preference that is free as it is, else the free center closest to `fallback` (or prefs[0])."""
    prefs = [p for p in prefs if p is not None]
    for p in prefs:
        if face_layout.free_center(p, above, below, blocked, lo, hi) == p:
            return p
    target = fallback if fallback is not None else (prefs[0] if prefs else (lo + hi) / 2.0)
    return face_layout.free_center(target, above, below, blocked, lo, hi)


def layout_pieces(windows: list[dict], fps: int, f0: int, f1: int, default_cy: float) -> list[tuple[int, int, float]]:
    """Caption center per frame interval, with the rule text_render applies to `layout` windows
    (the window that starts later wins)."""
    def fr(t):
        return int(round(float(t) * fps + 1e-6))

    lay = sorted(((fr(w["start"]), fr(w["end"]), float(w["center_y"])) for w in windows), key=lambda x: x[0])
    cuts = sorted({f0, f1} | {p for a, b, _ in lay for p in (a, b) if f0 < p < f1})
    out: list[tuple[int, int, float]] = []
    for a, b in zip(cuts, cuts[1:]):
        cy = default_cy
        for l0, l1, lcy in lay:
            if l0 <= a and b <= l1:
                cy = lcy
        if out and out[-1][1] == a and out[-1][2] == cy:
            out[-1] = (out[-1][0], b, cy)
        else:
            out.append((a, b, cy))
    return out


def add_decorations(plan: Plan, mid: "Track", inserts: list[Insert]) -> None:
    """Pip cards and split seam shadows, left out while an insert later in the list covers them."""
    W, H, s = plan.W, plan.H, plan.s
    for ins in inserts:
        if ins.type == "pip":
            cx, cy_, cw, ch = ins.card  # type: ignore[misc]
            key, rect = f'pip{ins.idx}', (cx, cy_, cx + cw, cy_ + ch)
            mid.add_image(key, pip_decoration(plan, ins))
        elif ins.seam_y is not None:
            key = f'seam{ins.type}{ins.seam_y}'
            depth = max(4, int(round(34 * s)))
            rect = (0, max(0, ins.seam_y - depth), W, min(H, ins.seam_y + depth))
            if key not in mid.images:
                mid.add_image(key, seam_shadow(plan, ins))
        else:
            continue
        covered = []
        for other in inserts:
            if other.idx <= ins.idx or other.f1 <= ins.f0 or other.f0 >= ins.f1:
                continue
            ocard = other.card if other.type == "pip" else None
            if other.type != "pip" or boxes_hit(rect, (ocard[0], ocard[1], ocard[0] + ocard[2], ocard[1] + ocard[3])):
                covered.append((max(ins.f0, other.f0), min(ins.f1, other.f1)))
        z = mid.next_z()
        for a, b in subtract_windows([(ins.f0, ins.f1)], merge_windows(covered)):
            mid.add(a, b, ("img", key, 1.0, 1.0, 0.0), z=z)
        if covered:
            plan.adjustments.append({"layer": f'insert {ins.idx + 1}', "action": "decoration_hidden",
                                     "windows": [[round(a / plan.fps, 3), round(b / plan.fps, 3)] for a, b in merge_windows(covered)],
                                     "because": "an insert later in the list appears above this one"})


def build_tracks(plan: Plan, inserts: list[Insert], motions: list[Visual] | None = None) -> dict:
    s = plan.s
    W, H, fps, nf = plan.W, plan.H, plan.fps, plan.nf
    data = plan.data
    motions = list(motions or [])
    catalog_defaults = load_style_catalog().get("defaults") or {}
    result: dict = {"mid": None, "captions": None, "top": None, "sfx_events": [], "motion": []}
    sfx_on = _enabled(_section(data, "sfx"))
    lay = tr.layout_defaults((W, H))
    lo, hi = lay["top"], lay["bottom"]
    disc_hi = min(H - 8.0 * H / 1920.0, lay["bottom"] + 0.035 * H)
    g_face, g_text = GAP_FACE * H, GAP_TEXT * H
    out_dir = plan.renders / "overlays"

    def to_out(px):
        return None if px is None else int(round(px / s))

    def overlaps(a0, a1, b0, b1):
        return a0 < b1 and b0 < a1

    # ---------------- settings of each text layer
    hook = _section(data, "hook")
    hk = None
    if _enabled(hook) and str(hook.get("text") or "").strip():
        style = hook.get("style") or catalog_defaults.get("hook_style") or "headline"
        if style not in tr.HOOK_STYLES:
            raise ComposeError(f"hook style '{style}' does not exist. Options: {', '.join(tr.HOOK_STYLES)}", 2)
        h0 = plan.fr(hook.get("start", 0.0))
        h1 = plan.fr(hook.get("end"), default=min(nf, plan.fr(3.0)))
        if h1 - h0 < 2:
            plan.warnings.append('hook shorter than two frames was skipped')
        else:
            text = str(hook["text"])
            maxw = int(round(900 * s))
            fit = tr.hook_fit(text, style, (W, H), max_width=maxw)
            if fit["too_long"]:
                plan.warnings.append(f"hook too long; text became small ({fit['size']}px). Shorten the text.")
            plan.warnings.extend(tr.text_issues("hook", text, style))
            manual = _center_value(hook, "center_y", "hook.center_y")
            hk = {"style": style, "text": text, "f0": h0, "f1": h1, "fit": fit, "maxw": maxw,
                  "manual": manual * s if manual is not None else None}

    cap = _section(data, "captions")
    cp = None
    if _enabled(cap):
        cap_file = _resolve(plan.edit_dir, cap.get("file") or "captions.json", "captions.file")
        try:
            cues = load_json(cap_file) or {}
        except ValueError as e:
            raise ComposeError(f'invalid captions.json ({e})', 2)
        check_captions_cut(plan, cues, cap_file)
        cues = clamp_cue_words(cues)
        has_cues = bool(cues.get("cues") if isinstance(cues, dict) else cues)
        if not has_cues:
            plan.warnings.append('captions.json has no captions')
        style = cap.get("style") or (cues.get("style") if isinstance(cues, dict) else None)\
            or catalog_defaults.get("caption_style") or "impact"
        try:
            tr.caption_style_info(style)
        except ValueError as e:
            raise ComposeError(str(e), 2)
        if isinstance(cues, dict) and cues.get("style") and cues.get("style") != style:
            plan.warnings.append(f"captions.json was grouped for '{cues.get('style')}' while the video uses '{style}'. If it looks wrong, run captions.py --style {style}")
        font_scale = float(cap.get("font_scale", 1.0) or 1.0)
        manual = _center_value(cap, "center_y", "captions.center_y")
        hide = [(hk["f0"], hk["f1"])] if hk and cap.get("hide_during_hook", True) else []
        seams = [{"start": i.f0 / fps, "end": i.f1 / fps, "center_y": float(i.seam_y)} for i in inserts if i.seam_y is not None]
        cp = {"file": cap_file, "cues": cues, "style": style, "font_scale": font_scale, "has_cues": has_cues,
              "manual": manual * s if manual is not None else None, "hide": hide, "seams": seams, "moves": [],
              "base": [], "ext": tr.caption_extent(cues, style, (W, H), font_scale, fps)}

    disc = _section(data, "disclaimer")
    dc = None
    if _enabled(disc):
        text = str(disc.get("text") or DEFAULT_DISCLAIMER)
        d0 = plan.fr(disc.get("start", 0.0))
        d1 = plan.fr(disc.get("end"), default=nf)
        if d1 > d0:
            plan.warnings.extend(tr.text_issues("disclaimer", text))
            manual = _center_value(disc, "center_y", "disclaimer.center_y")
            bb = tr.ink_bbox(tr.render_disclaimer(text, canvas=(W, H), center_y=lay["disclaimer"]))
            dc = {"text": text, "f0": d0, "f1": d1, "manual": manual * s if manual is not None else None,
                  "ext": (lay["disclaimer"] - bb[1], bb[3] - lay["disclaimer"]) if bb else (0.0, 0.0)}

    cta = _section(data, "cta")
    ct = None
    if _enabled(cta):
        text = str(cta.get("text") or DEFAULT_CTA)
        dur = float(cta.get("duration", 2.5) or 2.5)
        c1 = nf
        c0 = plan.fr(cta.get("start")) if cta.get("start") is not None else max(0, nf - plan.fr(dur))
        if cta.get("start") is not None:
            c1 = min(nf, c0 + plan.fr(dur))
        if c1 - c0 >= 2:
            sub = cta.get("sub")
            plan.warnings.extend(tr.text_issues("cta", text) + tr.text_issues("cta_sub", str(sub or "")))
            manual = _center_value(cta, "center_y", "cta.center_y")
            pref = lay["cta"] if cp and cp["has_cues"] else lay["cta_alone"]
            bb = tr.ink_bbox(tr.render_cta(text, sub, plan.accent, canvas=(W, H), center_y=pref))
            ext = (((pref - bb[1]) * CTA_MAX_SCALE, (bb[3] - pref) * CTA_MAX_SCALE + CTA_SLIDE * s) if bb else (0.0, 0.0))
            ct = {"text": text, "sub": sub, "f0": c0, "f1": c1, "pref": pref, "ext": ext,
                  "manual": manual * s if manual is not None else None}

    for ins in inserts:
        if hk and overlaps(ins.f0, ins.f1, hk["f0"], hk["f1"]):
            plan.warnings.append(f"insert {ins.idx + 1} overlaps the hook ({fmt_time(max(ins.f0, hk['f0']) / fps)} to {fmt_time(min(ins.f1, hk['f1']) / fps)}): start the insert after the hook ends")
        if ct and overlaps(ins.f0, ins.f1, ct["f0"], ct["f1"]):
            plan.warnings.append(f"insert {ins.idx + 1} overlaps the call to action ({fmt_time(max(ins.f0, ct['f0']) / fps)} to {fmt_time(min(ins.f1, ct['f1']) / fps)}): finish the insert before the call to action")

    for m in motions:
        if hk and overlaps(m.f0, m.f1, hk["f0"], hk["f1"]):
            plan.warnings.append(f"a {m.name} overlaps the hook ({fmt_time(max(m.f0, hk['f0']) / fps)} to {fmt_time(min(m.f1, hk['f1']) / fps)}): start the animation after the hook ends")
        if ct and overlaps(m.f0, m.f1, ct["f0"], ct["f1"]):
            plan.warnings.append(f"a {m.name} overlaps the call to action ({fmt_time(max(m.f0, ct['f0']) / fps)} to {fmt_time(min(m.f1, ct['f1']) / fps)}): finish the animation before the call to action")

    def caption_windows() -> list[dict]:
        return ([{"start": a / fps, "end": b / fps, "center_y": cy} for a, b, cy in cp["base"]] + cp["seams"] + cp["moves"])

    def caption_pieces(f0, f1):
        return [(a, b, cy) for a, b, cy in layout_pieces(caption_windows(), fps, f0, f1, lay["caption"])
                if not any(h0 <= a and b <= h1 for h0, h1 in cp["hide"])]

    def caption_bands(f0, f1):
        return [(cy - cp["ext"][0] - g_text, cy + cp["ext"][1] + g_text) for _, _, cy in caption_pieces(f0, f1)]

    # ---------------- 1. captions: base height, off the faces (and, if it is cheap, off the disclaimer)
    if cp:
        above, below = cp["ext"]
        # A fixed caption height can overlap a moving face in green-screen compositions.
        # to the free height closest to it
        tv_manual = cp["manual"] is not None and plan.tv is not None
        if cp["manual"] is not None and not tv_manual:
            cp["base"] = [(0, nf, cp["manual"])]
        else:
            pref = lay["caption"] if not tv_manual else cp["manual"]
            soft = []
            if dc and not tv_manual:
                dcy = dc["manual"] if dc["manual"] is not None else lay["disclaimer"]
                soft = [(dcy - dc["ext"][0] - g_text, dcy + dc["ext"][1] + g_text)]

            def choose_caption(f0, f1, prev=None, whole=False):
                faces = face_bands(plan, f0, f1, gap=g_face, skip=cp["hide"])
                if tv_manual and face_layout.free_center(pref, above, below, faces, lo, hi) == pref:
                    return pref      # the height written by hand is free of the face here: it stays
                body = person_bands(plan, f0, f1, gap=g_face, skip=cp["hide"])
                if body:   # Place captions above the cut-out body when there is room.
                    # and off the main area of a whole background picture (between it and the head)
                    imgs = image_bands(plan, f0, f1, gap=g_face, skip=cp["hide"])
                    c = pick_center([prev, pref], above, below, faces + body + imgs, lo, hi, pref)
                    if c is not None:
                        return c
                    if imgs and whole:
                        return None      # one height for the whole video only when it is free of everything
                    if imgs:             # else below the face, before covering the picture
                        c = pick_center([prev, pref], above, below, faces + imgs, lo, hi, pref)
                        if c is not None:
                            return c
                    c = pick_center([prev, pref], above, below, faces + body, lo, hi, pref)
                    if c is not None:
                        return c
                if soft:
                    c = pick_center([prev, pref], above, below, faces + soft, lo, hi, pref)
                    if c is not None and abs(c - pref) <= SOFT_SHIFT * H:
                        return c
                return pick_center([prev, pref], above, below, faces, lo, hi, pref)

            c = choose_caption(0, nf, whole=True)
            if c is not None:
                cp["base"] = [(0, nf, c)]
            else:
                prev = None
                in_room = []
                for a, b in layout_spans(plan):
                    c = choose_caption(a, b, prev)
                    if c is None:
                        c = pref
                        in_room.append((a, b))
                    if cp["base"] and cp["base"][-1][1] == a and cp["base"][-1][2] == c:
                        cp["base"][-1] = (cp["base"][-1][0], b, c)
                    else:
                        cp["base"].append((a, b, c))
                    prev = c
                if in_room:
                    total = sum(b - a for a, b in in_room) / fps
                    plan.warnings.append(f'captions found no face-safe space at {len(in_room)} intervals ({total:.1f}s, first at {fmt_time(in_room[0][0] / fps)}): retained the default height')
            if tv_manual:
                moved = [(a, b, c) for a, b, c in cp["base"] if abs(c - cp["manual"]) > 0.5]
                for a, b, c in moved:
                    plan.adjustments.append({"layer": "captions", "action": "moved", "start": round(a / fps, 3),
                                             "end": round(b / fps, 3), "because": "face (green screen)",
                                             "from_center_y": to_out(cp["manual"]), "center_y": to_out(c)})
                if moved:
                    total = sum(b - a for a, b, _ in moved) / fps
                    plan.warnings.append(f"captions.center_y {to_out(cp['manual'])} would overlap the repositioned green-screen face at {total:.1f} seconds: captions moved to the nearest available vertical position ({to_out(moved[0][2])}). Remove center_y to allow automatic placement")

    # ---------------- 2. CTA: off the faces, and off the captions when there is room
    if ct:
        above, below = ct["ext"]
        if ct["manual"] is not None:
            ct["cy"] = ct["manual"]
            if plan.tv is not None:
                ct["cy"] = manual_off_face(plan, "cta", ct["manual"], above, below, ct["f0"], ct["f1"], lo, hi, to_out)
        else:
            faces = face_bands(plan, ct["f0"], ct["f1"], gap=g_face)
            caps = caption_bands(ct["f0"], ct["f1"]) if cp and cp["has_cues"] else []
            body = person_bands(plan, ct["f0"], ct["f1"], gap=g_face)
            c = None
            imgs = image_bands(plan, ct["f0"], ct["f1"], gap=g_face) if body else []
            if imgs:
                c = pick_center([ct["pref"]], above, below, faces + body + imgs + caps, lo, hi) if caps else None
                if c is None:
                    c = pick_center([ct["pref"]], above, below, faces + body + imgs, lo, hi)
            if body and c is None:
                c = pick_center([ct["pref"]], above, below, faces + body + caps, lo, hi) if caps else None
                if c is None:
                    c = pick_center([ct["pref"]], above, below, faces + body, lo, hi)
            if c is None:
                c = pick_center([ct["pref"]], above, below, faces + caps, lo, hi) if caps else None
            if c is None:
                c = pick_center([ct["pref"]], above, below, faces, lo, hi)
            if c is None:
                c = ct["pref"]
                plan.warnings.append('the call to action found no face-safe space; retained its default position')
            ct["cy"] = c
        ct["img"] = tr.render_cta(ct["text"], ct["sub"], plan.accent, canvas=(W, H), center_y=ct["cy"])
        ct["band"] = (ct["cy"] - above, ct["cy"] + below)

    # ---------------- 3. hook: off the faces and the CTA
    if hk:
        img0 = tr.render_hook(hk["text"], hk["style"], plan.accent, canvas=(W, H), center_y=int(round(lay["hook"])),
                              max_width=hk["maxw"])
        bb = tr.ink_bbox(img0)
        above, below = ((lay["hook"] - bb[1]) * HOOK_MAX_SCALE, (bb[3] - lay["hook"]) * HOOK_MAX_SCALE) if bb else (0.0, 0.0)
        if hk["manual"] is not None:
            cy = hk["manual"]
            if plan.tv is not None:
                cy = manual_off_face(plan, "hook", hk["manual"], above, below, hk["f0"], hk["f1"], lo, hi, to_out)
        else:
            faces = face_bands(plan, hk["f0"], hk["f1"], gap=g_face)
            highs = [(ct["band"][0] - g_text, ct["band"][1] + g_text)] if ct and overlaps(hk["f0"], hk["f1"], ct["f0"], ct["f1"]) else []
            caps = caption_bands(hk["f0"], hk["f1"]) if cp and cp["has_cues"] else []
            body = person_bands(plan, hk["f0"], hk["f1"], gap=g_face)
            cy = None
            imgs = image_bands(plan, hk["f0"], hk["f1"], gap=g_face) if body else []
            if imgs:
                cy = pick_center([lay["hook"]], above, below, faces + body + imgs + highs + caps, lo, hi) if caps else None
                if cy is None:
                    cy = pick_center([lay["hook"]], above, below, faces + body + imgs + highs, lo, hi)
            if body and cy is None:
                cy = pick_center([lay["hook"]], above, below, faces + body + highs + caps, lo, hi) if caps else None
                if cy is None:
                    cy = pick_center([lay["hook"]], above, below, faces + body + highs, lo, hi)
            if cy is None:
                cy = pick_center([lay["hook"]], above, below, faces + highs + caps, lo, hi) if caps else None
            if cy is None:
                cy = pick_center([lay["hook"]], above, below, faces + highs, lo, hi)
            if cy is None:
                cy = lay["hook"]
                plan.warnings.append('the hook found no face-safe space; retained its default position')
        hk["cy"] = cy
        hk["img"] = img0 if int(round(cy)) == int(round(lay["hook"])) else tr.render_hook(
            hk["text"], hk["style"], plan.accent, canvas=(W, H), center_y=int(round(cy)), max_width=hk["maxw"])
        hk["band"] = (cy - above, cy + below)

    # ---------------- 3b. animations: off the faces, the CTA and the hook (and the captions when it is cheap)
    if motions:
        t_mo = time.time()
        highs_mo = [(why, cfg["f0"], cfg["f1"], (cfg["band"][0] - g_text, cfg["band"][1] + g_text))
                    for why, cfg in (("callout", ct), ("hook", hk)) if cfg]
        place_motion(plan, motions, highs_mo,
                     lambda a, b: caption_bands(a, b) if cp and cp["has_cues"] else [])
        cached = sum(1 for m in motions if m.clip and m.clip.get("cached"))
        log(f"animations ready ({len(motions)}{(f', {cached} cached' if cached else '')}) in {time.time() - t_mo:.1f}s")
        motions = [m for m in motions if m.ink is not None]
        above_motion = []
        if ct:
            above_motion.append(("cta", ct["f0"], ct["f1"],
                                 grow_box(tr.ink_bbox(ct["img"]), CTA_MAX_SCALE, CTA_SLIDE * s)))
        if hk:
            above_motion.append(("hook", hk["f0"], hk["f1"], grow_box(tr.ink_bbox(hk["img"]), HOOK_MAX_SCALE)))
        resolve_motion_windows(plan, motions, above_motion)

    # ---------------- 4. captions step aside (auto) or hide (manual, or no room) for the CTA, the hook and the animations
    if cp and cp["has_cues"]:
        above, below = cp["ext"]
        highs = []
        if ct:
            highs.append(("cta", ct["f0"], ct["f1"], ct["band"]))
        if hk:
            highs.append(("hook", hk["f0"], hk["f1"], hk["band"]))
        for m in motions:
            highs += [("motion", a, b, (m.ink[1], m.ink[3])) for a, b in m.shown]
        for name, f0, f1, band in highs:
            for a, b, cyc in caption_pieces(f0, f1):
                if cyc + below + g_text <= band[0] or cyc - above - g_text >= band[1]:
                    continue
                new = None
                if cp["manual"] is None:
                    blocked = face_bands(plan, a, b, gap=g_face) + [
                        (hb[0] - g_text, hb[1] + g_text) for _, x0, x1, hb in highs if overlaps(a, b, x0, x1)]
                    body = person_bands(plan, a, b, gap=g_face)
                    if body:
                        new = pick_center([cyc], above, below, blocked + body, lo, hi)
                    if new is None:
                        new = pick_center([cyc], above, below, blocked, lo, hi)
                if new is not None:
                    cp["moves"].append({"start": a / fps, "end": b / fps, "center_y": new})
                    plan.adjustments.append({"layer": "captions", "action": "moved", "start": round(a / fps, 3),
                                             "end": round(b / fps, 3), "because": name, "from_center_y": to_out(cyc),
                                             "center_y": to_out(new)})
                else:
                    cp["hide"].append((a, b))
                    plan.adjustments.append({"layer": "captions", "action": "hidden", "start": round(a / fps, 3),
                                             "end": round(b / fps, 3), "because": name})

    # ---------------- 5. disclaimer: off the faces and every text on screen with it
    if dc:
        above, below = dc["ext"]
        spans: list[tuple[int, int, float]] = []
        if dc["manual"] is not None:
            spans = [(dc["f0"], dc["f1"], dc["manual"])]
        else:
            pref = lay["disclaimer"]
            top_anchor = lo + above + g_text

            def text_bands(a, b):
                out = []
                for cfg in (hk, ct):
                    if cfg and overlaps(a, b, cfg["f0"], cfg["f1"]):
                        out.append((cfg["band"][0] - g_text, cfg["band"][1] + g_text))
                for m in motions:
                    if any(overlaps(a, b, x0, x1) for x0, x1 in m.shown):
                        out.append((m.ink[1] - g_text, m.ink[3] + g_text))
                if cp and cp["has_cues"]:
                    out += caption_bands(a, b)
                return out

            def choose_disc(a, b, prev=None):
                txt = text_bands(a, b)
                body = person_bands(plan, a, b, gap=g_face)
                c = None
                if body:
                    c = pick_center([prev, pref, top_anchor], above, below,
                                    face_bands(plan, a, b, gap=g_face) + body + txt, lo, disc_hi, pref)
                if c is None:
                    c = pick_center([prev, pref, top_anchor], above, below, face_bands(plan, a, b, gap=g_face) + txt,
                                    lo, disc_hi, pref)
                if c is None:   # small print may sit on the chin or the forehead, never on the eyes or the mouth
                    c = pick_center([prev, pref, top_anchor], above, below,
                                    face_bands(plan, a, b, core=True, gap=3 * g_face) + txt, lo, disc_hi, pref)
                return c

            c = choose_disc(dc["f0"], dc["f1"])
            if c is not None:
                spans = [(dc["f0"], dc["f1"], c)]
            else:
                cuts = {dc["f0"], dc["f1"]}
                for a, b, _, _ in plan.zones:
                    cuts |= {a, b}
                for cfg in (hk, ct):
                    if cfg:
                        cuts |= {cfg["f0"], cfg["f1"]}
                for m in motions:
                    for x0, x1 in m.shown:
                        cuts |= {x0, x1}
                if cp:
                    for a, b, _ in layout_pieces(caption_windows(), fps, 0, nf, lay["caption"]):
                        cuts |= {a, b}
                    for a, b in cp["hide"]:
                        cuts |= {a, b}
                points = sorted(f for f in cuts if dc["f0"] <= f <= dc["f1"])
                prev = None
                for a, b in zip(points, points[1:]):
                    if b <= a:
                        continue
                    c = choose_disc(a, b, prev)
                    if c is None:
                        plan.adjustments.append({"layer": "disclaimer", "action": "hidden", "start": round(a / fps, 3),
                                                 "end": round(b / fps, 3), "because": 'no free space'})
                        prev = None
                        continue
                    if spans and spans[-1][1] == a and spans[-1][2] == c:
                        spans[-1] = (spans[-1][0], b, c)
                    else:
                        spans.append((a, b, c))
                    prev = c
            for a, b, c in spans:
                if abs(c - pref) > 0.5:
                    plan.adjustments.append({"layer": "disclaimer", "action": "moved", "start": round(a / fps, 3),
                                             "end": round(b / fps, 3), "because": "face or other text",
                                             "from_center_y": to_out(pref), "center_y": to_out(c)})
        dc["imgs"] = {}
        for _, _, c in spans:
            key = f'disc{int(round(c))}'
            if key not in dc["imgs"]:
                dc["imgs"][key] = (c, tr.render_disclaimer(dc["text"], canvas=(W, H), center_y=c))
        dc["spans"] = [(a, b, f'disc{int(round(c))}') for a, b, c in spans]

    # ---------------- 6. caption track at the chosen heights
    states: list[dict] = []
    if cp:
        layout = caption_windows()
        hide_s = [[a / fps, b / fps] for a, b in merge_windows(cp["hide"])]
        cap_dir = plan.renders / f"captions_{cp['style']}_{W}x{H}"
        states = tr.render_caption_track(cp["cues"], cp["style"], plan.accent, str(cap_dir), canvas=(W, H), fps=fps,
                                         layout=layout or None, hide=hide_s or None, font_scale=cp["font_scale"])
        states = [st for st in states if st["start"] < plan.duration]

    # ---------------- 7. what is really drawn: overlapping texts resolved by priority
    drawn: dict[str, list[tuple]] = {"cta": [], "hook": [], "motion": [], "captions": [], "disclaimer": []}
    if ct:
        drawn["cta"] = [(ct["f0"], ct["f1"], grow_box(tr.ink_bbox(ct["img"]), CTA_MAX_SCALE, CTA_SLIDE * s), None)]
    if hk:
        drawn["hook"] = [(hk["f0"], hk["f1"], grow_box(tr.ink_bbox(hk["img"]), HOOK_MAX_SCALE), None)]
    drawn["motion"] = [(a, b, m.ink, m.idx) for m in motions for a, b in m.shown]   # resolved in 3b
    if cp:
        drawn["captions"] = [(plan.fr(st["start"]), plan.fr(st["end"]), tuple(st["bbox"]) if st.get("bbox") else None,
                              st["file"]) for st in states]
    if dc:
        boxes = {k: tr.ink_bbox(img) for k, (_, img) in dc["imgs"].items()}
        drawn["disclaimer"] = [(a, b, boxes[k], k) for a, b, k in dc["spans"]]
    for i, low in enumerate(PRIORITY):
        if low == "motion":
            continue
        hits: dict[str, list[tuple[int, int]]] = {}
        for high in PRIORITY[:i]:
            for h0, h1, hbox, _ in drawn[high]:
                for l0, l1, lbox, _ in drawn[low]:
                    a, b = max(h0, l0), min(h1, l1)
                    if b > a and boxes_hit(hbox, lbox):
                        hits.setdefault(high, []).append((a, b))
        if hits:
            windows = merge_windows([w for ws in hits.values() for w in ws])
            min_f = max(1, int(round(MIN_PIECE.get(low, 0.0) * fps)))
            before = sum(span[1] - span[0] for span in drawn[low])
            kept = []
            for span in drawn[low]:
                for piece in subtract_windows([span], windows):
                    if (piece[0], piece[1]) != (span[0], span[1]) and piece[1] - piece[0] < min_f:
                        # a sliver left between two hides would blink on screen: hide it with its neighbour
                        high = next((h for h, ws in hits.items() for a, b in ws if b == piece[0] or a == piece[1]),
                                    next(iter(hits)))
                        hits[high].append((piece[0], piece[1]))
                    else:
                        kept.append(piece)
            drawn[low] = kept
            for high, ws in hits.items():
                for a, b in merge_windows(ws):
                    plan.adjustments.append({"layer": low, "action": "hidden", "start": round(a / fps, 3),
                                             "end": round(b / fps, 3), "because": high})
            hidden_share = 1.0 - sum(span[1] - span[0] for span in kept) / max(1, before)
            if low == "disclaimer" and hidden_share > DISCLAIMER_HIDDEN_WARN:
                names = [OF_LABELS[h] for h in PRIORITY if h in hits]
                behind = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
                plan.warnings.append(f'the disclaimer was hidden for {round(hidden_share * 100)}% of its duration to avoid overlapping {behind}. Set disclaimer.center_y to "auto" or choose another height')

    # ---------------- 8. text still over a face (manual heights, or no free spot)
    face_report(plan, drawn, {"cta": ct, "hook": hk, "captions": cp, "disclaimer": dc}, lay, disc_hi)
    motion_face_report(plan, motions)

    # ---------------- 9. tracks
    for m in motions:
        if not m.shown:
            plan.warnings.append(f'a {m.name} remained hidden throughout and was excluded')
            continue
        result["motion"].append((m.clip, m.f0, m.shown))
        m.cues = motion_cues(m)
        for cue in m.cues:
            if m.sfx and sfx_on and any(a <= cue["frame"] < b for a, b in m.shown):
                result["sfx_events"].append(cue)
    mid = Track(plan, "mid")
    add_decorations(plan, mid, inserts)
    if hk:
        mid.add_image("hook", hk["img"])
        z = mid.next_z()
        shown = drawn["hook"]
        h0, h1 = hk["f0"], hk["f1"]
        n = h1 - h0
        animate = n >= len(HOOK_IN) + len(HOOK_OUT) + 2
        for j in range(n):
            f = h0 + j
            if not any(a <= f < b for a, b, _, _ in shown):
                continue
            sc, al = 1.0, 1.0
            if animate and j < len(HOOK_IN):
                sc, al = HOOK_IN[j]
            elif animate and j >= n - len(HOOK_OUT):
                al = HOOK_OUT[j - (n - len(HOOK_OUT))]
            mid.add(f, f + 1, ("img", "hook", sc, al, 0.0), z=z)
        plan.layers.append({"type": "hook", "style": hk["style"], "text": hk["text"], "start": h0 / fps, "end": h1 / fps,
                            "frames": [h0, h1], "lines": hk["fit"]["lines"], "center_y": to_out(hk["cy"]),
                            "auto": hk["manual"] is None})
        snd = hook.get("sfx", "pop")
        if sfx_on and snd:
            result["sfx_events"].append({"name": str(snd), "frame": h0, "lead_frames": 0, "why": "hook"})
    result["mid"] = mid.build(out_dir)

    if cp:
        by_file = {}
        final_states = []
        for a, b, bbox, file in drawn["captions"]:
            final_states.append({"file": file, "start": round(a / fps, 6), "end": round(b / fps, 6),
                                 "bbox": list(bbox) if bbox else None})
            by_file[file] = True
        cap_dir = plan.renders / f"captions_{cp['style']}_{W}x{H}"
        ff = tr.write_ffconcat(final_states, str(cap_dir / "captions.ffconcat"), total_duration=plan.duration,
                               blank_png=str(cap_dir / "blank.png"), fps=fps)
        windows = merge_windows([(plan.fr(st["start"]), plan.fr(st["end"])) for st in final_states])
        result["captions"] = (ff, final_states, windows)
        plan.layers.append({"type": "captions", "style": cp["style"], "file": plan.rel(cp["file"]),
                            "words_from": cp["cues"].get("words_from") if isinstance(cp["cues"], dict) else None,
                            "states": len(final_states), "png_files": len(by_file),
                            "start": round(final_states[0]["start"], 3) if final_states else None,
                            "end": round(final_states[-1]["end"], 3) if final_states else None,
                            "hide": [[round(a / fps, 3), round(b / fps, 3)] for a, b in merge_windows(cp["hide"])],
                            "layout": [{"start": round(w["start"], 3), "end": round(w["end"], 3),
                                        "center_y": to_out(w["center_y"])} for w in caption_windows()],
                            "auto": cp["manual"] is None, "font_scale": cp["font_scale"]})

    top = Track(plan, "top")
    if dc and dc["spans"]:
        for key, (_, img) in dc["imgs"].items():
            top.add_image(key, img)
        z = top.next_z()
        d0 = dc["f0"]
        for a, b, _, key in drawn["disclaimer"]:
            fade = 4 if (a == d0 and d0 > 0) else 0
            for j in range(min(fade, b - a)):
                top.add(a + j, a + j + 1, ("img", key, 1.0, round((j + 1) / (fade + 1), 3), 0.0), z=z)
            top.add(a + min(fade, b - a), b, ("img", key, 1.0, 1.0, 0.0), z=z)
        centers = sorted({to_out(c) for c, _ in dc["imgs"].values()})
        plan.layers.append({"type": "disclaimer", "text": dc["text"], "start": dc["f0"] / fps, "end": dc["f1"] / fps,
                            "frames": [dc["f0"], dc["f1"]], "center_y": centers[0] if len(centers) == 1 else centers,
                            "auto": dc["manual"] is None,
                            "shown": [[round(a / fps, 3), round(b / fps, 3)] for a, b in
                                      merge_windows([(a, b) for a, b, _, _ in drawn["disclaimer"]])]})
    if ct:
        top.add_image("cta", ct["img"])
        z = top.next_z()
        c0, c1 = ct["f0"], ct["f1"]
        n_in = min(len(CTA_IN), c1 - c0)
        for j in range(n_in):
            sc, al, dy = CTA_IN[j]
            top.add(c0 + j, c0 + j + 1, ("img", "cta", sc, al, round(dy * s, 1)), z=z)
        # gentle pulse after the entrance (quantized so PNGs repeat)
        for f in range(c0 + n_in, c1):
            tt = (f - c0 - n_in) / fps
            sc = 1.0 + 0.028 * (0.5 - 0.5 * math.cos(2 * math.pi * tt / 1.1))
            sc = round(round(sc / 0.004) * 0.004, 3)
            top.add(f, f + 1, ("img", "cta", sc, 1.0, 0.0), z=z)
        plan.layers.append({"type": "cta", "text": ct["text"], "sub": ct["sub"], "start": c0 / fps, "end": c1 / fps,
                            "frames": [c0, c1], "center_y": to_out(ct["cy"]), "auto": ct["manual"] is None})
        snd = cta.get("sfx")
        if sfx_on and snd:
            result["sfx_events"].append({"name": str(snd), "frame": c0, "lead_frames": 0, "why": "callout"})
    for k, trn in enumerate(data.get("transitions") or []):
        if not isinstance(trn, dict):
            continue
        if (trn.get("type") or "flash") != "flash":
            plan.warnings.append(f"transition '{trn.get('type')}' is unsupported; only 'flash' is available. Skipped")
            continue
        try:
            f = plan.fr(float(trn["at"]))
        except (KeyError, TypeError, ValueError):
            plan.warnings.append(f"transitions[{k}] has no valid 'at' and was skipped")
            continue
        z = top.next_z()
        for off, al in FLASH:
            top.add(f + off, f + off + 1, ("flash", al), z=z)
        plan.layers.append({"type": "flash", "at": f / fps, "frame": f,
                            "frames": [max(0, f - 1), min(nf, f + 4)]})
        snd = trn.get("sfx", "click")
        if sfx_on and snd:
            result["sfx_events"].append({"name": str(snd), "frame": f, "lead_frames": 0, "why": "flash"})
    result["top"] = top.build(out_dir)
    result['overlayMasks'] = [{'start': a / fps, 'end': b / fps,
                               'box': [max(0, box[0] - 4) / W, max(0, box[1] - 4) / H,
                                       min(W, box[2] + 4) / W, min(H, box[3] + 4) / H]}
                              for spans in drawn.values() for a, b, box, _ in spans if box]
    return result


def face_report(plan: Plan, drawn: dict, cfgs: dict, lay: dict, disc_hi: float) -> None:
    """List texts still covering the eyes, nose or mouth, with a center_y that would free them."""
    fps, H = plan.fps, plan.H
    g_face = GAP_FACE * H
    for layer in TEXT_LAYERS:
        hits = []
        for f0, f1, box, _ in drawn[layer]:
            for a, b, _, colors_value in plan.zones:
                lo_f, hi_f = max(f0, a), min(f1, b)
                if hi_f <= lo_f:
                    continue
                share = max([cover_share(box, core) for core in colors_value] or [0.0])
                if share > FACE_COVER_WARN:
                    hits.append((lo_f, hi_f, share))
        if not hits:
            continue
        hits.sort()
        merged: list[list] = []
        for a, b, share in hits:
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
                merged[-1][2] = max(merged[-1][2], share)
            else:
                merged.append([a, b, share])
        cfg = cfgs.get(layer) or {}
        worst = max(merged, key=lambda m: m[2])
        pref = {"cta": cfg.get("pref"), "hook": lay["hook"], "captions": lay["caption"], "disclaimer": lay["disclaimer"]}[layer]
        ext = cfg.get("ext")
        if layer == "hook" and cfg.get("band") and cfg.get("cy") is not None:
            ext = (cfg["cy"] - cfg["band"][0], cfg["band"][1] - cfg["cy"])
        suggest = None
        if ext:
            hi = disc_hi if layer == "disclaimer" else lay["bottom"]
            c = pick_center([pref], ext[0], ext[1], face_bands(plan, worst[0], worst[1], gap=g_face), lay["top"], hi)
            suggest = int(round(c / plan.s)) if c is not None else None
        for a, b, share in merged[:20]:
            plan.face_checks.append({"layer": layer, "start": round(a / fps, 3), "end": round(b / fps, 3),
                                     "cover": round(share, 3), "suggest_center_y": suggest})
        where = (f'at {fmt_time(worst[0] / fps)}' if worst[1] - worst[0] <= fps
                 else f'between {fmt_time(worst[0] / fps)} and {fmt_time(worst[1] / fps)}')
        on_seam = layer == "captions" and any(
            plan.fr(sm["start"]) < worst[1] and worst[0] < plan.fr(sm["end"]) for sm in cfg.get("seams") or [])
        if on_seam:
            tip = ("Captions overlap the split boundary: switch split_top and split_bottom or adjust this insert's split")
        elif cfg.get("manual") is None:
            tip = ('No face-safe space remains with the other text: shorten or remove a text element in this interval')
        elif suggest is not None:
            tip = f'Use {CENTER_KEYS[layer]} near {suggest} (or "auto")'
        else:
            tip = 'No face-safe space remains: shorten text or use a split screen'
        plan.warnings.append(f'{LABELS[layer]} covers the face {where} ({round(worst[2] * 100)}%). {tip}')


def motion_cues(m: Visual) -> list[dict]:
    'Map template sound cues onto output frames in the sfx.events schema.'
    out = []
    for cue in m.clip.get("sfx") or []:
        ev = {"name": str(cue["name"]), "frame": m.f0 + int(cue["frame"]), "lead_frames": 0,
              "why": f'animation {m.idx}', "motion": m.idx, "volume": float(cue.get("volume", 1.0))}
        if cue.get("length"):
            ev["length"] = float(cue["length"])
        out.append(ev)
    return out


def resolve_motion_windows(plan: Plan, motions: list[Visual], highs: list[tuple]) -> None:
    """m.shown: where each animation really shows. The CTA and the hook (above it in PRIORITY) hide it where their
    boxes touch it, and a leftover shorter than MIN_PIECE["motion"] is hidden with its neighbour. Done before the
    captions and the disclaimer are placed, so they only give way to what is really on screen."""
    fps = plan.fps
    min_f = max(1, int(round(MIN_PIECE["motion"] * fps)))
    for m in motions:
        hits: dict[str, list[tuple[int, int]]] = {}
        for name, h0, h1, hbox in highs:
            a, b = max(h0, m.f0), min(h1, m.f1)
            if b > a and boxes_hit(hbox, m.ink):
                hits.setdefault(name, []).append((a, b))
        if not hits:
            m.shown = [(m.f0, m.f1)]
            continue
        kept = []
        for p0, p1 in subtract_windows([(m.f0, m.f1)], merge_windows([w for ws in hits.values() for w in ws])):
            if p1 - p0 < min_f:
                # a sliver left between two hides would blink on screen: hide it with its neighbour
                high = next((h for h, ws in hits.items() for a, b in ws if b == p0 or a == p1), next(iter(hits)))
                hits[high].append((p0, p1))
            else:
                kept.append((p0, p1))
        m.shown = kept
        for high in PRIORITY:
            for a, b in merge_windows(hits.get(high, [])):
                plan.adjustments.append({"layer": f'motion {m.idx}', "action": "hidden", "start": round(a / fps, 3),
                                         "end": round(b / fps, 3), "because": high})
                plan.warnings.append(f'a {m.name} was hidden from {fmt_time(a / fps)} to {fmt_time(b / fps)} to avoid overlapping {OF_LABELS[high]}. Change start, end, or region')


def motion_face_report(plan: Plan, motions: list[Visual]) -> None:
    """An animation still over the eyes, nose or mouth goes to face_checks and to a warning."""
    fps = plan.fps
    for m in motions:
        hits = []
        for a, b, _, colors_value in plan.zones:
            for w0, w1 in m.shown:
                lo_f, hi_f = max(a, w0), min(b, w1)
                if hi_f <= lo_f:
                    continue
                share = max([cover_share(m.ink, core) for core in colors_value] or [0.0])
                if share > FACE_COVER_WARN:
                    hits.append((lo_f, hi_f, share))
        if not hits:
            continue
        hits.sort()
        worst = max(hits, key=lambda h: h[2])
        for a, b, share in hits[:20]:
            plan.face_checks.append({"layer": f'motion {m.idx}', "start": round(a / fps, 3), "end": round(b / fps, 3),
                                     "cover": round(share, 3), "suggest_center_y": None,
                                     "suggest_region": "auto" if not m.auto else None})
        if not m.item.template.movable:
            tip = 'This template stays at its target. Change its timing or use an interval without the face there'
        elif m.auto:
            tip = 'No face-safe space remains: use fewer items, another region, or another interval'
        else:
            tip = 'Use "region": "auto" for face avoidance'
        plan.warnings.append(f'a {m.name} covers the face between {fmt_time(hits[0][0] / fps)} and {fmt_time(hits[-1][1] / fps)} ({round(worst[2] * 100)}%). {tip}')


def motion_layers(plan: Plan, motions: list[Visual]) -> None:
    """layers[type=motion] of the report."""
    fps = plan.fps
    ko = min(plan.full_w, plan.full_h) / 1080.0
    for m in motions:
        if m.clip is None:
            continue
        item = m.item
        asked = m.region_asked
        layer = {"type": "motion", "index": m.idx, "template": m.template, "label": item.template.label,
                 "start": m.f0 / fps, "end": m.f1 / fps, "frames": [m.f0, m.f1],
                 "shown": [[round(a / fps, 3), round(b / fps, 3)] for a, b in m.shown],
                 "region_asked": asked if asked is not None else "auto", "auto": m.auto,
                 "region": [int(round(v * ko)) for v in item.region], "moved_by": int(round(item.moved_by * ko)),
                 "rect": [m.clip["x"], m.clip["y"], m.clip["w"], m.clip["h"]],
                 "ink": list(m.ink) if m.ink else None,
                 "clip": plan.rel(Path(m.clip["file"])), "cached": bool(m.clip.get("cached")),
                 "params": item.params, "sfx_on": m.sfx,
                 "sfx": [{"name": c["name"], "frame": c["frame"], "time": round(c["frame"] / fps, 3),
                          "volume": c["volume"]} for c in m.cues]}
        plan.layers.append(layer)


# ============================================================== video graph

def run_video(plan: Plan, inserts: list[Insert], tracks: dict, cam: str | None, tmp_video: Path,
              base_clip: Path | None = None) -> None:
    """base_clip: the green screen clip, already on the output grid and size, used in place of the base."""
    W, H, fps, nf = plan.W, plan.H, plan.fps, plan.nf
    from media_policy import encoder_args, frame_tags, preserve_scale
    active_tracks = bool(tracks.get('motion')) or any(
        track and track[0] and track[1] for name, track in tracks.items() if name in ('mid', 'captions', 'top'))
    # Copying keeps the base codec, so it is valid only when that is the confirmed codec (or none was confirmed).
    wanted = {'libx264': 'h264', 'libx265': 'hevc'}.get(plan.media_plan.get('encoder'))
    if not inserts and not active_tracks and not cam and base_clip is None and (W, H) == (plan.base_w, plan.base_h) and abs(fps - plan.base_fps) < 1e-6 \
            and (wanted is None or probe_video(plan.base).get('codec') == wanted):
        ffmpeg_cmd(['-v', 'error', '-y', '-i', plan.base, '-map', '0:v:0', '-c:v', 'copy', '-an',
                    '-frames:v', nf, '-movflags', '+faststart', str(tmp_video)], 'preserve unchanged video essence')
        return
    if plan.media_plan.get('bit_depth', 8) > 8 or plan.media_plan.get('is_hdr') or '420' not in plan.media_plan.get('pix_fmt', ''):
        raise ComposeError('This FFmpeg effects path is validated only for 8-bit SDR 4:2:0; preserve the cut-only master or confirm a supported conversion', 2)
    if (inserts or tracks.get('motion')) and (plan.media_plan.get('color_space') != 'bt709' or plan.media_plan.get('color_range') != 'tv'):
        raise ComposeError('Video inserts and motion require a validated common composition space; do not reinterpret source color', 2)
    time_base = f'{Fraction(plan.fps_str).denominator}/{Fraction(plan.fps_str).numerator}'
    inputs: list[str] = ["-i", str(base_clip or plan.base)]
    chains: list[str] = []
    n_in = 1

    pre = []
    if base_clip is None and abs(plan.base_fps - fps) > 0.01:
        pre.append(f'fps={fps}')
    if base_clip is None and (plan.base_w, plan.base_h) != (W, H):
        pre.append(f'scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic{preserve_scale(plan.media_plan)},crop={W}:{H},setsar=1')
    else:
        pre.append(f'scale=iw:ih{preserve_scale(plan.media_plan)}')
    pre += [f'settb={time_base}', "setpts=N", "format=yuv420p"]
    if cam:
        pre.append(cam)
    splits = [i for i in inserts if i.speaker is not None]
    if splits:
        labels = "[cur0]" + "".join(f'[spk{i.idx}]' for i in splits)
        chains.append(f"[0:v]{','.join(pre)},split={len(splits) + 1}{labels}")
    else:
        chains.append(f"[0:v]{','.join(pre)}[cur0]")
    cur = "cur0"
    step = 0

    def nxt() -> str:
        nonlocal step
        step += 1
        return f'cur{step}'

    for ins in inserts:
        en = f'between(n,{ins.f0},{ins.f1 - 1})'
        if ins.speaker is not None:
            _, sy, sw, sh = ins.speaker
            lab = nxt()
            pieces = ins.speaker_pieces or [(ins.f0, ins.f1, ins.speaker_src_y)]
            ypos = str(pieces[-1][2])
            for p0, p1, y in reversed(pieces[:-1]):      # crop evaluates y on every frame (n = frame of the base)
                ypos = f'if(lt(n,{p1}),{y},{ypos})'
            chains.append(f"[spk{ins.idx}]crop={sw}:{sh}:0:'{ypos}'[spkc{ins.idx}]")
            chains.append(f"[{cur}][spkc{ins.idx}]overlay=x=0:y={sy}:enable='{en}'[{lab}]")
            cur = lab
        inputs += ["-i", str(ins.clip)]
        x, y = ins.content[0], ins.content[1]
        lab = nxt()
        chains.append(f'[{n_in}:v]settb={time_base},setpts=N+{ins.f0},format=yuv420p[ins{ins.idx}]')
        chains.append(f"[{cur}][ins{ins.idx}]overlay=x={x}:y={y}:eof_action=pass:enable='{en}'[{lab}]")
        cur = lab
        n_in += 1

    motion_tracks = tracks.get("motion") or []
    if motion_tracks:
        from visuals import render as MR
        for clip, f0, windows in motion_tracks:
            inputs += ["-i", str(clip["file"])]
            lab = nxt()
            chains.extend(MR.overlay_filters(clip, n_in, cur, lab, f0, windows))
            cur = lab
            n_in += 1

    for name in ("mid", "captions", "top"):
        track = tracks.get(name)
        if not track or not track[0] or not track[1]:
            continue
        ff, _, windows = track
        inputs += ["-f", "concat", "-safe", "0", "-i", str(ff)]
        en = enable_expr(windows, nf)
        lab = nxt()
        matrix = plan.media_plan.get('color_space') or 'bt709'
        output_range = plan.media_plan.get('color_range') or 'tv'
        chains.append(f'[{n_in}:v]format=rgba,scale=in_range=pc:out_color_matrix={matrix}:out_range={output_range},format=yuva420p[t{name}]')
        opt = f":enable='{en}'" if en else ""
        chains.append(f'[{cur}][t{name}]overlay=0:0:eof_action=pass:format=yuv420{opt}[{lab}]')
        cur = lab
        n_in += 1
    tags = frame_tags(plan.media_plan)
    chains.append(f'[{cur}]format=yuv420p' + (',' + tags if tags else '') + '[vout]')

    script = plan.renders / "final_graph.txt"
    script.write_text(";\n".join(chains) + "\n", encoding="utf-8")
    crf, preset = ("26", "veryfast") if plan.preview else ("18", "medium")
    args = ["-v", "error", "-y", *inputs, *graph_args(script), "-map", "[vout]",
            "-frames:v", str(nf), "-fps_mode", "cfr", "-r", plan.fps_str,
            *encoder_args(plan.media_plan, quality=crf), '-preset', preset,
            "-an", "-f", "mp4", str(tmp_video)]
    ffmpeg_cmd(args, 'compose the final video')


# ============================================================== audio

def loudnorm_measure(path: Path, extra: list[str] | None = None, i=-14.0, tp=-1.0, lra=11.0) -> dict | None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", *(extra or []), "-i", str(path),
                           "-vn", "-af", f'loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json', "-f", "null", "-"],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    if proc.returncode or not m:
        return None
    try:
        d = json.loads(m.group(0))
        vals = {k: float(d[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError):
        return None
    if not all(math.isfinite(v) for v in vals.values()):
        return None
    return vals


def _sound_share_kept(path: Path, n_keep: int) -> tuple[float, float] | None:
    """(share of the sound's energy inside its first n_keep samples at SR, its length in seconds).

    None when the file cannot be read or is silent."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    x = np.frombuffer(proc.stdout[: len(proc.stdout) // 4 * 4], dtype="<f4").astype(np.float64)
    whole = float(np.sum(x * x))
    if whole <= 0:
        return None
    return float(np.sum(x[: max(0, n_keep)] ** 2)) / whole, len(x) / SR


SFX_WHY_LABELS = {'hook': "of the hook", "callout": "of the call to action", "flash": "of the flash"}


def _anchor_warning(e: dict, anchor: float, late: int, fps: int) -> str:
    """Warning for an anchored effect that had to start at 0 and so hits after its frame."""
    if e["why"] == "effect":
        label = f"sfx.events[{e['event']}] ('{e['name']}' at {fmt_time(e['at'])})"
    else:
        of = SFX_WHY_LABELS.get(e["why"]) or ("of the " + e["why"])
        label = f"effect '{e['name']}' {of} at {fmt_time(e['frame'] / fps)}"
    return (f'{label}: its peak comes {anchor:.2f} seconds after the sound starts, so the sound would begin before the video. It starts at zero and its peak arrives {late / SR:.2f} s late')


def run_audio(plan: Plan, inserts: list[Insert], sfx_events: list[dict], out_wav: Path, report: dict,
              voice: Path | None = None) -> None:
    """voice: a cleaned voice track (audio.denoise) used instead of the base's audio; None = the base's audio."""
    data = plan.data
    fps, total = plan.fps, round(plan.nf * SR / plan.fps)
    loud = _section(data, "loudness")
    target_i = float(loud.get("i", -14))
    target_tp = float(loud.get("tp", -1.0))
    target_lra = float(loud.get("lra", 11))
    work = plan.renders / "audio"
    work.mkdir(parents=True, exist_ok=True)
    voice_in = voice if voice is not None else plan.base

    voice_i = None
    if plan.has_audio:
        m = loudnorm_measure(voice_in)
        voice_i = m["input_i"] if m else None
    ref_i = voice_i if voice_i is not None else target_i

    inputs: list[str] = []
    chains: list[str] = []
    if plan.has_audio:
        inputs += ["-i", str(voice_in)]
        voice_src = "[0:a]"
    else:
        inputs += ["-f", "lavfi", "-t", f'{plan.duration:.6f}', "-i", f'anullsrc=r={SR}:cl=stereo']
        voice_src = "[0:a]"
    n_in = 1
    music = _section(data, "music")
    music_on = _enabled(music) and music.get("file")
    duck = bool(music.get("duck", True)) if music_on else False
    chains.append(f'{voice_src}aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,apad=whole_len={total},atrim=end_sample={total},asetpts=N/SR/TB[voice]')
    mix_inputs = []
    if duck:
        chains.append("[voice]asplit=2[vmix][vkey]")
        mix_inputs.append("[vmix]")
    else:
        mix_inputs.append("[voice]")

    # SFX: gain calibrated for a voice at the target loudness
    sfx_sec = _section(data, "sfx")
    placed = []
    if _enabled(sfx_sec):
        vol = float(sfx_sec.get("volume", 0.6) or 0.0)
        adj_db = max(-10.0, min(10.0, ref_i - target_i))
        gain = vol * SFX_CALIBRATION * (10 ** (adj_db / 20.0))
        events = list(sfx_events)
        for ins in inserts:
            if ins.sfx:
                events.append({"name": ins.sfx, "frame": ins.f0, "lead_frames": 2, "why": f'insert {ins.idx + 1}'})
        names = {e["name"] for e in events}
        custom = {}
        for nm in names:
            p = Path(nm)
            if p.suffix.lower() in SFX_FILE_EXT:
                custom[nm] = _resolve(plan.edit_dir, nm, "sfx")
        # a category of the effects library ("transition", "humor") or one of its takes ("humor-012"): a category
        # hands out a different take to every event, in a fixed order per edit. A category name wins over a
        # built-in alias that happens to match it ("impact" is also an alias of metal_hit)
        import effects
        library = effects.resolve([nm for nm in names if nm not in custom], seed=plan.edit_dir.name)
        builtin = make_sfx.builtin_sfx([nm for nm in names if nm not in custom and nm not in library])
        manifest = make_sfx.load_manifest()
        handed: dict[str, int] = {}
        for e in sorted(events, key=lambda e: e["frame"]):
            known = builtin.get(e["name"])
            if known is None and e["name"] in library:
                pool = library[e["name"]]["takes"]
                k = handed.get(e["name"], 0)
                handed[e["name"]] = k + 1
                known = pool[k % len(pool)]
            path = custom.get(e["name"]) or (known["path"] if known else None)
            if path is None:
                if make_sfx.resolve_name(e["name"], manifest) is not None or e["name"] in make_sfx.NAMES:
                    plan.warnings.append(f"sound-effect file '{e['name']}' is missing from the skill's assets/sfx folder and was not played. Reinstall the skill")
                else:
                    plan.warnings.append(f"sound effect '{e['name']}' does not exist; use {', '.join(make_sfx.NAMES)}, an effect-library category ({', '.join(effects.CATEGORIES)}), a library sound ID, or a local file")
                continue
            ev_gain = gain * float(e.get("volume", 1.0))      # sfx.events can carry their own multiplier
            anchor = e.get("anchor_s")
            if anchor is None and known:
                anchor = known["anchor_s"]
            late = 0
            if anchor is None:
                # a sound file with no anchor: its t=0 on the event frame (lead_frames early), as always
                start = max(0, round((e["frame"] - e["lead_frames"]) * SR / fps))
            else:
                # the anchor (the moment the sound hits full strength) lands on the event frame
                start = round(e["frame"] * SR / fps) - int(round(anchor * SR))
                if start < 0:
                    late, start = -start, 0
            if start >= total or ev_gain <= 0:
                continue
            kept = _sound_share_kept(path, total - start) if "event" in e else None
            # an anchor_s past the end of the sound gets its own warning below, not a "late" one
            past_end = kept is not None and e.get("anchor_s") is not None and e["anchor_s"] > kept[1]
            if late and anchor >= (e["frame"] + 1) / fps and not past_end:
                plan.warnings.append(_anchor_warning(e, anchor, late, fps))
            inputs += ["-i", str(path)]
            lab = f'sfx{n_in}'
            trim = ""
            if e.get("length"):
                # a cue that lasts as long as its animation (typing): cut the sound after its strong point + length
                cut = (anchor or 0.0) + float(e["length"])
                trim = f'atrim=end={cut:.4f},afade=t=out:st={max(0.0, cut - SFX_TRIM_FADE):.4f}:d={SFX_TRIM_FADE:.3f},'
            chains.append(f'[{n_in}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,{trim}volume={ev_gain:.4f},adelay=delays={start}S:all=1[{lab}]')
            mix_inputs.append(f'[{lab}]')
            entry = {"name": e["name"], "why": e["why"], "sample": start, "time": round(start / SR, 4),
                     "file": str(path)}
            if known and known.get("id") and known["id"] != e["name"]:
                entry["effect"] = known["id"]           # the sfx.json ID it played
            for key in ("at", "volume", "event", "motion", "length"):
                if key in e:
                    entry[key] = e[key]
            if anchor:
                entry["anchor_s"] = anchor
            if late:
                entry["late_s"] = round(late / SR, 4)
            if "event" in e:
                if kept is not None and kept[0] < SFX_EVENT_MIN_KEPT:
                    plan.warnings.append(f"""sfx.events[{e['event']}] ('{e['name']}' at {fmt_time(e['at'])}) is truncated by the video ending; only {(total - start) / SR:.2f} s of the {kept[1]:.2f} seconds of sound. Move "at" earlier to hear the complete effect""")
                if past_end:
                    plan.warnings.append(f"sfx.events[{e['event']}].anchor_s {e['anchor_s']:g} exceeds the sound ending ({kept[1]:.2f} s): the entire sound played before {fmt_time(e['at'])}")
            placed.append(entry)
            n_in += 1
    report["sfx"] = placed

    if music_on:
        mpath = _resolve(plan.edit_dir, music.get("file"), "music.file")
        mm = loudnorm_measure(mpath, extra=["-t", f'{plan.duration + 1:.3f}'])
        vol_db = float(music.get("volume_db", MUSIC_VOLUME_DB))
        if mm:
            gain_db = (ref_i - mm["input_i"]) + vol_db
        else:
            gain_db = vol_db
            plan.warnings.append('could not measure music loudness; using volume_db directly')
        gain_db = max(-60.0, min(12.0, gain_db))
        fade = max(0.0, min(plan.duration, float(music.get("fade_out", 1.5) or 0.0)))
        inputs += ["-stream_loop", "-1", "-i", str(mpath)]
        chains.append(f'[{n_in}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=end_sample={total},asetpts=N/SR/TB,volume={gain_db:.2f}dB,afade=t=in:d=0.35[mus0]')
        cur = "mus0"
        if duck:
            # sidechaincompress ends when either input ends, even with samples of the other still waiting, and
            # ffmpeg 8 feeds the inputs from their own threads: endless silence after both, cut at total after
            chains.append("[mus0]apad[mus0p]")
            chains.append("[vkey]apad[vkeyp]")
            chains.append(f'[mus0p][vkeyp]sidechaincompress=threshold=0.1:ratio=3:attack=20:release=300:knee=3:makeup=1,atrim=end_sample={total}[mus1]')
            cur = "mus1"
        if fade > 0:
            chains.append(f'[{cur}]afade=t=out:st={plan.duration - fade:.4f}:d={fade:.4f}[mus]')
        else:
            chains.append(f'[{cur}]anull[mus]')
        mix_inputs.append("[mus]")
        report["music"] = {"file": plan.rel(mpath), "measured_i": mm["input_i"] if mm else None,
                           "volume_db": vol_db, "gain_db": round(gain_db, 2), "duck": duck, "fade_out": fade}
        layer = {"type": "music", "file": plan.rel(mpath), "gain_db": round(gain_db, 2), "duck": duck,
                 "fade_out": fade}
        choice = plan.music_choice
        if choice:
            picked = {"mood": choice.get("mood"), "track_id": choice.get("track_id"), "title": choice.get("title"),
                      "chosen_from": choice.get("from"), "why": choice.get("why"), "source": choice.get("source"),
                      "license": choice.get("license"), "sha256": choice.get("sha256")}
            report["music"].update(picked)
            layer.update({"mood": picked["mood"], "track_id": picked["track_id"]})
        elif music.get("mood") not in (None, ""):
            report["music"]["mood_ignored"] = music.get("mood")      # music.file wins over music.mood
        plan.layers.append(layer)
        n_in += 1

    if len(mix_inputs) > 1:
        # every input exactly total samples long: where amix stops never depends on which input arrived first
        fitted = []
        for k, lab in enumerate(mix_inputs):
            chains.append(f'{lab}apad=whole_len={total},atrim=end_sample={total}[fit{k}]')
            fitted.append(f'[fit{k}]')
        chains.append(f"{''.join(fitted)}amix=inputs={len(fitted)}:normalize=0:duration=longest:dropout_transition=0[mixraw]")
    else:
        chains.append(f'{mix_inputs[0]}anull[mixraw]')
    chains.append(f'[mixraw]apad=whole_len={total},atrim=end_sample={total}[aout]')
    script = work / "audio_graph.txt"
    script.write_text(";\n".join(chains) + "\n", encoding="utf-8")
    mix = work / "mix.wav"
    ffmpeg_cmd(["-v", "error", "-y", *inputs, *graph_args(script), "-map", "[aout]", "-c:a", "pcm_f32le",
                "-ar", SR, "-ac", 2, str(mix)], 'mix audio')

    normal_tp = target_tp - AAC_TP_HEADROOM
    if (not plan.has_audio and not music_on) or data.get('_audio_policy', {}).get('mode') == 'preserve':
        # no voice and no music: only effects. Normalizing them to -14 LUFS would make them blast.
        measured = None
        os.replace(mix, out_wav)
        plan.warnings.append('the base video has no audio; only effects will be mixed, without loudness normalization')
    else:
        measured = loudnorm_measure(mix, i=target_i, tp=normal_tp, lra=target_lra)
        if measured:
            af = (f"loudnorm=I={target_i}:TP={normal_tp}:LRA={target_lra}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true")
        else:
            raise ComposeError('Normalization needs a finite measured voice track; select preserve for silence')
        ffmpeg_cmd(["-v", "error", "-y", "-i", mix, "-af",
                    f'{af},aresample={SR},apad=whole_len={total},atrim=end_sample={total}',
                    "-c:a", "pcm_f32le", "-ar", SR, "-ac", 2, str(out_wav)], "normalize volume")
    report["loudnorm_input"] = measured
    report["voice_i"] = voice_i
    if not plan.data.get("_keep_temp") and mix.exists():
        try:
            mix.unlink()
        except OSError:
            pass


# ============================================================== main

def probe_output(path: Path) -> dict:
    info = ffprobe_json(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    from fractions import Fraction
    try:
        fps = float(Fraction(v.get("avg_frame_rate") or "0/1"))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {"duration": float((info.get("format") or {}).get("duration") or 0), "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0), "fps": round(fps, 3), "frames": int(v.get("nb_frames") or 0),
            "video_codec": v.get("codec_name"), "profile": v.get("profile"), "pix_fmt": v.get("pix_fmt"),
            "color_space": v.get("color_space"), "color_primaries": v.get("color_primaries"),
            "color_transfer": v.get("color_transfer"), "color_range": v.get("color_range"),
            "audio_codec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0),
            "channels": int(a.get("channels") or 0), "audio_duration": float(a.get("duration") or 0)}


def update_state(plan: Plan) -> None:
    state_path = plan.edit_dir / "state.json"
    state = load_json(state_path, default={}) or {}
    if not isinstance(state, dict):
        state = {}
    if plan.preview:
        # The low-res preview is for the agent to check frames; it never asks the person to approve.
        state.update({
            "phase": "final",
            "awaiting": None,
            "edit_data": plan.rel(plan.edit_data_path),
            "message": 'Composing the final video...',
            "updated_at": now_iso(),
        })
    else:
        state.update({
            "phase": "final",
            "awaiting": "approval_final",
            "final_video": plan.rel(plan.out_path),
            "edit_data": plan.rel(plan.edit_data_path),
            "message": 'Final video ready. Watch it and identify any requested changes.',
            "updated_at": now_iso(),
        })
    save_json(state_path, state)


def compose(edit_data_path: Path, out: str | None, preview: bool, keep_temp: bool = False) -> int:
    from workspace import managed_operation
    with managed_operation(Path(edit_data_path).resolve().parent):
        return _compose(edit_data_path, out, preview, keep_temp)


def _compose(edit_data_path: Path, out: str | None, preview: bool, keep_temp: bool = False) -> int:
    t_start = time.time()
    require_binary("ffmpeg")
    require_binary("ffprobe")
    plan = build_plan(edit_data_path, out, preview)
    timed_sfx = plan_sfx_events(plan)
    denoise = plan_denoise(plan)
    motions = plan_motion(plan)
    tv_cfg = plan_screen_green(plan)
    if keep_temp:
        plan.data["_keep_temp"] = True
    plan.renders.mkdir(parents=True, exist_ok=True)
    log(f"composing {plan.rel(plan.out_path)}: {plan.W}x{plan.H} {plan.fps}fps, {plan.nf} frames ({fmt_time(plan.duration)}){(' · preview' if preview else '')}")
    if plan.music_choice:
        c = plan.music_choice
        why = 'same as the previous composition for this edit' if c.get("from") == "previous selection" else c.get("why")
        log(f'''music for mood {c.get('mood')}: {c.get('track_id')} "{c.get('title')}" in {c.get('file')} ({why})''')

    load_faces(plan)
    cam = camera_filter(plan)
    if tv_cfg is not None:
        # the inserts first: a wrong one stops before the cut-out; split_top / split_bottom become the background
        inserts = plan_inserts(plan, person_cutout=True)
        insert_backgrounds(plan, tv_cfg, inserts)
        prepare_screen_green(plan, tv_cfg)
        place_pips_screen_green(plan, inserts)
        cam = None
    else:
        inserts = plan_inserts(plan)
    drawn = [i for i in inserts if not i.to_bg]         # Every insert when the composition does not use green screen.
    compute_face_zones(plan, drawn)
    compute_person_zones(plan, drawn)
    if plan.image_zones:
        covered = [(i.f0, i.f1) for i in drawn if i.type == "full"]
        plan.image_zones = subtract_windows(plan.image_zones, merge_windows(covered)) if covered else plan.image_zones
    audio_report: dict = {}
    denoise_report: dict = {"mode": "off", "applied": "off"}
    audio_wav = plan.renders / "audio" / "final_audio.wav"
    audio_error: list[BaseException] = []

    t0 = time.time()
    tv_clip = None
    with ThreadPoolExecutor(max_workers=3) as ex:
        tv_job = ex.submit(render_screen_green, plan) if plan.tv else None
        clips = list(ex.map(lambda i: render_insert_clip(plan, i), drawn))
        if tv_job is not None:
            tv_clip = tv_job.result()
            plan.tv["compose_seconds"] = tv_clip.get("render_seconds", 0.0)
            layer = screen_green_layer(plan, tv_clip)
            plan.layers.insert(0, layer)
            log(f"green screen ready{(' (cached)' if tv_clip.get('cached') else '')}: cutout {plan.tv['matte_seconds']:.1f}s, composition {tv_clip.get('render_seconds', 0.0):.1f}s ({plan.nf} frames)")
    for ins, clip in zip(drawn, clips):
        ins.clip = clip
    bg_rows = {b.insert: b for b in plan.tv["cfg"].from_inserts} if plan.tv else {}
    for ins in inserts:
        if ins.to_bg:
            b = bg_rows.get(ins.idx + 1)
            plan.layers.append({"type": "insert", "insert_type": ins.type, "as": "background", "src": plan.rel(ins.src),
                                "kind": ins.kind, "start": ins.f0 / plan.fps, "end": ins.f1 / plan.fps,
                                "frames": [ins.f0, ins.f1], "ken_burns": ins.ken_burns,
                                "fit": b.fit if b else None,
                                "rect": ([int(round(v / plan.s)) for v in b.place] if b and b.place
                                         else [0, 0, plan.full_w, plan.full_h]),
                                "speaker_rect": None, "speaker_offsets": None, "seam_y": None, "sfx": ins.sfx})
            continue
        plan.layers.append({"type": "insert", "insert_type": ins.type, "src": plan.rel(ins.src), "kind": ins.kind,
                            "start": ins.f0 / plan.fps, "end": ins.f1 / plan.fps, "frames": [ins.f0, ins.f1],
                            "ken_burns": ins.ken_burns, "rect": list(ins.content),
                            "speaker_rect": list(ins.speaker) if ins.speaker else None,
                            "speaker_offsets": ([{"start": round(p0 / plan.fps, 3), "end": round(p1 / plan.fps, 3),
                                                  "src_y": int(round(y / plan.s))} for p0, p1, y in ins.speaker_pieces]
                                                if ins.speaker else None),
                            "seam_y": ins.seam_y, "sfx": ins.sfx})
    if drawn:
        log(f'inserts ready ({len(drawn)}) in {time.time() - t0:.1f}s')

    t0 = time.time()
    tracks = build_tracks(plan, drawn, motions)
    motion_layers(plan, motions)
    log(f'text layers ready in {time.time() - t0:.1f}s')

    def audio_job():
        try:
            voice = None
            if denoise != "off":
                t_dn = time.time()
                voice, rep = run_denoise(plan, denoise, keep_temp)
                denoise_report.clear()
                denoise_report.update(rep)
                denoise_report["seconds"] = round(time.time() - t_dn, 2)
            run_audio(plan, inserts, tracks["sfx_events"] + timed_sfx, audio_wav, audio_report, voice=voice)
        except BaseException as e:  # noqa: BLE001
            audio_error.append(e)

    th = threading.Thread(target=audio_job, daemon=True)
    th.start()

    t0 = time.time()
    tmp_video = plan.renders / "final_video_only.mp4"
    run_video(plan, drawn, tracks, cam, tmp_video, Path(tv_clip["file"]) if tv_clip else None)
    log(f'video encoded in {time.time() - t0:.1f}s')
    th.join()
    if audio_error:
        err = audio_error[0]
        raise err if isinstance(err, ComposeError) else ComposeError(f'audio failure: {err}')

    plan.out_path.parent.mkdir(parents=True, exist_ok=True)
    loud = _section(plan.data, "loudness")
    target_i, target_tp = float(loud.get("i", -14)), float(loud.get("tp", -1.0))
    import tempfile
    with tempfile.TemporaryDirectory(prefix='.compose-', dir=plan.out_path.parent) as temporary:
        tmp_out = Path(temporary) / 'candidate.mp4'

        def mux(gain_db: float) -> None:
            policy = plan.data.get('_audio_policy', {})
            untouched = policy.get('mode') == 'preserve' and policy.get('denoise') == 'off' and not policy.get('music') and not policy.get('effects')
            if untouched:
                ffmpeg_cmd(['-v', 'error', '-y', '-i', tmp_video, '-i', plan.base, '-map', '0:v:0', '-map', '1:a?',
                            '-c', 'copy', '-t', f'{plan.duration:.6f}', '-movflags', '+faststart', '-f', 'mp4', tmp_out],
                           'preserve original audio streams under the edited picture')
                return
            af = ["-af", f'volume={gain_db:.2f}dB'] if abs(gain_db) > 0.001 else []
            ffmpeg_cmd(["-v", "error", "-y", "-i", tmp_video, "-i", audio_wav, "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", *af, "-c:a", "aac", "-b:a", "192k", "-ar", SR, "-ac", 2,
                        "-t", f'{plan.duration:.6f}', "-movflags", "+faststart", "-f", "mp4", tmp_out],
                       'mux video and audio')

        mux(0.0)
        normalize = plan.has_audio and plan.data.get('_audio_policy', {}).get('mode', 'normalize') == 'normalize'
        final_loud = loudnorm_measure(tmp_out, i=target_i, tp=target_tp) if normalize else None
        if final_loud and final_loud["input_tp"] > target_tp + 0.05:
            # the AAC encoder pushed a true peak over the target: trim the gain just enough and encode again
            mux(target_tp - final_loud["input_tp"] - 0.1)
            final_loud = loudnorm_measure(tmp_out, i=target_i, tp=target_tp)
        if normalize and (not final_loud or final_loud['input_tp'] > target_tp):
            tmp_out.unlink(missing_ok=True)
            raise ComposeError('Encoded audio did not meet the confirmed peak limit; candidate was not published')
        if plan.data.get('_audio_policy'):
            from project_contract import managed_config
            managed_config(plan.edit_dir)
            os.link(tmp_out, plan.out_path)
        else:
            os.replace(tmp_out, plan.out_path)
    if plan.data.get('_audio_policy'):
        from common import content_hash
        from workspace import record_artifact, render_dependencies
        project = plan.edit_dir.parent / 'project'
        record_artifact(project, 'render:' + content_hash(plan.out_path), plan.out_path, render_dependencies(project),
                        {'overlayMasks': tracks.get('overlayMasks', []), 'renderer': 'ffmpeg-compose'})
    for p in (tmp_video, audio_wav):
        if keep_temp:
            break
        try:
            p.unlink()
        except OSError:
            pass

    facts = probe_output(plan.out_path)
    elapsed = time.time() - t_start
    for e in audio_report.get("sfx", []):
        layer = {"type": "sfx", "name": e["name"], "why": e["why"], "time": e["time"], "sample": e["sample"]}
        for key in ("effect", "at", "volume", "event", "motion", "length", "anchor_s", "late_s"):
            if key in e:
                layer[key] = e[key]
        plan.layers.append(layer)
    report = {
        "output": plan.rel(plan.out_path),
        "edit_data": plan.rel(plan.edit_data_path),
        "base": plan.rel(plan.base),
        "preview": plan.preview,
        "duration": round(plan.duration, 6),
        "frames": plan.nf,
        "fps": plan.fps,
        "width": plan.W,
        "height": plan.H,
        "probe": facts,
        "loudness": {
            "target_i": target_i, "target_tp": target_tp,
            "i": final_loud["input_i"] if final_loud else None,
            "tp": final_loud["input_tp"] if final_loud else None,
            "lra": final_loud["input_lra"] if final_loud else None,
            "voice_before_i": audio_report.get("voice_i"),
        },
        "music": audio_report.get("music"),
        "denoise": denoise_report,
        "layers": plan.layers,
        "faces": {"file": "face_layout.json" if plan.faces else None,
                  "segments_with_face": sum(1 for cs in plan.cam_segs if cs.get("face")),
                  "segments": len(plan.cam_segs)},
        "adjustments": plan.adjustments,
        "face_checks": plan.face_checks,
        "warnings": plan.warnings,
        "render_seconds": round(elapsed, 2),
        "realtime_factor": round(elapsed / max(plan.duration, 1e-6), 3),
        "created_at": now_iso(),
    }
    report_path = plan.edit_dir / "verify" / "final_report.json"
    save_json(report_path, report)
    update_state(plan)

    counts = {}
    for layer in plan.layers:
        counts[layer["type"]] = counts.get(layer["type"], 0) + 1
    parts = [f'{v} {k}' for k, v in counts.items() if k != "camera"]
    cam_layer = next((layer for layer in plan.layers if layer["type"] == "camera"), {})
    if cam_layer.get("zoom_cuts"):
        parts.append("zoom on cuts")
    if cam_layer.get("push_in"):
        parts.append('slow push-in')
    log(f"layers: {(', '.join(parts) if parts else 'none beyond the base')}")
    moved = [a for a in plan.adjustments if a.get("action") == "moved"]
    hidden = [a for a in plan.adjustments if a.get("action") == "hidden"]
    if moved or hidden:
        names = {"captions": ("captions", "hidden"), "disclaimer": ('warning', "hidden"), "hook": ("hook", "hidden"),
                 "cta": ("callout", "hidden")}
        for a in moved + hidden:
            if a["layer"].startswith("motion ") and a["layer"] not in names:
                names[a["layer"]] = (a["layer"].replace("motion", 'animation'), "hidden")
        bits = [f"{names.get(a['layer'], (a['layer'], 'hidden'))[0]} {('changed vertical position' if a['action'] == 'moved' else names.get(a['layer'], ('', 'hidden'))[1])} {fmt_time(a['start'])}-{fmt_time(a['end'])}" for a in (moved + hidden)[:6]]
        more = f' and {len(moved) + len(hidden) - 6} more' if len(moved) + len(hidden) > 6 else ""
        log(f"text-overlap adjustments: {'; '.join(bits)}{more} (see adjustments in the report)")
    if final_loud:
        log(f"final volume: {final_loud['input_i']:.1f} LUFS, peak {final_loud['input_tp']:.1f} dBTP")
    line = denoise_line(denoise_report)
    if line:
        log(line)
    for w in plan.warnings:
        log(f'notice: {w}')
    log(f"ok: {plan.out_path} ({facts['width']}x{facts['height']}, {facts['fps']:g}fps, {facts['duration']:.2f}s)")
    log(f'report: {report_path}')
    log(f'total render time: {elapsed:.1f}s ({elapsed / max(plan.duration, 1e-06):.2f} times the video duration)')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Compose the final video from edit_data.json: framing, inserts, text, effects, and music.')
    ap.add_argument('edit_data', help='edit_data.json path in the edit folder')
    ap.add_argument("-o", "--output", help='output file (default: final.mp4 in the edit folder; final_preview.mp4 with --preview)', dest='output')
    ap.add_argument("--preview", action="store_true", help='quick preview at 540 pixels wide and CRF 26', dest='preview')
    ap.add_argument("--keep-temp", action="store_true", help='retain intermediate files in renders/ for investigation', dest='keep_temp')
    args = ap.parse_args(argv)
    try:
        return compose(Path(args.edit_data).expanduser().resolve(), args.output, args.preview, args.keep_temp)
    except ComposeError as e:
        print(f'error: {e}', file=sys.stderr)
        return e.code
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
