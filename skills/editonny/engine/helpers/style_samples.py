"""Sample PNGs of every caption and hook style on a real frame of the cut.

Writes edit/samples/caption_<id>.png and edit/samples/hook_<id>.png at 540x960 (960x540 for a
horizontal cut) plus edit/samples/index.json, and points state.json "samples" at the index.

The speaker's face is found on the frame (face_layout.py, YuNet) and each sample goes to the free
spot closest to its usual height, off the face, the way compose_final.py places the real text.
Without --time, when the frame at 25% shows no speaker (a screen, a product), the frame moves to the
middle of the nearest cut segment with a face (face_layout.json, which compose_final.py reuses); a
frame where the detector misses the face uses its segment's face zone. A
number in edit_data.json (hook.center_y, captions.center_y) is used as it is. index.json also
gets "face" (the zone found, fractions of the frame, or null), "positions" ({"caption": {id: px},
"hook": {id: px}} on the full-size canvas) and "suggest" ({"camera_focus_y": eyes line or null}).

Usage:
    uv run python helpers/style_samples.py --edit-dir DIR [--time T] [--accent HEX] [--text "..."]
        [--hook-text "..."] [--video other.mp4]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import hex_to_rgb, load_json, load_style_catalog, now_iso, probe_video, require_binary, save_json  # noqa: E402
import face_layout  # noqa: E402
import text_render as tr  # noqa: E402

DEFAULT_CAPTION_TEXT = 'Take a look at this'
DEFAULT_HOOK_TEXT = 'Make every word count'


def grab_frame(video: Path, t: float) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="samples-") as tmp:
        out = Path(tmp) / "frame.png"
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f'{max(0.0, t):.3f}', "-i", str(video), "-frames:v", "1", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if proc.returncode != 0 or not out.exists():
            # seeking past the last frame gives nothing: fall back to the first frame
            cmd[5] = "0"
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if not out.exists():
            raise RuntimeError(f'could not extract a frame from {video}: {proc.stderr[-400:]}')
        with Image.open(out) as im:
            return im.convert("RGB")


def caption_text_near(captions: dict | None, t: float) -> str | None:
    """A real cue around time t (prefers cues with 2+ words)."""
    if not captions:
        return None
    cues = [c for c in captions.get("cues", []) if c.get("words")]
    if not cues:
        return None
    multi = [c for c in cues if len(c["words"]) >= 2] or cues
    best = min(multi, key=lambda c: abs((c["start"] + c["end"]) / 2.0 - t))
    return " ".join(w["text"] for w in best["words"])


def _load_dict(path: Path, notes: list[str]) -> dict:
    """A JSON object from the edit dir, or {} (with a note) when it is missing or unreadable."""
    try:
        data = load_json(path)
    except (ValueError, OSError) as e:
        notes.append(f'ignorei {path.name}: could not read ({e})')
        return {}
    return data if isinstance(data, dict) else {}


def _manual(sec: dict, key: str) -> float | None:
    v = sec.get(key) if isinstance(sec, dict) else None
    if v is None or (isinstance(v, str) and v.strip().lower() == "auto"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def free_spot(img, pref: float, faces: list[tuple[float, float]], lay: dict) -> float:
    """Center for a text drawn at `pref` in `img` (ink box measured) that clears the faces."""
    bb = tr.ink_bbox(img)
    if not bb:
        return pref
    c = face_layout.free_center(pref, pref - bb[1], bb[3] - pref, faces, lay["top"], lay["bottom"])
    return pref if c is None else c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate caption and hook style samples on a frame from the cut.")
    ap.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    ap.add_argument("--time", type=float, help='frame time in seconds (default: 25%% of video duration)', dest='time')
    ap.add_argument("--accent", help='hex accent color (default: edit configuration, preview, or catalog)', dest='accent')
    ap.add_argument("--text", help="sample caption and hook text", dest='text')
    ap.add_argument("--hook-text", help='hook-only text (default: --text, configured hook, or a sample)', dest='hook_text')
    ap.add_argument("--video", help='video from which to extract the frame (default: edit/cut.mp4)', dest='video')
    args = ap.parse_args(argv)

    require_binary("ffmpeg")
    edit = Path(args.edit_dir).resolve()
    if not edit.is_dir():
        print(f'error: edit folder not found: {edit}', file=sys.stderr)
        return 2
    video = Path(args.video).resolve() if args.video else edit / "cut.mp4"
    if not video.exists():
        print(f'error: video not found: {video}. Render the cut first (render_cut.py) or supply --video.',
              file=sys.stderr)
        return 2

    catalog = load_style_catalog()
    notes = []
    edit_data = _load_dict(edit / "edit_data.json", notes)
    picks = _load_dict(edit / "preview_style.json", notes)
    captions = _load_dict(edit / "captions.json", notes) or None

    accent = args.accent or edit_data.get("accent") or picks.get("accent")\
        or (catalog.get("defaults") or {}).get("accent") or tr.DEFAULT_ACCENT
    try:
        hex_to_rgb(accent)
    except ValueError:
        print(f'error: invalid color: {accent!r}; use a value such as #FFC400', file=sys.stderr)
        return 2

    info = probe_video(video)
    duration = float(info.get("duration") or 0.0)
    t = args.time if args.time is not None else duration * 0.25
    if duration > 0:
        t = min(max(0.0, t), max(0.0, duration - 0.05))
    layout_seg = None
    if video == edit / "cut.mp4":
        # the face of each cut segment (face_layout.json, cached for compose_final): the default frame
        # is moved to the nearest segment where the speaker shows up
        try:
            segs = [s for s in face_layout.build(edit).get("segments") or [] if isinstance(s, dict)]
        except Exception as e:  # noqa: BLE001  (samples still work without the face)
            segs = []
            notes.append(f'could not detect a face in the cut ({e})')
        here = next((s for s in segs if s["start"] <= t < s["end"]), None)
        with_face = [s for s in segs if s.get("face")]
        if args.time is None and with_face and not (here and here.get("face")):
            here = min(with_face, key=lambda s: min(abs(t - s["start"]), abs(t - s["end"])))
            t = round((here["start"] + here["end"]) / 2.0, 3)
        layout_seg = here if here and here.get("face") else None

    caption_text = args.text or caption_text_near(captions, t) or DEFAULT_CAPTION_TEXT
    hook_text = args.hook_text or args.text or (edit_data.get("hook") if isinstance(edit_data.get("hook"), dict) else {}).get("text")\
        or picks.get("hook_text") or DEFAULT_HOOK_TEXT

    started = time.time()
    frame = grab_frame(video, t)
    vertical = frame.height >= frame.width
    size = (540, 960) if vertical else (960, 540)
    # compose on the full-size canvas, then shrink: sharper than drawing at 540 wide
    background = tr.cover_image(frame, (1080, 1920) if vertical else (1920, 1080))

    samples = edit / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    cap_ids = [s["id"] for s in catalog.get("caption_styles", [])] or tr.caption_style_ids()
    hook_ids = [s["id"] for s in catalog.get("hook_styles", [])] or tr.hook_style_ids()

    canvas = tr.sample_canvas(background.size)
    lay = tr.layout_defaults(canvas)
    face = zone = None
    try:
        face = face_layout.detect_image(background)
    except Exception as e:  # noqa: BLE001  (samples still work without the face)
        notes.append(f'could not detect the face ({e}); samples use default vertical positions')
    faces: list[tuple[float, float]] = []
    if not face and layout_seg is not None:
        face = layout_seg["face"]       # not found on this very frame: the segment's face
    if face:
        _, zone, _ = face_layout._summarize([face])
        if layout_seg is not None and face is layout_seg["face"] and layout_seg.get("zone"):
            zone = layout_seg["zone"]
        gap = face_layout.GAP_FRACTION * canvas[1]
        faces = [(zone["y0"] * canvas[1] - gap, zone["y1"] * canvas[1] + gap)]
    manual = {"caption": _manual(edit_data.get("captions"), "center_y"), "hook": _manual(edit_data.get("hook"), "center_y")}
    out_cfg = edit_data.get("output") if isinstance(edit_data.get("output"), dict) else {}
    try:
        scale_y = canvas[1] / float(out_cfg.get("height") or canvas[1])
    except (TypeError, ValueError, ZeroDivisionError):
        scale_y = 1.0

    def position(kind: str, sid: str, text: str) -> float:
        if manual[kind] is not None:
            return manual[kind] * scale_y
        if kind == "caption":
            words, active = tr.sample_caption_words(text, sid)
            probe = tr.render_caption_image(words, sid, accent, canvas, active=active, center_y=lay["caption"])
            return free_spot(probe, lay["caption"], faces, lay) if faces else lay["caption"]
        probe = tr.render_hook(text, sid, accent, canvas, center_y=int(round(lay["hook"])))
        return free_spot(probe, lay["hook"], faces, lay) if faces else lay["hook"]

    index = {"frame_time": round(t, 3), "accent": accent, "caption": {}, "hook": {},
             "caption_text": caption_text, "hook_text": hook_text, "size": list(size),
             "face": zone, "positions": {"caption": {}, "hook": {}},
             "suggest": {"camera_focus_y": round(face["eyes_y"], 3) if face else None},
             "created_at": now_iso()}
    skipped = []
    for kind, ids, text in (("caption", cap_ids, caption_text), ("hook", hook_ids, hook_text)):
        for sid in ids:
            try:
                cy = position(kind, sid, text)
                img = tr.render_sample_frame(background, kind, sid, accent, text, center_y=cy).resize(size, Image.LANCZOS)
            except ValueError as e:
                skipped.append(f'{kind} {sid}: {e}')
                continue
            index["positions"][kind][sid] = int(round(cy))
            name = f'{kind}_{sid}.png'
            tmp = samples / f'.tmp-{name}'
            img.save(tmp, "PNG", optimize=False, compress_level=6)
            tmp.replace(samples / name)
            index[kind][sid] = name
    save_json(samples / "index.json", index)

    state_path = edit / "state.json"
    state = load_json(state_path, default={}) or {}
    state["samples"] = "samples/index.json"
    state["updated_at"] = now_iso()
    save_json(state_path, state)

    for msg in skipped:
        print(f'notice: style skipped ({msg})', file=sys.stderr)
    for msg in notes + tr.text_issues("hook", hook_text, hook_ids[0] if hook_ids else "headline"):
        print(f'notice: {msg}', file=sys.stderr)
    where = ""
    if face:
        caps = sorted(set(index["positions"]["caption"].values()))
        hooks = sorted(set(index["positions"]["hook"].values()))
        span = lambda v: f'{v[0]}' if len(v) == 1 else f'{v[0]} to {v[-1]}'  # noqa: E731
        where = (f", face in {zone['y0']:.2f}-{zone['y1']:.2f} of frame height: captions at {span(caps)} px, hook at {span(hooks)} px, camera.focus_y suggested {index['suggest']['camera_focus_y']}")
    print(f"ok: {len(index['caption'])} caption samples and {len(index['hook'])} hook samples (frame at {t:.2f}s, color {accent}, {time.time() - started:.1f}s{where}) -> {samples / 'index.json'}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
