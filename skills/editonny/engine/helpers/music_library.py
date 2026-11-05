'Personal music library organized by mood and stored outside the installed skill.\n\nEDITONNY_MUSIC_LIBRARY overrides the personal library directory. Reject a library placed inside a skill because an update could remove user tracks and distribution could expose them. Keep one folder per English mood ID, a recoverable trash area, an incoming area, and music_library.json with moods, tracks, licenses, provenance, measurements, favorites, and content hashes. Invalid hand-edited entries are preserved separately rather than silently deleted.\n\nRequire contained relative file paths. Copy lossy audio without another lossy conversion. Convert lossless audio or extracted video sound to 48 kHz stereo WAV, preserving appropriate sample precision. Measure integrated loudness, duration, energy development, and speech-band share.\n\nChoose a mood favorite when available, otherwise score duration coverage, late starts, and voice-band competition with deterministic tie-breaking. Copy the chosen file into the edit only when it does not overwrite unrelated content, and record the choice for stable rerenders even if the library changes or disappears. An explicit track selects it; an explicit music file takes priority over mood selection.\n\nUse the English location, moods, list, add, favorite, remove, restore, and choose commands shown by --help. Removal is recoverable. Supply provenance and usage rights when adding music; generated tracks require prior cost/service authorization. Existing personal libraries and files are never migrated or overwritten automatically.'

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HELPERS_DIR, SKILL_ROOT, ffprobe_json, load_json, now_iso, save_json  # noqa: E402

LIBRARY_ENV = "EDITONNY_MUSIC_LIBRARY"
INDEX_NAME = "music_library.json"
TRASH_DIR = "_trash"
INBOX_DIR = "_incoming"
PRESETS_PATH = HELPERS_DIR / "music_moods.json"
LOCK_NAME = "selected_music.json"            # in <EDIT>/assets
LOSSY_KEEP = (".mp3", ".m4a", ".aac", ".ogg", ".opus")
LOSSLESS_CONVERT = (".wav", ".wave", ".flac", ".aif", ".aiff")
VIDEO_CONVERT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
AUDIO_EXT = LOSSY_KEEP + LOSSLESS_CONVERT
SOURCE_USER = 'user-supplied file'
LICENSE_USER = 'user file: use only with appropriate rights, such as original music or a commercial license'
MIN_SECONDS = 1.0
SR_MEASURE = 24000
WINDOW_S = 0.5
FULL_WITHIN_DB = 4.0
DIP_DB = 12.0
DIP_MIN_S = 1.5
TAIL_IGNORED_S = 3.0
VOICE_BAND = (300.0, 3400.0)
FULL_BAND = (20.0, 12000.0)
FADE_ROOM_S = 1.5                                 # compose fades the last 1.5 s: a track should cover cut + this
MOOD_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
RESERVED_MOODS = ("off", "file", "none")   # Choices of the preview's music control.


class MusicError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ text helpers

def slug(text) -> str:
    """'Epic' -> 'epic', 'Light motivation' -> 'light_motivation' (mood IDs)."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40].strip("_")


def num(value: float, digits: int = 1) -> str:
    """Format a decimal number without unnecessary trailing zeros."""
    text = f'{value:.{digits}f}'
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def dur(seconds: float) -> str:
    """64.3 -> '1:04'."""
    s = int(round(max(0.0, float(seconds))))
    return f'{s // 60}:{s % 60:02d}'


def skill_cmd(helper: str, args: str) -> str:
    return f"uv run --directory '{SKILL_ROOT}' python helpers/{helper} {args}"


# ------------------------------------------------------------------ location

def default_library() -> Path:
    return Path.home() / ".editonny" / "music_library"


def library_dir() -> Path:
    raw = (os.environ.get(LIBRARY_ENV) or "").strip()
    return Path(raw).expanduser().absolute() if raw else default_library()


def _looks_like_skill(folder: Path) -> bool:
    try:
        md = folder / "SKILL.md"
        return md.is_file() and "name: editonny-engine" in md.read_text(encoding="utf-8", errors="replace")[:600]
    except OSError:
        return False


def check_location(lib: Path | None = None) -> Path:
    """The library folder, refused when it sits inside a skill folder (an install would delete it)."""
    lib = Path(lib).expanduser().absolute() if lib else library_dir()
    try:
        resolved = lib.resolve()
    except OSError:
        resolved = lib
    skill = SKILL_ROOT.resolve()
    for folder in (resolved, *resolved.parents):
        if folder == skill or _looks_like_skill(folder):
            raise MusicError(f'The music library cannot be inside a skill directory ({folder}): an update could remove tracks and distribution could include them. Use the default folder ({default_library()}) or set {LIBRARY_ENV} to a directory outside the skill.')
    return lib


# ------------------------------------------------------------------ presets and index

def load_presets() -> dict:
    try:
        data = load_json(PRESETS_PATH) or {}
    except (OSError, ValueError) as e:
        raise MusicError(f'could not read {PRESETS_PATH.name} from the skill ({e.__class__.__name__}). Reinstall the skill.', 1)
    return data if isinstance(data, dict) else {}


def preset_moods() -> list[dict]:
    return [m for m in (load_presets().get("moods") or []) if isinstance(m, dict) and m.get("id")]


def empty_index() -> dict:
    return {"version": 1, "moods": {}, "tracks": [], "trash": []}


def fill_hint(mood_id: str) -> str:
    return (f"To add music for mood {mood_id}: (1) a user-supplied file with usage rights: {skill_cmd('music_library.py', f'add FILE --mood {mood_id}')} (2) or authorized AI generation through the user's account, after explicit cost approval: {skill_cmd('gen_music.py', f'--mood {mood_id} --library --dry-run')}")


def missing_library_message(lib: Path, mood_id: str = "sad") -> str:
    return (f'The music library does not exist yet ({lib}). It is created when the first track is added. {fill_hint(mood_id)}. Alternatively, specify a file directly in edit_data.json: "music": {{"enabled": true, "file": "assets/my_music.mp3"}}.')


def load_index(lib: Path, required: bool = True) -> dict | None:
    path = lib / INDEX_NAME
    if not path.is_file():
        if required:
            raise MusicError(missing_library_message(lib))
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MusicError(f'Could not read the library index ({path}): {e.__class__.__name__}. Nothing was changed. Inspect the file before continuing.')
    if not isinstance(data, dict) or not isinstance(data.get("tracks", []), list)\
            or not isinstance(data.get("moods", {}), dict) or not isinstance(data.get("trash", []), list):
        raise MusicError(f'The library index ({path}) has an unexpected format. Nothing was changed.')
    for key, value in empty_index().items():
        data.setdefault(key, value)
    # Manually edited unusable entries are retained under ignored; validate numeric fields.
    ignored = data["ignored"] if isinstance(data.get("ignored"), list) else []
    for key in ("tracks", "trash"):
        good = []
        for t in data[key]:
            if _usable_entry(t):
                good.append(_clean_entry(t))
            else:
                ignored.append(t)
        data[key] = good
    if ignored:
        data["ignored"] = ignored
    return data


TRACK_NUMBERS = ("duration_s", "full_at_s", "voice_band_share", "integrated_lufs")


def _usable_entry(t) -> bool:
    return isinstance(t, dict) and all(isinstance(t.get(k), str) and t[k].strip() for k in ("id", "mood", "file"))


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clean_entry(t: dict) -> dict:
    for key in TRACK_NUMBERS:
        if key in t:
            t[key] = _number(t[key])
    t["favorite"] = t.get("favorite") is True
    if not isinstance(t.get("title"), str) or not t["title"].strip():
        t["title"] = t["id"]
    return t


def library_file(lib: Path, rel) -> Path | None:
    """<lib>/<rel> for a file named in the index; None unless rel is a plain relative path inside the library
    (a hand-edited index must never make remove or choose touch a file outside it)."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel) or any(p in ("", ".", "..") for p in parts):
        return None
    path = lib.joinpath(*parts)
    return path if inside(lib, path) else None


def inside(lib: Path, path: Path) -> bool:
    """A symlinked folder inside the library must not lead outside it."""
    try:
        return path.resolve().is_relative_to(lib.resolve())
    except (OSError, RuntimeError):
        return False


def _has_file(lib: Path, t: dict) -> bool:
    path = library_file(lib, t.get("file"))
    return path is not None and path.is_file()


def save_index(lib: Path, index: dict) -> None:
    try:
        save_json(lib / INDEX_NAME, index)
    except OSError as e:
        raise MusicError(f'Could not write the library index to {lib} ({e.__class__.__name__}). Check free space and directory permissions.', 1)


def all_moods(index: dict | None) -> list[dict]:
    out, seen = [], set()
    for m in preset_moods():
        out.append({"id": m["id"], "label": m.get("label") or m["id"], "when": m.get("when") or "",
                    "prompt": m.get("prompt"), "custom": False})
        seen.add(m["id"])
    for mid, info in sorted(((index or {}).get("moods") or {}).items()):
        if mid in seen or not MOOD_ID.match(str(mid)) or mid in RESERVED_MOODS:
            continue
        info = info if isinstance(info, dict) else {}
        text = {k: (info.get(k) if isinstance(info.get(k), str) else "") for k in ("label", "when", "prompt")}
        out.append({"id": mid, "label": text["label"].strip() or mid, "when": text["when"],
                    "prompt": text["prompt"] or None, "custom": True})
        seen.add(mid)
    for t in (index or {}).get("tracks") or []:
        mid = t.get("mood") if isinstance(t, dict) else None
        if isinstance(mid, str) and mid not in seen and MOOD_ID.match(mid) and mid not in RESERVED_MOODS:
            # a mood the "moods" list lost
            out.append({"id": mid, "label": mid, "when": "", "prompt": None, "custom": True})
            seen.add(mid)
    return out


def find_mood(index: dict | None, text) -> dict | None:
    key = slug(text)
    if not key:
        return None
    for m in all_moods(index):
        if key in (m["id"], slug(m["label"])):
            return m
    return None


def unknown_mood_message(text, index: dict | None) -> str:
    ids = ", ".join(m["id"] for m in all_moods(index))
    return f"Mood '{text}' does not exist. Available moods: {ids}. Full list: {skill_cmd('music_library.py', 'moods')}"


def mood_tracks(lib: Path, index: dict, mood_id: str, existing_only: bool = True) -> list[dict]:
    tracks = [t for t in index.get("tracks", []) if t.get("mood") == mood_id]
    if existing_only:
        tracks = [t for t in tracks if _has_file(lib, t)]
    return sorted(tracks, key=lambda t: (not t.get("favorite"), str(t.get("added_at", "")), str(t["id"])))


def find_track(index: dict, track_id: str) -> dict | None:
    return next((t for t in index.get("tracks", []) if t.get("id") == track_id), None)


def next_id(lib: Path, index: dict, mood_id: str) -> str:
    used = [0]
    for t in list(index.get("tracks", [])) + list(index.get("trash", [])):
        m = re.fullmatch(re.escape(mood_id) + r"-(\d+)", str(t.get("id", "")))
        if m:
            used.append(int(m.group(1)))
    n = max(used) + 1
    while list((lib / mood_id).glob(f'{mood_id}-{n}.*')):
        n += 1
    return f'{mood_id}-{n}'


# ------------------------------------------------------------------ measuring

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def need_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MusicError(f"'{tool}' not found. macOS: brew install ffmpeg · Windows: winget install Gyan.FFmpeg", 1)


def probe_audio(path: Path, name: str | None = None) -> dict:
    name = name or path.name
    try:
        info = ffprobe_json(path)
    except (RuntimeError, ValueError, OSError):
        raise MusicError(f'Could not open {name} as audio; the file may be corrupt or a different type.')
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise MusicError(f'{name} has no audio.')
    try:
        seconds = float(audio.get("duration") or (info.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    return {"duration": seconds, "codec": audio.get("codec_name") or "", "sample_fmt": audio.get("sample_fmt") or "",
            "sample_rate": int(audio.get("sample_rate") or 0), "channels": int(audio.get("channels") or 0)}


def integrated_lufs(path: Path) -> float | None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path), "-vn", "-af",
                           "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    try:
        value = float(json.loads(m.group(0))["input_i"]) if m else None
    except (ValueError, KeyError):
        return None
    return value if value is not None and math.isfinite(value) else None


def decode_mono(path: Path, sr: int = SR_MEASURE, name: str | None = None):
    import numpy as np

    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise MusicError(f'Could not read audio from {name or path.name}.')
    data = proc.stdout[: len(proc.stdout) // 4 * 4]
    return np.frombuffer(data, dtype="<f4").astype(np.float64)


def energy_profile(x, sr: int = SR_MEASURE) -> tuple[str, float, float | None]:
    """(note, full_at_s, dip_at_s) from 0.5 s RMS windows."""
    import numpy as np

    win = max(1, int(WINDOW_S * sr))
    n = len(x) // win
    if n == 0:
        return 'full from the beginning', 0.0, None
    power = (x[: n * win].reshape(n, win) ** 2).mean(axis=1)
    level = 10 * np.log10(power + 1e-12)
    ref = float(np.percentile(level, 90))
    full_idx = int(np.argmax(level >= ref - FULL_WITHIN_DB))
    full_at = full_idx * WINDOW_S
    note = 'full from the beginning' if full_at <= 1.0 else f'starts quietly and builds at {num(full_at)} s'
    dip_at = None
    need = int(round(DIP_MIN_S / WINDOW_S))
    last = n - int(round(TAIL_IGNORED_S / WINDOW_S))
    run = 0
    for i in range(full_idx + 1, max(full_idx + 1, last)):
        run = run + 1 if level[i] < ref - DIP_DB else 0
        if run >= need:
            dip_at = (i - need + 1) * WINDOW_S
            break
    if dip_at is not None:
        note += f'; drops at {num(dip_at)} s'
    return note, round(full_at, 2), dip_at


def voice_band_share(x, sr: int = SR_MEASURE) -> float:
    import numpy as np

    frame = 4096
    n = len(x) // frame
    if n == 0:
        x = np.pad(x, (0, frame - len(x)))
        n = 1
    frames = x[: n * frame].reshape(n, frame) * np.hanning(frame)
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).sum(axis=0)
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    total = power[(freqs >= FULL_BAND[0]) & (freqs <= FULL_BAND[1])].sum()
    voice = power[(freqs >= VOICE_BAND[0]) & (freqs <= VOICE_BAND[1])].sum()
    return round(float(voice / total), 3) if total > 0 else 0.0


def measure(path: Path, name: str | None = None) -> dict:
    """name: the file the person gave (path may be the temporary copy), for the messages."""
    info = probe_audio(path, name)
    x = decode_mono(path, name=name)
    if len(x) == 0 or not (abs(x).max() > 1e-6):
        raise MusicError(f'{name or path.name} is silent.')
    note, full_at, _dip_at = energy_profile(x)
    seconds = info["duration"] or len(x) / SR_MEASURE
    lufs = integrated_lufs(path)
    return {"duration_s": round(seconds, 3), "integrated_lufs": round(lufs, 1) if lufs is not None else None,
            "energy": note, "full_at_s": full_at, "voice_band_share": voice_band_share(x)}


# ------------------------------------------------------------------ changing the library

def _convert(src: Path, dest: Path, sample_fmt: str) -> None:
    codec = "pcm_s16le" if sample_fmt.startswith(("s16", "u8")) else "pcm_s24le"
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0",
                           "-vn", "-ac", "2", "-ar", "48000", "-c:a", codec, "-f", "wav", str(dest)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-2:])[:200]
        raise MusicError(f'Could not convert {src.name} to WAV ({tail}).', 1)


def known_origin(src: Path, lib: Path) -> dict:
    """Source, prompt, license and generator facts the skill already wrote for a generated file: the note next to
    a file waiting in <lib>/_incoming (gen_music --library) or its entry in <EDIT>/assets/credits.json."""
    try:
        if src.resolve().parent == (lib / INBOX_DIR).resolve():
            note = load_json(src.with_suffix(".json"))
            if isinstance(note, dict):
                facts = ("model", "instrumental", "generated_seconds")
                return {"source": note.get("source"), "prompt": note.get("prompt"), "license": note.get("license"),
                        "title": note.get("title"), "extra": {k: note[k] for k in facts if k in note},
                        "note_file": src.with_suffix(".json")}
        if src.parent.name == "assets":
            credits = load_json(src.parent / "credits.json")
            for c in credits if isinstance(credits, list) else []:
                if isinstance(c, dict) and c.get("file") == f'assets/{src.name}':
                    rules = "; ".join(str(c[k]) for k in ("license", "note") if isinstance(c.get(k), str) and c[k])
                    return {"source": c.get("source"), "prompt": c.get("prompt"), "license": rules or None,
                            "extra": {k: c[k] for k in ("model", "instrumental") if k in c}}
    except (OSError, ValueError):
        pass
    return {}


def add_track(src, mood_text, *, title: str | None = None, favorite: bool = False, source: str | None = None,
              prompt: str | None = None, license_note: str | None = None, new_mood: bool = False,
              when: str | None = None, lib: Path | None = None, move_source: bool = False,
              extra: dict | None = None) -> dict:
    """Copy (or convert) one file into the library and index it. Returns the new entry (+ "_new_mood")."""
    lib = check_location(lib)
    src = Path(src).expanduser()
    if not src.is_file():
        raise MusicError(f'file not found: {src}')
    ext = src.suffix.lower()
    if ext not in AUDIO_EXT + VIDEO_CONVERT:
        raise MusicError(f"Unsupported format ({ext or 'no extension'}): {src.name}. Use wav, mp3, m4a, aac, ogg, opus, flac, or aiff, or extract audio from an mp4 or mov video.")
    need_ffmpeg()
    info = probe_audio(src)
    if info["duration"] and info["duration"] < MIN_SECONDS:
        raise MusicError(f"{src.name} is only {num(info['duration'], 2)} seconds long. A music track needs at least {num(MIN_SECONDS)} seconds; use sfx.events for a short effect")
    index = load_index(lib, required=False) or empty_index()
    mood = find_mood(index, mood_text)
    created = False
    if mood is None:
        mid = slug(mood_text)
        if not mid or not MOOD_ID.match(mid) or mid in RESERVED_MOODS:
            raise MusicError(f"Invalid mood name: '{mood_text}'. Use simple words, for example --mood sad.")
        close = difflib.get_close_matches(mid, [m["id"] for m in all_moods(index)], n=1, cutoff=0.75)
        if close and not new_mood:
            raise MusicError(f"Mood '{mood_text}' does not exist. Did you mean '{close[0]}'? To create this distinct mood, retry with --new-mood")
        label = " ".join(str(mood_text).split())[:40]
        index["moods"][mid] = {"label": label, "when": " ".join(str(when or "").split())[:120],
                               "created_at": now_iso()}
        mood = {"id": mid, "label": label, "when": index["moods"][mid]["when"], "custom": True}
        created = True
    elif when and mood["custom"]:
        info = index["moods"].get(mood["id"])
        if not isinstance(info, dict):
            info = index["moods"][mood["id"]] = {"label": mood["label"], "created_at": now_iso()}
        info["when"] = " ".join(str(when).split())[:120]

    folder = lib / mood["id"]
    try:
        inside = src.resolve().relative_to(lib.resolve())
    except (ValueError, OSError):
        inside = None
    if inside is not None and inside.parts and inside.parts[0] == TRASH_DIR:
        raise MusicError(f"{src.name} is in the library trash. Restore it with: {skill_cmd('music_library.py', 'restore ID')}")
    src_sha = sha256(src)
    for t in index["tracks"]:
        if t.get("mood") == mood["id"] and src_sha in (t.get("sha256"), t.get("source_sha256")):
            raise MusicError(f"This track already belongs to mood {mood['id']} as {t['id']} ('{t.get('title')}'). Nothing was copied.")
    for t in index["trash"]:
        if t.get("mood") == mood["id"] and src_sha in (t.get("sha256"), t.get("source_sha256")):
            back = skill_cmd("music_library.py", "restore " + t["id"])
            raise MusicError(f"This track is in the trash for mood {mood['id']} as {t['id']} ('{t.get('title')}'). Nothing was copied. To restore it: {back}")
    origin = known_origin(src, lib)
    source = source or origin.get("source")
    title = title or (origin.get("title") if isinstance(origin.get("title"), str) else None)
    prompt = prompt or origin.get("prompt")
    license_note = license_note or origin.get("license")
    extra = {**(origin.get("extra") or {}), **(extra or {})}
    keep = ext in LOSSY_KEEP
    if origin.get("note_file") and keep:
        move_source = True              # A generated file waiting in the incoming folder moves in as an exact byte copy.

    track_id = next_id(lib, index, mood["id"])
    dest = folder / f"{track_id}{(ext if keep else '.wav')}"
    tmp = folder / f'.tmp-{dest.name}'
    new_folder = not folder.exists()
    stored = False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if keep:
            shutil.copyfile(str(src), str(tmp))
        else:
            _convert(src, tmp, info["sample_fmt"])
        facts = measure(tmp, src.name)
        stored_sha = src_sha if keep else sha256(tmp)
        os.replace(str(tmp), str(dest))
        stored = True
    except OSError as e:
        raise MusicError(f'Could not copy {src.name} into the library ({e.__class__.__name__}). Check free space and permissions for {folder}.', 1)
    finally:
        if tmp.exists():
            tmp.unlink()
        if not stored and new_folder:
            try:
                folder.rmdir()          # only an empty folder this call created
            except OSError:
                pass

    if favorite:
        for t in index["tracks"]:
            if t.get("mood") == mood["id"]:
                t["favorite"] = False
    clean_title = " ".join(str(title or "").split())[:80] or\
        " ".join(re.sub(r"[_\-]+", " ", src.stem).split())[:80] or track_id
    entry = {
        "id": track_id, "mood": mood["id"], "file": dest.relative_to(lib).as_posix(), "title": clean_title,
        **facts,
        "source": " ".join(str(source or SOURCE_USER).split())[:120],
        "license": " ".join(str(license_note or LICENSE_USER).split())[:300],
        "prompt": (" ".join(str(prompt).split())[:2000] if prompt else None),
        "added_at": now_iso(), "favorite": bool(favorite), "original_name": src.name,
        "format": 'original copy' if keep else '48 kHz stereo WAV',
        "sha256": stored_sha, "source_sha256": src_sha,
    }
    for key, value in (extra or {}).items():
        entry.setdefault(key, value)
    index["tracks"].append(entry)
    try:
        save_index(lib, index)
    except MusicError:
        dest.unlink()
        raise
    if move_source:
        for leftover in (src, origin.get("note_file")):
            try:
                if leftover is not None:
                    leftover.unlink()
            except OSError:
                pass
    return {**entry, "_new_mood": created}


def set_favorite(track_id: str, on: bool = True, lib: Path | None = None) -> dict:
    lib = check_location(lib)
    index = load_index(lib)
    entry = find_track(index, track_id)
    if entry is None:
        raise MusicError(_in_track(index, track_id))
    for t in index["tracks"]:
        if t.get("mood") == entry["mood"]:
            t["favorite"] = False
    entry["favorite"] = bool(on)
    save_index(lib, index)
    return entry


def _in_track(index: dict, track_id: str) -> str:
    if any(t.get("id") == track_id for t in index.get("trash", [])):
        return (f"Track {track_id} is in the library trash. Restore it with: {skill_cmd('music_library.py', f'restore {track_id}')}")
    return f"Could not find track '{track_id}' in the library. List available IDs with {skill_cmd('music_library.py', 'list')}"


def _free_path(folder: Path, name: str) -> Path:
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f'{Path(name).stem}_{n}{Path(name).suffix}'
        n += 1
    return path


def remove_track(track_id: str, lib: Path | None = None) -> tuple[dict, Path]:
    """Move a track to _trash/<mood>/ (never deletes) and keep its entry under "trash"."""
    lib = check_location(lib)
    index = load_index(lib)
    entry = find_track(index, track_id)
    if entry is None:
        raise MusicError(_in_track(index, track_id))
    src = library_file(lib, entry["file"])
    if src is None:
        raise MusicError(f"Track {track_id} points outside the library ({entry['file']}); fix its entry by hand.")
    folder = lib / TRASH_DIR / (entry["mood"] if MOOD_ID.match(entry["mood"]) else "other")
    if not inside(lib, folder):
        raise MusicError(f"The library trash ({folder}) leads outside the library; fix that folder by hand.")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _free_path(folder, Path(entry["file"].replace("\\", "/")).name)
    if src is not None and src.is_file():
        shutil.move(str(src), str(dest))
    index["tracks"] = [t for t in index["tracks"] if t is not entry]
    was_favorite = bool(entry.get("favorite"))
    entry.update({"file": dest.relative_to(lib).as_posix(), "favorite": False, "was_favorite": was_favorite,
                  "removed_at": now_iso()})
    index["trash"].append(entry)
    save_index(lib, index)
    return entry, dest


def restore_track(track_id: str, lib: Path | None = None) -> dict:
    lib = check_location(lib)
    index = load_index(lib)
    entry = next((t for t in index["trash"] if t.get("id") == track_id), None)
    if entry is None:
        raise MusicError(f"Track '{track_id}' is not in the library trash.")
    src = library_file(lib, entry["file"])
    if src is None or not src.is_file():
        raise MusicError(f'Track file {track_id} is missing from the trash ({lib / TRASH_DIR}).')
    if not MOOD_ID.match(entry["mood"]):
        raise MusicError(f"Track {track_id} has an invalid mood in the index ('{entry['mood']}').")
    folder = lib / entry["mood"]
    if not inside(lib, folder):
        raise MusicError(f"Mood folder {folder} leads outside the library; fix that folder by hand.")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _free_path(folder, src.name)
    shutil.move(str(src), str(dest))
    index["trash"] = [t for t in index["trash"] if t is not entry]
    for key in ("removed_at", "was_favorite"):
        entry.pop(key, None)
    entry["file"] = dest.relative_to(lib).as_posix()
    index["tracks"].append(entry)
    save_index(lib, index)
    return entry


# ------------------------------------------------------------------ choosing

def _score(t: dict, seconds: float | None) -> tuple:
    length = float(t.get("duration_s") or 0.0)
    voice = float(t.get("voice_band_share") if t.get("voice_band_share") is not None else 0.5)
    full_at = float(t.get("full_at_s") or 0.0)
    if seconds:
        short = max(0.0, seconds + FADE_ROOM_S - length) / seconds
        late = min(1.0, max(0.0, full_at - 1.0) / seconds)
        closeness = abs(length - (seconds + 2.0))
    else:
        short, late, closeness = 0.0, min(1.0, max(0.0, full_at - 1.0) / 30.0), 0.0
    return (round(3.0 * short + late + voice, 6), closeness, str(t["id"]))


def choose(lib: Path, index: dict, mood: dict, seconds: float | None = None, track_id: str | None = None,
           exclude: tuple = ()) -> tuple[dict, str]:
    """(track, why) for a mood: the asked track, else the favorite, else the best fit."""
    if track_id:
        entry = find_track(index, track_id)
        if entry is None:
            raise MusicError(_in_track(index, track_id))
        if not _has_file(lib, entry):
            raise MusicError(f'Track file {track_id} is missing from the library ({lib}).')
        other = f" (it belongs to mood {entry['mood']})" if entry["mood"] != mood["id"] else ""
        return entry, f'the requested track ({track_id}){other}'
    all_tracks = mood_tracks(lib, index, mood["id"])
    tracks = [t for t in all_tracks if t["id"] not in exclude]
    if not tracks:
        if all_tracks:
            raise MusicError(f"Mood '{mood['id']}' contains only the track already used by this edit ({', '.join(exclude)}). {fill_hint(mood['id'])}.")
        missing = [t for t in index.get("tracks", []) if t.get("mood") == mood["id"]]
        gone = f' ({len(missing)} whose file is missing)' if missing else ""
        raise MusicError(f"Mood '{mood['id']}' ({mood['label']}) has no tracks in the library yet{gone} ({lib}). {fill_hint(mood['id'])}.")
    favorite = next((t for t in tracks if t.get("favorite")), None)
    if favorite:
        return favorite, f"it is the favorite for mood {mood['label']}"
    best = min(tracks, key=lambda t: _score(t, seconds))
    parts = []
    if len(tracks) == 1:
        head = f"it is the only track for mood {mood['label']}"
    else:
        head = f"it is the closest fit among {len(tracks)} tracks for mood {mood['label']} (none is a favorite)"
    length = float(best.get("duration_s") or 0.0)
    if seconds:
        if length >= seconds + FADE_ROOM_S:
            parts.append(f'runs {dur(length)} and covers the {dur(seconds)} of the cut without looping')
        else:
            parts.append(f'runs {dur(length)}, shorter than the cut ({dur(seconds)}), so it will be reused')
    share = best.get("voice_band_share")
    if share is not None:
        lowest = len(tracks) > 1 and all(share <= (t.get("voice_band_share") if t.get("voice_band_share") is not None
                                                   else 1.0) for t in tracks)
        parts.append(f'{round(share * 100)}% of energy in the speech band' + (" (the lowest)" if lowest else ""))
    if best.get("energy"):
        parts.append(str(best["energy"]))
    return best, head + (": " + ", ".join(parts) if parts else "")


# ------------------------------------------------------------------ edit folder

def load_lock(edit: Path) -> tuple[dict, str | None]:
    """(lock, warning). A broken lock counts as empty (the choice is made again)."""
    path = edit / "assets" / LOCK_NAME
    empty = {"version": 1, "moods": {}, "copies": [], "hashes": {}}
    if not path.is_file():
        return empty, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("moods", {}), dict):
            raise ValueError("format")
    except (OSError, ValueError):
        return empty, f'assets/{LOCK_NAME} was unreadable; music was selected again'
    data.setdefault("version", 1)
    data.setdefault("moods", {})
    if not isinstance(data.get("copies"), list):
        data["copies"] = []
    if not isinstance(data.get("hashes"), dict):
        data["hashes"] = {}
    return data, None


def copy_to_edit(lib: Path, entry: dict, edit: Path, mood_id: str, why: str) -> dict:
    """Copy the chosen library file to <EDIT>/assets/music_<mood>.<ext> and record it in the lock."""
    src = library_file(lib, entry.get("file"))
    if src is None or not src.is_file():
        raise MusicError(f"Track file {entry.get('id')} is missing from the library ({lib}).")
    ext = src.suffix.lower()
    assets = edit / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lock, _warn = load_lock(edit)
    digest = sha256(src)
    records = [r for r in lock["moods"].values() if isinstance(r, dict)]
    target = None
    for n in range(1, 100):
        name = f'music_{mood_id}{ext}' if n == 1 else f'music_{mood_id}_{n}{ext}'
        path = assets / name
        rel = f'assets/{name}'
        if not path.exists():
            target = path
            break
        if not path.is_file():
            continue
        current = sha256(path)
        # rewritten only when it still holds what this helper copied (a file the person put there stays)
        if current == digest or lock["hashes"].get(rel) == current or\
                any(r.get("file") == rel and r.get("sha256") == current for r in records):
            target = path
            break
    if target is None:
        raise MusicError(f'assets/ already contains too many files with the music_ prefix{mood_id}. Nothing was copied.', 1)
    rel = f'assets/{target.name}'
    if not (target.is_file() and sha256(target) == digest):
        tmp = assets / f'.tmp-{target.name}'
        try:
            shutil.copyfile(str(src), str(tmp))
            os.replace(str(tmp), str(target))
        except OSError as e:
            raise MusicError(f'Could not copy the track to {assets} ({e.__class__.__name__}).', 1)
        finally:
            if tmp.exists():
                tmp.unlink()
    record = {"track_id": entry["id"], "mood": mood_id, "title": entry.get("title"), "file": rel, "sha256": digest,
              "library_file": entry["file"], "duration_s": entry.get("duration_s"),
              "source": entry.get("source"), "license": entry.get("license"), "why": why, "chosen_at": now_iso()}
    lock["moods"][mood_id] = record
    if rel not in lock["copies"]:
        lock["copies"].append(rel)
    lock["hashes"][rel] = digest
    save_json(assets / LOCK_NAME, lock)
    return record


def cut_seconds(edit: Path) -> float | None:
    try:
        value = float((load_json(edit / "cut_timeline.json") or {}).get("duration") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def _index_for(lib: Path, mood_text) -> dict:
    index = load_index(lib, required=False)
    if index is None:
        raise MusicError(missing_library_message(lib, slug(mood_text) or "sad"))
    return index


def pick_for_edit(edit: Path, mood_text, seconds: float | None = None, track_id: str | None = None,
                  change: bool = False, lib: Path | None = None) -> dict:
    """The `choose` command: always choose again (or the asked track), copy and record."""
    lib = check_location(lib)
    index = _index_for(lib, mood_text)
    mood = find_mood(index, mood_text)
    if mood is None:
        raise MusicError(unknown_mood_message(mood_text, index))
    lock, _warn = load_lock(edit)
    exclude = ()
    if change and not track_id:
        current = (lock["moods"].get(mood["id"]) or {}).get("track_id")
        exclude = (current,) if current else ()
    entry, why = choose(lib, index, mood, seconds, track_id, exclude)
    if exclude:
        why = f'replaced {exclude[0]}; {why}'
    record = copy_to_edit(lib, entry, edit, mood["id"], why)
    return {**record, "mood_label": mood["label"], "from": "library"}


def resolve_for_edit(edit: Path, mood_text, seconds: float | None = None, track_id: str | None = None,
                     lib: Path | None = None) -> dict:
    """compose_final's music.mood: the recorded choice when its file is intact, else choose and record."""
    mood_id = slug(mood_text)
    if not mood_id:
        raise MusicError('music.mood is empty. Specify a mood, for example "mood": "sad".')
    lock, warn = load_lock(edit)
    rec = lock["moods"].get(mood_id)
    if rec and (not track_id or track_id == rec.get("track_id")):
        path = edit / str(rec.get("file", ""))
        if path.is_file() and sha256(path) == rec.get("sha256"):
            out = {**rec, "from": "previous selection"}
            return out
    lib = check_location(lib)
    index = _index_for(lib, mood_text)
    mood = find_mood(index, mood_text)
    if mood is None:
        raise MusicError(unknown_mood_message(mood_text, index))
    entry, why = choose(lib, index, mood, seconds, track_id)
    record = copy_to_edit(lib, entry, edit, mood["id"], why)
    out = {**record, "mood_label": mood["label"], "from": "library"}
    if warn:
        out["warning"] = warn
    return out


def edit_music_files(edit: Path) -> list[str]:
    """Music files already in <EDIT>/assets that the person could use as they are (not the library copies)."""
    assets = edit / "assets"
    if not assets.is_dir():
        return []
    lock, _warn = load_lock(edit)
    ours = set(lock.get("copies") or []) | {str(r.get("file")) for r in lock["moods"].values() if isinstance(r, dict)}
    out = []
    for path in sorted(assets.iterdir()):
        rel = f'assets/{path.name}'
        if not path.is_file() or path.name.startswith((".", "sfx_")) or rel in ours:
            continue
        if path.suffix.lower() in AUDIO_EXT:
            out.append(rel)
    return out


# ------------------------------------------------------------------ read-only views

def moods_summary(lib: Path | None = None) -> dict:
    """What the preview shows: moods that have tracks. Never creates or changes anything."""
    try:
        lib = check_location(lib)
    except MusicError as e:
        return {"exists": False, "error": str(e), "moods": []}
    try:
        index = load_index(lib, required=False)
    except MusicError as e:
        return {"exists": True, "error": str(e), "moods": []}
    if index is None:
        return {"exists": False, "moods": []}
    moods = []
    for m in all_moods(index):
        tracks = mood_tracks(lib, index, m["id"])
        if not tracks:
            continue
        fav = next((t for t in tracks if t.get("favorite")), None)
        moods.append({"id": m["id"], "label": m["label"], "when": m["when"], "count": len(tracks),
                      "favorite_id": fav["id"] if fav else None, "favorite_title": fav.get("title") if fav else None,
                      "custom": m["custom"]})
    return {"exists": True, "moods": moods}


def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))


def track_line(t: dict, lib: Path) -> str:
    bits = [dur(t.get("duration_s") or 0)]
    if t.get("integrated_lufs") is not None:
        bits.append(f"{num(t['integrated_lufs'])} LUFS")
    if t.get("energy"):
        bits.append(str(t["energy"]))
    if t.get("voice_band_share") is not None:
        bits.append(f"speech band {round(t['voice_band_share'] * 100)}%")
    bits.append(str(t.get("source") or SOURCE_USER))
    star = "★" if t.get("favorite") else " "
    gone = "" if _has_file(lib, t) else "  [FILE MISSING]"
    return f'''  {star} {t['id']}  "{t.get('title')}" · ''' + " · ".join(bits) + gone


def cmd_list(args) -> int:
    lib = check_location()
    index = load_index(lib, required=False)
    if args.json:
        _say(json.dumps({"library": str(lib), "index": index}, ensure_ascii=False, indent=2))
        return 0
    if index is None:
        _say(f'The music library is still empty ({lib}).')
        _say(fill_hint("sad") + ".")
        return 0
    only = None
    if args.mood_value:
        only = find_mood(index, args.mood_value)
        if only is None:
            raise MusicError(unknown_mood_message(args.mood_value, index))
    _say(f'Music library: {lib}')
    empty = []
    for m in all_moods(index):
        if only and m["id"] != only["id"]:
            continue
        tracks = mood_tracks(lib, index, m["id"], existing_only=False)
        if not tracks:
            empty.append(m["id"])
            continue
        when = f" · {m['when']}" if m["when"] else ""
        label = "track" if len(tracks) == 1 else "tracks"
        _say(f"\n{m['label']} ({m['id']}){when} · {len(tracks)} {label}")
        for t in tracks:
            _say(track_line(t, lib))
    if empty:
        _say(f"\nNo tracks yet: {', '.join(empty)}.")
        _say(fill_hint(empty[0]) + ".")
    trash = index.get("trash") or []
    if trash and not only:
        _say(f"\nIn trash ({lib / TRASH_DIR}): {', '.join((t['id'] for t in trash))}")
    return 0


def cmd_moods(args) -> int:
    lib = check_location()
    index = load_index(lib, required=False)
    rows = []
    for m in all_moods(index):
        tracks = mood_tracks(lib, index, m["id"]) if index else []
        fav = next((t for t in tracks if t.get("favorite")), None)
        rows.append({**m, "count": len(tracks), "favorite_id": fav["id"] if fav else None,
                     "favorite_title": fav.get("title") if fav else None})
    beats = load_presets().get("beats") or {}
    if args.json:
        _say(json.dumps({"library": str(lib), "moods": rows, "beats": beats}, ensure_ascii=False, indent=2))
        return 0
    _say(f'Library moods ({lib}):')
    for r in rows:
        count = "empty" if not r["count"] else f"{r['count']} {('track' if r['count'] == 1 else 'tracks')}"
        fav = f''' (favorite: "{r['favorite_title']}")''' if r["favorite_title"] else ""
        tag = " [custom]" if r["custom"] else ""
        _say(f"  {r['id']:<12} {r['label']:<12} {r['when'] or '-':<45} {count}{fav}{tag}")
    if beats:
        labels = {m["id"]: m["label"].lower() for m in rows}
        tips = [f"{beat} {' or '.join((labels.get(i, i) for i in ids))}" for beat, ids in beats.items()]
        _say('Suggested for this ad passage: ' + " · ".join(tips))
    return 0


def cmd_add(args) -> int:
    entry = add_track(args.file_value, args.mood_value, title=args.title, favorite=args.favorite_value, source=args.source,
                      prompt=args.prompt, license_note=args.license, new_mood=args.new_mood_value, when=args.when_value)
    lib = library_dir()
    if entry.get("_new_mood"):
        _say(f"Created mood: {entry['mood']}")
    _say(f'''ok: {entry['id']} "{entry['title']}" added to mood {entry['mood']}''')
    _say(f"  file: {lib / entry['file']} ({entry['format']})")
    lufs = f"{num(entry['integrated_lufs'])} LUFS" if entry.get("integrated_lufs") is not None else 'loudness unmeasured'
    _say(f"  {dur(entry['duration_s'])} · {lufs} · {entry['energy']} · {round(entry['voice_band_share'] * 100)}% of energy in the speech band")
    _say(f"  source: {entry['source']} · usage: {entry['license']}")
    _say(f"  favorite: {('yes' if entry['favorite'] else 'no')}")
    return 0


def cmd_favorite(args) -> int:
    entry = set_favorite(args.id, on=not args.remove_value)
    if entry["favorite"]:
        _say(f'''ok: {entry['id']} "{entry.get('title')}" is the favorite for mood {entry['mood']}''')
    else:
        _say(f"ok: mood {entry['mood']} has no favorite; selection follows duration fit")
    return 0


def cmd_remove(args) -> int:
    entry, dest = remove_track(args.id)
    track_id = entry["id"]
    _say(f'''ok: {track_id} "{entry.get('title')}" moved to trash ({dest}). Nothing was deleted.''')
    _say("To restore it: " + skill_cmd("music_library.py", f'restore {track_id}'))
    return 0


def cmd_restore(args) -> int:
    entry = restore_track(args.id)
    _say(f'''ok: {entry['id']} "{entry.get('title')}" restored to mood {entry['mood']}''')
    return 0


def cmd_choose(args) -> int:
    edit = Path(args.edit_dir).expanduser().resolve()
    if not edit.is_dir():
        raise MusicError(f'edit folder not found: {edit}')
    seconds = args.duration_value
    origin = "requested with --duration"
    if seconds is None:
        seconds = cut_seconds(edit)
        origin = 'cut duration' if seconds else 'cut duration unavailable'
    elif not (math.isfinite(seconds) and seconds > 0):
        raise MusicError('--duration must be a positive number, for example 32')
    rec = pick_for_edit(edit, args.mood_value, seconds, args.music_value, args.switch)
    _say(f'''music: {rec['track_id']} "{rec.get('title')}" (mood {rec['mood']})''')
    _say(f"file: {edit / rec['file']}")
    _say(f"reason: {rec['why']}" + (f' ({origin}: {dur(seconds)})' if seconds else ""))
    _say(f"source: {rec.get('source')} · usage: {rec.get('license')}")
    _say(f'''In edit_data.json: "music": {{"enabled": true, "mood": "{rec['mood']}", "volume_db": -15, "duck": true, "fade_out": 1.5}}''')
    return 0


def cmd_location(_args) -> int:
    lib = check_location()
    state = "exists" if (lib / INDEX_NAME).is_file() else 'not created yet; add the first track to create it'
    _say(f'{lib} ({state})')
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="music_library.py", description="Personal music library organized by mood "
                                 "(outside the skill, at ~/.editonny/music_library).")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.required = True
    p = sub.add_parser("location", help="show the library location")
    p.set_defaults(fn=cmd_location)
    p = sub.add_parser("moods", help="list moods, usage guidance, and track counts")
    p.add_argument("--json", action="store_true", dest='json')
    p.set_defaults(fn=cmd_moods)
    p = sub.add_parser("list", help="list tracks by mood with favorites first")
    p.add_argument("--mood", help='only this mood', dest='mood_value')
    p.add_argument("--json", action="store_true", dest='json')
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("add", help='copy a music file into the library')
    p.add_argument('file_value', help='music file (wav, mp3, m4a, aac, ogg, opus, flac, aiff) or video')
    p.add_argument("--mood", required=True, help="mood, such as sad; a new name creates a custom mood", dest='mood_value')
    p.add_argument("--title", help='display name (default: filename)', dest='title')
    p.add_argument("--favorite", action="store_true", help="make this the favorite for its mood", dest='favorite_value')
    p.add_argument("--source", help=f'origin (default: {SOURCE_USER})', dest='source')
    p.add_argument("--prompt", help='prompt used for generated music, when applicable', dest='prompt')
    p.add_argument("--license", help='license or usage note', dest='license')
    p.add_argument("--new-mood", action="store_true", help='create a mood despite a similar existing name', dest='new_mood_value')
    p.add_argument("--when", help='when to use it (for a custom mood)', dest='when_value')
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("favorite", help="set the favorite for a mood")
    p.add_argument('id')
    p.add_argument("--remove", action="store_true", help="clear the favorite flag", dest='remove_value')
    p.set_defaults(fn=cmd_favorite)
    p = sub.add_parser("remove", help='move a track to the library trash without deleting it')
    p.add_argument('id')
    p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("restore", help="restore a track from trash")
    p.add_argument('id')
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("choose", help='choose music for an edit and copy it into assets/')
    p.add_argument("--mood", required=True, dest='mood_value')
    p.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    p.add_argument("--duration", type=float, default=None, help='cut duration in seconds (default: cut_timeline.json)', dest='duration_value')
    p.add_argument("--music", help="use this track ID instead of choosing automatically", dest='music_value')
    p.add_argument("--switch", action="store_true", help='choose a different track from the one currently used', dest='switch')
    p.set_defaults(fn=cmd_choose)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except MusicError as e:
        print(f'error: {e}', file=sys.stderr, flush=True)
        return e.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    utf8_console()
    sys.exit(main())
