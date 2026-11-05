"""Local web preview + JSON API for one edit session.

Serves the preview UI (preview/index.html, app.js, app.css), the media inside the
edit dir with HTTP Range support (so the player can scrub), cached waveform and
filmstrip PNGs, and a small JSON API the UI uses to send marks, approvals and
style picks back to the agent.

Stdlib HTTP server. numpy + PIL + ffmpeg are only used to draw the strips.

Security (the agent treats every saved click as the person speaking):
- binds to 127.0.0.1 by default; the Host header must name this computer (blocks DNS rebinding);
- /api/* and /media/* refuse requests another site started (Sec-Fetch-Site cross-site/same-site);
- every POST needs the per-run token from /api/state in an X-Preview-Token header, a matching Origin
  when one is sent, and JSON; the body is always read and the connection closed, so bytes of a
  refused request can never run as a second request;
- with --host other than loopback, every route needs the token (link with ?t=..., then a cookie);
- GET /api/music (the moods of the personal soundtrack library, read-only) also needs the token header
  and a matching Origin when one is sent.

It writes preview_cache/server.json ({status, url, port, host, pid, edit_dir, started_at}, or
{status: "error", error}) so the agent never has to parse the log.

Usage:
  uv run python helpers/preview_server.py --edit-dir DIR [--port 4830] [--host 127.0.0.1] [--open]
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    PREVIEW_DIR,
    STYLE_CATALOG_PATH,
    file_signature,
    content_hash,
    load_json,
    probe_video,
    save_json,
)
import music_library  # noqa: E402  (personal soundtrack library, read-only here)

DEFAULT_PORT = 4830
MAX_BODY = 1_000_000
CHUNK = 256 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}
TOKEN_HEADER = "X-Preview-Token"
TOKEN_COOKIE = "editonny_preview"
WATCHER_ALIVE_S = 10.0
STRIP_JOBS = threading.BoundedSemaphore(2)   # at most 2 ffmpeg strip renders at once
STRIP_VARIANTS_KEPT = 6                      # newest cached PNGs kept per kind and file
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/api.mjs": ("api.mjs", "text/javascript; charset=utf-8"),
    "/app-shell.mjs": ("app-shell.mjs", "text/javascript; charset=utf-8"),
    "/constants.mjs": ("constants.mjs", "text/javascript; charset=utf-8"),
    "/dom.mjs": ("dom.mjs", "text/javascript; charset=utf-8"),
    "/feedback.mjs": ("feedback.mjs", "text/javascript; charset=utf-8"),
    "/review/approval.mjs": ("review/approval.mjs", "text/javascript; charset=utf-8"),
    "/review/marks.mjs": ("review/marks.mjs", "text/javascript; charset=utf-8"),
    "/review/playback.mjs": ("review/playback.mjs", "text/javascript; charset=utf-8"),
    "/review/timeline.mjs": ("review/timeline.mjs", "text/javascript; charset=utf-8"),
    "/review/trims.mjs": ("review/trims.mjs", "text/javascript; charset=utf-8"),
    "/review/view.mjs": ("review/view.mjs", "text/javascript; charset=utf-8"),
    "/style/controls.mjs": ("style/controls.mjs", "text/javascript; charset=utf-8"),
    "/style/media.mjs": ("style/media.mjs", "text/javascript; charset=utf-8"),
    "/style/state.mjs": ("style/state.mjs", "text/javascript; charset=utf-8"),
    "/style/view.mjs": ("style/view.mjs", "text/javascript; charset=utf-8"),
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "font-src 'self' data:; frame-ancestors 'self'"
)
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
PRIVACY_HINT = (
    'On macOS, protected folders and external volumes may require app-specific write permission. In System Settings, review Privacy & Security permissions for the application running the agent. Alternatively, use a writable folder outside protected locations, such as ~/Videos/my-project. Do not change privacy permissions automatically or bypass a denied write.'
)


def utf8_console() -> None:
    """Windows pipes default to the ANSI code page: an emoji in a path or note must not crash the server."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso_ms() -> str:
    """ISO time with milliseconds, so two saves in the same second stay distinct for the watcher."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ session


def preview_dependencies_hash(edit_dir: Path) -> str:
    from project_contract import digest, read_json
    project = edit_dir.parent / 'project'
    config = project / 'project.json'
    if (edit_dir / 'source_manifest.json').is_file() or (config.is_file() and read_json(config).get('workflow')):
        from workspace import render_dependencies
        return digest(render_dependencies(project))
    dependencies = {}
    for name in ('edl.json', 'cut_timeline.json', 'edit_data.json', 'captions.json', 'source_manifest.json', 'transcripts/cut.json'):
        path = edit_dir / name
        if path.is_file():
            dependencies[name] = content_hash(path)
    if config.is_file():
        dependencies['project'] = content_hash(config)
    return digest(dependencies)


class Session:
    """Everything the handlers need about one edit dir."""

    def __init__(self, edit_dir: Path, catalog_path: Path, host: str, port: int) -> None:
        self.edit_dir = edit_dir.resolve()
        self.catalog_path = catalog_path
        self.cache_dir = self.edit_dir / "preview_cache"
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.loopback = host in LOOPBACK_HOSTS
        self.allowed_names = set(LOOPBACK_NAMES) if self.loopback else allowed_host_names(host)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last_good: dict[str, Any] = {}
        self._identities: dict[str, tuple] = {}

    # -- paths

    def safe_path(self, rel: str) -> Path | None:
        """Resolve a path given relative to the edit dir. None when it escapes the edit dir."""
        if rel is None:
            return None
        rel = str(rel).replace("\\", "/")
        if "\x00" in rel:
            return None
        rel = rel.lstrip("/")
        if not rel:
            return None
        try:
            candidate = (self.edit_dir / rel).resolve()
        except (OSError, RuntimeError):
            return None
        if candidate != self.edit_dir and self.edit_dir not in candidate.parents:
            return None
        return candidate

    def lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    # -- state

    def read_json_file(self, rel: str | None, errors: dict[str, str]) -> Any:
        if not rel:
            return None
        path = self.safe_path(rel)
        if path is None or not path.is_file():
            return None
        key = str(path)
        for attempt in range(3):
            try:
                data = load_json(path)
                self._last_good[key] = data
                return data
            except json.JSONDecodeError as exc:
                if attempt == 2:
                    errors[rel] = f'Invalid JSON: {exc}'
                    return self._last_good.get(key)
                time.sleep(0.05)
            except OSError as exc:
                errors[rel] = f'Could not read: {exc}'
                return self._last_good.get(key)
        return None

    def file_info(self, rel: str, fresh: bool = False) -> dict | None:
        """`fresh` rehashes the bytes; Windows st_ctime is creation time, so the key misses rewrites."""
        path = self.safe_path(rel)
        if path is None or not path.is_file():
            return None
        st = path.stat()
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with self.lock_for('identity:' + str(path)):
            cached = self._identities.get(str(path))
            if fresh or not cached or cached[0] != key:
                cached = (key, content_hash(path))
                self._identities[str(path)] = cached
        return {"mtime": st.st_mtime, "size": st.st_size, 'sha256': cached[1], 'dependencies_hash': preview_dependencies_hash(self.edit_dir),
                "age": max(0.0, time.time() - st.st_mtime),
                'display_label': 'Preview derivative; color fidelity requires the master' if 'preview' in rel else 'Exported media; approval identifies these exact bytes'}

    def api_state(self) -> dict:
        errors: dict[str, str] = {}
        state = self.read_json_file("state.json", errors)
        if not isinstance(state, dict):
            state = None if state is None else state
        st = state if isinstance(state, dict) else {}

        def rel_of(key: str, default: str | None) -> str | None:
            value = st.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            return default

        timeline_rel = rel_of("cut_timeline", "cut_timeline.json")
        captions_rel = rel_of("captions", "captions.json")
        edit_data_rel = rel_of("edit_data", "edit_data.json")
        samples_rel = rel_of("samples", "samples/index.json")
        cut_rel = rel_of("cut_video", "cut.mp4")
        final_rel = rel_of("final_video", None)

        files: dict[str, Any] = {}
        for rel in dict.fromkeys(["cut.mp4", "final.mp4", cut_rel, final_rel, timeline_rel,
                                  captions_rel, edit_data_rel, samples_rel]):
            if rel:
                files[rel] = self.file_info(rel)

        catalog = None
        try:
            catalog = load_json(self.catalog_path)
        except (json.JSONDecodeError, OSError) as exc:
            errors["style_catalog.json"] = f'Invalid style catalog: {exc}'

        saved = {
            "marks": self.read_json_file("preview_marks.json", errors),
            "trims": self.read_json_file("preview_trims.json", errors),
            "approval": self.read_json_file("preview_approval.json", errors),
            "style": self.read_json_file("preview_style.json", errors),
        }
        watcher = {"alive": False, "age": None}
        try:
            beat = load_json(self.cache_dir / "watcher.json") or {}
            if beat.get("updated_at"):
                age = max(0.0, time.time() - float(beat["updated_at"]))
                watcher = {"alive": bool(beat.get("mode") == "loop" and age < WATCHER_ALIVE_S), "age": round(age, 1)}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        return {
            "state": state,
            "timeline": self.read_json_file(timeline_rel, errors),
            "captions": self.read_json_file(captions_rel, errors),
            "edit_data": self.read_json_file(edit_data_rel, errors),
            "samples": self.read_json_file(samples_rel, errors),
            "catalog": catalog,
            "files": files,
            "saved": saved,
            "edit_dir": str(self.edit_dir),
            "errors": errors,
            "server_time": time.time(),
            "token": self.token,
            "watcher": watcher,
        }

    def api_music(self) -> dict:
        """Moods of the personal library that have tracks, plus the music files already in this edit.

        Read-only: never creates the library or changes the index."""
        summary = music_library.moods_summary()
        try:
            files = music_library.edit_music_files(self.edit_dir)
        except OSError:
            files = []
        errors = {}
        edit_data = self.read_json_file("edit_data.json", errors)
        music = edit_data.get("music") if isinstance(edit_data, dict) else None
        current = music.get("file") if isinstance(music, dict) else None
        if isinstance(current, str) and current.strip() and current not in files:
            path = self.safe_path(current)
            if path is not None and path.is_file():
                files.insert(0, current.strip())
        out = {"ok": True, "exists": bool(summary.get("exists")), "moods": summary.get("moods") or [],
               "edit_files": files[:8]}
        if summary.get("error"):
            out["error"] = summary["error"]
        return out

    # -- strips

    def _cache_png(self, kind: str, rel: str, path: Path, params: str, build) -> Path:
        """PNG cached as preview_cache/<kind>_<path hash>_<file sig hash>_<params>.png.

        When the file changes (new sig), strips of older versions of that file are removed.
        """
        sig = file_signature(path)
        rel_hash = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
        sig_hash = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
        prefix = f'{kind}_{rel_hash}_'
        out = self.cache_dir / f'{prefix}{sig_hash}_{params}.png'
        with self.lock_for(str(out)):
            if out.is_file() and out.stat().st_size > 0:
                return out
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with STRIP_JOBS:
                image = build()
            if file_signature(path) != sig:
                raise RuntimeError('The file changed while generating the image')
            tmp = out.with_name(f'.tmp-{os.getpid()}-{threading.get_ident()}-{out.name}')
            image.save(tmp, format="PNG", compress_level=6)
            os.replace(tmp, out)
            current = []
            for old in self.cache_dir.glob(f'{prefix}*.png'):
                if not old.name.startswith(f'{prefix}{sig_hash}_'):
                    try:
                        old.unlink()
                    except OSError:
                        pass
                elif old != out:
                    current.append(old)
            # every size variant of this version stays cached, but only the newest few
            current.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
            for old in current[STRIP_VARIANTS_KEPT - 1:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return out

    def waveform_png(self, rel: str, path: Path, width: int, height: int) -> Path:
        return self._cache_png("waveform", rel, path, f'{width}x{height}',
                               lambda: render_waveform(path, width, height))

    def filmstrip_png(self, rel: str, path: Path, count: int, height: int) -> Path:
        return self._cache_png("filmstrip", rel, path, f'{count}x{height}',
                               lambda: render_filmstrip(path, count, height))


def allowed_host_names(host: str) -> set[str]:
    """Names a browser may put in the Host header when the server listens beyond loopback."""
    names = set(LOOPBACK_NAMES)
    if host not in ("0.0.0.0", "::", ""):
        names.add(host.lower())
    names |= {ip for ip in lan_addresses()}
    hostname = socket.gethostname().lower()
    names |= {hostname, f'{hostname}.local'} if hostname else set()
    return names


def lan_addresses() -> list[str]:
    """IPv4 addresses of this computer on the local network (best effort, no traffic is sent)."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET address: picks the outgoing interface only
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if not ip.startswith("127.")]


def snap(value: int, step: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value / step)) * step))


# ------------------------------------------------------------------ renderers


def render_waveform(path: Path, width: int, height: int):
    """White waveform on transparent: faint peak envelope + solid RMS body, time-aligned to the container duration."""
    import numpy as np
    from PIL import Image

    info = probe_video(path)
    duration = float(info.get("duration") or 0.0)
    rate = 8000 if duration <= 600 else 2000
    alpha = np.zeros((height, width), dtype=np.uint8)
    if info.get("has_audio") and duration > 0:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1",
             "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
            capture_output=True,
        )
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        total = max(width, int(round(duration * rate)))
        if samples.size < total:
            samples = np.pad(samples, (0, total - samples.size))
        else:
            samples = samples[:total]
        edges = np.linspace(0, total, width + 1).astype(np.int64)
        peaks = np.zeros(width, dtype=np.float32)
        rms = np.zeros(width, dtype=np.float32)
        abs_s = np.abs(samples)
        sq = samples * samples
        cum_sq = np.concatenate([[0.0], np.cumsum(sq, dtype=np.float64)])
        for i in range(width):
            a, b = edges[i], max(edges[i] + 1, edges[i + 1])
            peaks[i] = abs_s[a:b].max(initial=0.0)
            rms[i] = np.sqrt(max(0.0, (cum_sq[b] - cum_sq[a]) / (b - a)))
        ref = float(np.percentile(peaks, 99.5)) if peaks.max() > 0 else 1.0
        ref = max(ref, 0.02)
        peaks_n = np.clip(peaks / ref, 0, 1) ** 0.75
        rms_n = np.clip(rms / ref * 1.6, 0, 1) ** 0.75
        half = (height - 2) / 2.0
        center = (height - 1) / 2.0
        dist = np.abs(np.arange(height, dtype=np.float32)[:, None] - center)
        peak_h = np.maximum(peaks_n * half, 0.5)[None, :]
        rms_h = np.maximum(rms_n * half, 0.5)[None, :]
        alpha = np.where(dist <= rms_h, 235, np.where(dist <= peak_h, 95, 0)).astype(np.uint8)
    else:
        mid = height // 2
        alpha[mid:mid + 1, :] = 90
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = 255
    rgba[..., 3] = alpha
    return Image.fromarray(rgba, mode="RGBA")


def render_filmstrip(path: Path, count: int, height: int):
    """`count` evenly spaced thumbnails (cbetween of each slot) side by side, `height` px tall."""
    import numpy as np
    from PIL import Image

    info = probe_video(path)
    if not info.get("has_video"):
        raise ValueError('The file has no video')
    duration = float(info.get("duration") or 0.0)
    dw = int(info.get("display_width") or 1080)
    dh = int(info.get("display_height") or 1920)
    thumb_w = max(8, int(round(height * dw / max(1, dh))))
    strip = Image.new("RGB", (thumb_w * count, height), (12, 13, 16))
    if duration <= 0:
        return strip
    slot = duration / count
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f'{slot / 2:.4f}', "-i", str(path), "-map", "0:v:0",
        "-vf", f'fps=fps={count}/{duration:.6f}:round=near,scale={thumb_w}:{height}:flags=area,format=rgb24',
        "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    frame_bytes = thumb_w * height * 3
    n_frames = len(proc.stdout) // frame_bytes
    if n_frames == 0:
        raise ValueError('FFmpeg returned no frames: ' + proc.stderr.decode("utf-8", "replace")[-300:])
    frames = np.frombuffer(proc.stdout[: n_frames * frame_bytes], dtype=np.uint8).reshape(n_frames, height, thumb_w, 3)
    for i in range(count):
        frame = frames[min(i, n_frames - 1)]
        strip.paste(Image.fromarray(frame, mode="RGB"), (i * thumb_w, 0))
    # thin dark separators between thumbs
    arr = np.asarray(strip).copy()
    for i in range(1, count):
        arr[:, i * thumb_w - 1 : i * thumb_w, :] = (arr[:, i * thumb_w - 1 : i * thumb_w, :] * 0.35).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


# ------------------------------------------------------------------ validation


class BadRequest(Exception):
    pass


def _num(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be a number")
    if f != f or f in (float("inf"), float("-inf")):
        raise BadRequest(f"'{name}' must be a number")
    return round(max(0.0, f), 3)


def _text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise BadRequest('Invalid text')
    return str(value).strip()[:limit]


def _phase(body: dict) -> str:
    phase = body.get("phase")
    if phase not in ("cut", "final"):
        raise BadRequest('\'phase\' must be "cut" or "final"')
    return phase


HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
DENOISE_CHOICES = ("off", "light", "strong")   # Background noise removal in the Style tab (edit_data audio.denoise).
TV_POSITIONS = ("left", "right", "center")   # Green screen: left corner, right corner, center.
MUSIC_OFF = "off"                            # No music.
MUSIC_FILE = "file"                       # Use a file from the folder (+ music_file).
MOOD_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")


def _accent(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not HEX_RE.match(value.strip()):
        raise BadRequest("'accent' must be a #RRGGBB color")
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


MOTION_COUNT = {"few": 3, "some": 5}       # style motionCount -> edit_data motion.count
MOTION_THEMES = ("paper", "bluePaper", "greenPaper", "orangePaper", "graphitePaper", "pop", "neon",
                "dark", "light")   # Looks from visuals.engine.THEME_NAMES.


def validate_marks(body: dict) -> dict:
    phase = _phase(body)
    marks_in = body.get("marks")
    if not isinstance(marks_in, list):
        raise BadRequest("'marks' must be a list")
    if len(marks_in) > 500:
        raise BadRequest('Too many marks (maximum 500)')
    marks = []
    for i, m in enumerate(marks_in):
        if not isinstance(m, dict):
            raise BadRequest(f'Mark {i + 1} is invalid')
        a, b = _num(m.get("in"), "in"), _num(m.get("out"), "out")
        if b < a:
            a, b = b, a
        row = {"in": a, "out": b, "note": _text(m.get("note"), 1000)}
        if m.get("kind") == "motion":          # Visual here: request an animation at this point.
            row["kind"] = "motion"
        marks.append(row)
    marks.sort(key=lambda m: (m["in"], m["out"]))
    return {"phase": phase, "marks": marks, "saved_at": now_iso_ms()}


def validate_trims(body: dict) -> dict:
    """Requested edge adjustments in seconds, relative to the current EDL."""
    phase = _phase(body)
    trims_in = body.get("trims")
    if not isinstance(trims_in, list):
        raise BadRequest("'trims' must be a list")
    if len(trims_in) > 200:
        raise BadRequest('Too many trim adjustments (maximum 200)')
    seen: set[str] = set()
    trims = []
    for i, t in enumerate(trims_in):
        if not isinstance(t, dict):
            raise BadRequest(f'adjustment {i + 1} is invalid')
        rid = t.get("id")
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 60:
            raise BadRequest(f'adjustment {i + 1}: invalid interval ID')
        rid = rid.strip()
        if rid in seen:
            raise BadRequest(f'interval {rid} appears twice')
        seen.add(rid)
        row = {"id": rid}
        for key in ("start_delta", "end_delta"):
            raw = t.get(key)
            if raw in (None, ""):
                row[key] = 0.0
                continue
            try:
                f = float(raw)
            except (TypeError, ValueError):
                raise BadRequest(f"adjustment {i + 1}: '{key}' must be a number")
            if f != f or f in (float("inf"), float("-inf")):
                raise BadRequest(f"adjustment {i + 1}: '{key}' must be a number")
            row[key] = round(max(-2.0, min(2.0, f)), 3)
        if not row["start_delta"] and not row["end_delta"]:
            continue
        for key in ("beat", "quote"):
            if isinstance(t.get(key), str) and t[key].strip():
                row[key] = t[key].strip()[:300]
        trims.append(row)
    return {"phase": phase, "trims": trims, "saved_at": now_iso_ms()}


def validate_approval(body: dict) -> dict:
    record = {"phase": _phase(body), "approved": True, "note": _text(body.get("note"), 2000)}
    if not body.get('file'):
        raise BadRequest('Approval requires the exact reviewed file identity')
    for key in ('file_sha256', 'dependencies_hash'):
        if not isinstance(body.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', body[key]):
            raise BadRequest(f'Approval requires {key}')
        record[key] = body[key]
    # the exact video the person watched, so an approval never carries over to a newer render
    if body.get("file") not in (None, ""):
        if not isinstance(body.get("file"), str) or len(body["file"]) > 300:
            raise BadRequest("'file' is invalid")
        record["file"] = body["file"].strip()
        record["file_mtime"] = _num(body.get("file_mtime"), "file_mtime")
        record["file_size"] = int(_num(body.get("file_size"), "file_size"))
    record["saved_at"] = now_iso_ms()
    return record


def _music_pick(body: dict, edit_dir: Path | None) -> tuple[str | None, str | None]:
    """(music_mood, music_file) of the music selection: None, "off", "file" + a file in the edit, or a mood id."""
    mood = body.get("music_mood")
    if mood in (None, ""):
        return None, None
    if not isinstance(mood, str) or not (mood == MUSIC_OFF or mood == MUSIC_FILE or MOOD_RE.match(mood)):
        raise BadRequest('\'music_mood\' must be "off", "file", or a library mood ID')
    if mood == MUSIC_OFF:
        return mood, None
    if mood == MUSIC_FILE:
        rel = body.get("music_file")
        if not isinstance(rel, str) or not rel.strip() or len(rel) > 300:
            raise BadRequest("'music_file' must name a music file in the edit folder")
        rel = rel.strip().replace("\\", "/")
        if edit_dir is not None:
            path = (edit_dir / rel).resolve()
            if (edit_dir not in path.parents) or not path.is_file() or path.suffix.lower() not in music_library.AUDIO_EXT:
                raise BadRequest("'music_file' is not a music file in the edit folder")
        return mood, rel
    known = {m["id"] for m in music_library.moods_summary().get("moods") or []}
    if mood not in known:
        raise BadRequest(f"The mood '{mood}' has no tracks in the library")
    return mood, None


def validate_style(body: dict, edit_dir: Path | None = None) -> dict:
    def opt_id(key: str) -> str | None:
        v = body.get(key)
        if v in (None, ""):
            return None
        if not isinstance(v, str) or len(v) > 80:
            raise BadRequest(f"'{key}' is invalid")
        return v.strip()

    denoise = body.get("denoise")
    if denoise in (None, ""):
        denoise = None
    elif denoise not in DENOISE_CHOICES:
        raise BadRequest('\'denoise\' must be "off", "light", or "strong"')

    elements_in = body.get("elements") or {}
    if not isinstance(elements_in, dict):
        raise BadRequest("'elements' must be an object of {id: true/false}")
    elements = {}
    for k, v in elements_in.items():
        if not isinstance(k, str) or len(k) > 80:
            raise BadRequest('Invalid element ID')
        elements[k] = bool(v)
    music_mood, music_file = _music_pick(body, edit_dir)
    if music_mood is not None:
        elements["music"] = music_mood != MUSIC_OFF
    edit_type = opt_id("edit_type")
    tv_pos = body.get("greenScreenPosition")
    if tv_pos in (None, ""):
        tv_pos = None
    elif tv_pos not in TV_POSITIONS:
        raise BadRequest('\'greenScreenPosition\' must be "left", "right", or "center"')
    motion_count, motion_theme = body.get("motionCount"), body.get("motionTheme")
    if motion_count not in (None, "") and motion_count not in MOTION_COUNT:
        raise BadRequest('\'motionCount\' must be "few" or "some"')
    if motion_theme not in (None, "") and motion_theme not in MOTION_THEMES:
        raise BadRequest(f"'motionTheme' must be one of: {', '.join(MOTION_THEMES)}")
    record = {
        "edit_type": edit_type,
        "caption_style": opt_id("caption_style"),
        "hook_style": opt_id("hook_style"),
        "accent": _accent(body.get("accent")),
        "hook_text": _text(body.get("hook_text"), 300),
        "cta_text": _text(body.get("cta_text"), 300),
        "elements": elements,
        "denoise": denoise,
        "music_mood": music_mood,
        "note": _text(body.get("note"), 2000),
        "saved_at": now_iso_ms(),
    }
    if music_file:
        record["music_file"] = music_file
    if elements.get("motion") and motion_count:
        record["motionCount"] = motion_count
        record["motionTheme"] = motion_theme or "paper"
    if edit_type == "greenScreen":
        record["greenScreenPosition"] = tv_pos or TV_POSITIONS[1]
        moved = _split_inserts(edit_dir)
        if moved is not None:
            record["greenScreenBackgroundInserts"] = moved
    return record


def _split_inserts(edit_dir: Path | None) -> int | None:
    """How many split_top / split_bottom inserts edit_data.json plans (in green screen they become the background;
    the file is only read). None without a readable edit_data.json."""
    if edit_dir is None:
        return None
    try:
        data = json.loads((edit_dir / "edit_data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("inserts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0
    return sum(1 for it in items if isinstance(it, dict) and it.get("type") in ("split_top", "split_bottom"))


# ------------------------------------------------------------------ HTTP


RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EditorVideoPreview/1.0"
    session: Session  # set on the subclass

    # quiet by default; errors still go to stderr
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[preview] " + (fmt % args) + "\n")

    # -- helpers

    def _send_bytes(self, status: int, body: bytes, ctype: str, extra: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if status >= 400 or self.command == "POST":
            # never reuse a connection after a refusal or a write: leftover bytes must not become a request
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, obj: Any, head: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"}, head)

    def _error(self, status: int, message: str, head: bool = False) -> None:
        self._json(status, {"ok": False, "error": message}, head)

    def _host_ok(self) -> bool:
        """The Host header must name this computer and this port (blocks DNS rebinding)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            name, _, rest = host.partition("]")
            name, port = name + "]", rest.lstrip(":")
        else:
            name, _, port = host.partition(":")
        if port and port != str(self.session.port):
            return False
        return name in self.session.allowed_names

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host") or ""
        return origin == f'http://{host}'

    def _site_ok(self) -> bool:
        """Browsers say who started the request: another site may not read or write the session."""
        return (self.headers.get("Sec-Fetch-Site") or "").strip().lower() not in ("cross-site", "same-site")

    def _token_in_request(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE:
                return value
        return ""

    def _gate_ok(self, query: dict) -> bool:
        """Listening beyond loopback: every route but the entry page needs the token (cookie or ?t=)."""
        if self.session.loopback:
            return True
        given = (query.get("t") or [""])[0] or self._token_in_request() or (self.headers.get(TOKEN_HEADER) or "")
        return bool(given) and hmac.compare_digest(given, self.session.token)

    # -- verbs

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch_get(head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_get(head=False)

    def _dispatch_get(self, head: bool) -> None:
        try:
            if self.headers.get("Content-Length") not in (None, "", "0") or self.headers.get("Transfer-Encoding"):
                self.close_connection = True  # a GET with a body is never ours
                return self._error(400, 'Invalid request', head)
            if not self._host_ok():
                return self._error(403, 'Host not allowed', head)
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if route in ("/", "/index.html") and not self.session.loopback:
                given = (query.get("t") or [""])[0]
                if given and hmac.compare_digest(given, self.session.token):
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f'{TOKEN_COOKIE}={self.session.token}; Path=/; HttpOnly; SameSite=Strict')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
            if not self._gate_ok(query):
                return self._error(403, 'open the complete link supplied by the agent, including ?t=...', head)
            if route.startswith(("/api/", "/media/")) and not self._site_ok():
                return self._error(403, "Request came from another site", head)
            if route in STATIC_FILES:
                return self._static(route, head)
            if route == "/favicon.ico":
                return self._send_bytes(204, b"", "image/x-icon", head=head)
            if route == "/api/health":
                return self._json(200, {"ok": True, "edit_dir": str(self.session.edit_dir)}, head)
            if route == "/api/state":
                return self._json(200, self.session.api_state(), head)
            if route == "/api/music":
                given = self.headers.get(TOKEN_HEADER) or ""
                if not self._origin_ok() or not given or not hmac.compare_digest(given, self.session.token):
                    return self._error(403, 'Invalid preview token; reload the page', head)
                return self._json(200, self.session.api_music(), head)
            if route.startswith("/media/"):
                return self._media(urllib.parse.unquote(route[len("/media/"):]), head)
            if route == "/api/waveform":
                return self._strip("waveform", query, head)
            if route == "/api/filmstrip":
                return self._strip("filmstrip", query, head)
            return self._error(404, 'not found', head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # Keep the server alive and report the error.
            sys.stderr.write(f'[preview] GET error {self.path}: {exc!r}\n')
            try:
                self._error(500, f'internal error: {exc}', head)
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            self.close_connection = True
            if self.headers.get("Transfer-Encoding"):
                return self._error(400, 'send the body with Content-Length')
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._error(400, 'Invalid Content-Length')
            if length < 0 or length > MAX_BODY:
                return self._error(413 if length > MAX_BODY else 400, "empty or oversized body")
            raw = self.rfile.read(length) if length else b""  # read it all before any refusal
            if not self._host_ok() or not self._origin_ok() or not self._site_ok():
                return self._error(403, 'Origin not allowed')
            given = self.headers.get(TOKEN_HEADER) or ""
            if not given or not hmac.compare_digest(given, self.session.token):
                return self._error(403, 'Invalid preview token; reload the page')
            route = urllib.parse.urlsplit(self.path).path
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._error(415, "Send JSON (Content-Type: application/json)")
            if not raw:
                return self._error(400, "empty or oversized body")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._error(400, 'Invalid JSON')
            if not isinstance(body, dict):
                return self._error(400, 'Request body must be a JSON object')
            targets = {
                "/api/marks": ("preview_marks.json", validate_marks),
                "/api/trims": ("preview_trims.json", validate_trims),
                "/api/approve": ("preview_approval.json", validate_approval),
                "/api/style": ("preview_style.json", validate_style),
            }
            if route not in targets:
                return self._error(404, 'not found')
            filename, validate = targets[route]
            try:
                record = validate(body, self.session.edit_dir) if validate is validate_style else validate(body)
            except BadRequest as exc:
                return self._error(400, str(exc))
            if validate is validate_approval and record.get("file"):
                state = self.session.read_json_file('state.json', {}) or {}
                expected_file = state.get('cut_video', 'cut.mp4') if record['phase'] == 'cut' else state.get('final_video')
                if expected_file and record['file'] != expected_file:
                    return self._error(409, 'Displayed media was superseded; wait for the current candidate before approving')
                current = self.session.file_info(record["file"], fresh=True)
                if (not current or abs(current["mtime"] - record["file_mtime"]) > 0.01
                        or current["size"] != record["file_size"] or current['sha256'] != record['file_sha256']
                        or current['dependencies_hash'] != record['dependencies_hash']):
                    return self._error(409, 'The video changed; watch the current version and approve again')
                try:
                    from project_contract import managed_config
                    managed_config(self.session.edit_dir)
                except (ValueError, OSError) as exc:
                    return self._error(409, str(exc))
            try:
                save_json(self.session.edit_dir / filename, record)
            except OSError as exc:
                return self._error(500, f'Could not save {filename}: {exc}')
            return self._json(200, {"ok": True, "file": filename, "saved": record})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            sys.stderr.write(f'[preview] POST error {self.path}: {exc!r}\n')
            try:
                self._error(500, f'internal error: {exc}')
            except Exception:
                pass

    # -- routes

    def _static(self, route: str, head: bool) -> None:
        name, ctype = STATIC_FILES[route]
        path = PREVIEW_DIR / name
        if not path.is_file():
            return self._error(404, f'Interface file missing: preview/{name}', head)
        body = path.read_bytes()
        extra = {"Cache-Control": "no-cache"}
        if name == "index.html":
            extra["Content-Security-Policy"] = CSP
            extra["X-Content-Type-Options"] = "nosniff"
            extra["Referrer-Policy"] = "no-referrer"
        self._send_bytes(200, body, ctype, extra, head)

    def _media(self, rel: str, head: bool) -> None:
        path = self.session.safe_path(rel)
        if path is None:
            return self._error(403, 'Path escapes the edit folder', head)
        if not path.is_file():
            return self._error(404, f'file not found: {rel}', head)
        st = path.stat()
        size = st.st_size
        ctype = MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
        common_headers = {
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Last-Modified": self.date_time_string(st.st_mtime),
            "Cache-Control": "no-cache",
        }
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if range_header and (not if_range or if_range == etag):
            first = range_header.split(",")[0].strip()
            m = RANGE_RE.match(first)
            if not m or (m.group(1) == "" and m.group(2) == ""):
                return self._range_not_satisfiable(size, head)
            if m.group(1) == "":
                suffix = int(m.group(2))
                if suffix == 0:
                    return self._range_not_satisfiable(size, head)
                start = max(0, size - suffix)
            else:
                start = int(m.group(1))
                if m.group(2) != "":
                    end = min(int(m.group(2)), size - 1)
            if start >= size or start > end:
                return self._range_not_satisfiable(size, head)
            status = 206
        length = end - start + 1 if size > 0 else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f'bytes {start}-{end}/{size}')
        for k, v in common_headers.items():
            self.send_header(k, v)
        self.end_headers()
        if head or length == 0:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # the player cancels range requests all the time while scrubbing
            self.close_connection = True

    def _range_not_satisfiable(self, size: int, head: bool) -> None:
        body = json.dumps({"ok": False, "error": 'Invalid interval'}).encode("utf-8")
        self._send_bytes(416, body, "application/json; charset=utf-8",
                         {"Content-Range": f'bytes */{size}', "Accept-Ranges": "bytes"}, head)

    def _strip(self, kind: str, query: dict, head: bool) -> None:
        rel = (query.get("file") or [""])[0]
        if not rel:
            return self._error(400, "informe ?file=", head)
        path = self.session.safe_path(rel)
        if path is None:
            return self._error(403, 'Path escapes the edit folder', head)
        if not path.is_file():
            return self._error(404, f'file not found: {rel}', head)

        def qint(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(float((query.get(name) or [default])[0]))))
            except (TypeError, ValueError):
                return default

        try:
            # sizes snap to the grid the page asks for, so no one can make endless variants
            if kind == "waveform":
                png = self.session.waveform_png(rel, path, snap(qint("w", 2000, 64, 8000), 250, 500, 4000),
                                                snap(qint("h", 104, 16, 800), 8, 16, 256))
            else:
                png = self.session.filmstrip_png(rel, path, snap(qint("n", 40, 1, 240), 4, 4, 160),
                                                 snap(qint("h", 90, 16, 400), 16, 32, 192))
        except Exception as exc:
            sys.stderr.write(f'[preview] Failed to generate {kind} for {rel}: {exc}\n')
            return self._error(422, f'Could not generate the image ({kind}) for {rel}', head)
        body = png.read_bytes()
        self._send_bytes(200, body, "image/png", {"Cache-Control": "private, max-age=31536000, immutable"
                                                  if query.get("v") else "no-cache"}, head)


# ------------------------------------------------------------------ startup


def check_writable(edit_dir: Path) -> str | None:
    """Return an English error message when the edit dir can't be created or written."""
    try:
        edit_dir.mkdir(parents=False, exist_ok=True)
        (edit_dir / "preview_cache").mkdir(exist_ok=True)
        probe = edit_dir / "preview_cache" / f'.write-test-{os.getpid()}'
        with probe.open("w", encoding="utf-8") as fh:
            fh.write("ok")
        probe.unlink()
        return None
    except FileNotFoundError:
        return f'The parent directory for this edit does not exist: {edit_dir.parent}'
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return (f'macOS blocked writing to {edit_dir} (Operation not permitted). This is usually a privacy restriction. {PRIVACY_HINT}')
        if exc.errno == errno.EACCES:
            return (f'No permission to write to {edit_dir}. Check directory write access and macOS privacy permissions. {PRIVACY_HINT}')
        if exc.errno == errno.EROFS:
            return f'The directory {edit_dir} is on a read-only volume. Copy the footage elsewhere.'
        return f'Could not write to {edit_dir}: {exc}'


def write_server_info(edit_dir: Path, info: dict) -> None:
    """preview_cache/server.json: where the agent reads the URL (or the startup error) on any system."""
    try:
        save_json(edit_dir / "preview_cache" / "server.json", {**info, "updated_at": now_iso_ms()})
    except OSError:
        pass


def reusable_preview(edit_dir: Path) -> dict | None:
    """server.json of a preview of this same edit dir that still answers."""
    try:
        info = load_json(edit_dir / "preview_cache" / "server.json") or {}
    except (json.JSONDecodeError, OSError):
        return None
    if info.get("status") != "running" or not isinstance(info.get("port"), int):
        return None
    other = existing_preview("127.0.0.1", info["port"])
    return info if other and Path(other).resolve() == edit_dir.resolve() else None


def existing_preview(host: str, port: int) -> str | None:
    """edit_dir of a preview already answering on host:port, if any."""
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with urllib.request.urlopen(f'http://{url_host}:{port}/api/health', timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("edit_dir") if data.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Local editing preview with playback, timeline, marks, approval, and style controls.',
    )
    parser.add_argument("--edit-dir", required=True, help='session edit/ folder beside the footage', dest='edit_dir')
    parser.add_argument("--port", type=int, default=None,
                        help=f'port (default: {DEFAULT_PORT}; without --port, reuse a running preview for this edit)', dest='port')
    parser.add_argument("--host", default="127.0.0.1",
                        help='address (default: 127.0.0.1, this computer only); other values expose the preview to the network and require a token link', dest='host')
    parser.add_argument("--open", action="store_true", help="Open the browser on startup", dest='open')
    parser.add_argument("--catalog", default=None, help='alternative style catalog for tests (default: helpers/style_catalog.json)', dest='catalog')
    parser.add_argument("--verbose", action="store_true", help='log every request in the terminal', dest='verbose')
    args = parser.parse_args()
    utf8_console()
    port_given = args.port is not None
    if not port_given:
        args.port = DEFAULT_PORT

    edit_dir = Path(args.edit_dir).expanduser()
    if not edit_dir.is_absolute():
        edit_dir = Path.cwd() / edit_dir
    if edit_dir.exists() and not edit_dir.is_dir():
        print(f'Error: {edit_dir} is not a directory.', file=sys.stderr)
        return 2
    problem = check_writable(edit_dir)
    if problem:
        print(f'Error: {problem}', file=sys.stderr)
        return 2

    def fail(message: str) -> int:
        print(f'Error: {message}', file=sys.stderr)
        write_server_info(edit_dir, {"status": "error", "error": message, "edit_dir": str(edit_dir.resolve()), "pid": os.getpid()})
        return 2

    if not (PREVIEW_DIR / "index.html").is_file():
        return fail(f'Preview interface not found in {PREVIEW_DIR}. Reinstall the skill.')
    catalog_path = Path(args.catalog).expanduser().resolve() if args.catalog else STYLE_CATALOG_PATH
    if args.catalog and not catalog_path.is_file():
        return fail(f'Catalog not found: {catalog_path}')
    if not port_given:
        running = reusable_preview(edit_dir)
        if running:
            log(f"This edit's preview is already running at {running.get('url')}")
            write_server_info(edit_dir, {**running, "reused": True})
            if args.open and running.get("url"):
                webbrowser.open(str(running["url"]))
            return 0

    session = Session(edit_dir, catalog_path, args.host, args.port)
    handler = type("BoundHandler", (Handler,), {"session": session})

    httpd = None
    port = args.port
    for attempt in range(20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), handler)
            break
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), getattr(errno, "WSAEACCES", -1)):
                return fail(f'Could not start the server on {args.host}:{port}: {exc}')
            other = existing_preview(args.host, port)
            if other and Path(other).resolve() == session.edit_dir:
                url = f"http://{('127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host)}:{port}"
                log(f"This edit's preview is already running at {url}")
                write_server_info(edit_dir, {"status": "running", "url": url, "port": port, "host": args.host,
                                             "edit_dir": str(session.edit_dir), "reused": True})
                if args.open:
                    webbrowser.open(url)
                return 0
            port += 1
    if httpd is None:
        return fail(f'ports {args.port} to {port - 1} are occupied. Use --port to select another port.')

    httpd.daemon_threads = True
    httpd.verbose = args.verbose  # type: ignore[attr-defined]
    port = httpd.server_port   # --port 0 asks the system for a free port.
    session.port = port
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f'http://{shown_host}:{port}'
    note = f' (port {args.port} was occupied)' if args.port and port != args.port else ""
    info = {"status": "running", "url": url, "port": port, "host": args.host, "pid": os.getpid(),
            "edit_dir": str(session.edit_dir), "started_at": now_iso_ms()}
    if not session.loopback:
        lan = [ip for ip in lan_addresses()] or [shown_host]
        url = f'http://{shown_host}:{port}/?t={session.token}'
        info["url"] = url
        info["lan_urls"] = [f'http://{ip}:{port}/?t={session.token}' for ip in lan]
    write_server_info(edit_dir, info)
    log(f'Preview running at {url}{note}')
    if not session.loopback:
        log(f'Warning: this preview is accessible on the network ({args.host}). Devices on the same network with this link can view and approve the video. Use only a trusted network and stop with Ctrl+C when finished.')
        for lan_url in info["lan_urls"]:
            log(f'On a phone (same network): {lan_url}')
    log(f'Edit folder: {session.edit_dir}')
    if not catalog_path.is_file():
        log('Warning: helpers/style_catalog.json is missing; the Style tab stays empty until it exists.')
    log("Stop with Ctrl+C")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)  # a killed server still marks server.json as stopped
    except (ValueError, OSError, AttributeError):
        pass
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("Preview stopped.")
    finally:
        httpd.server_close()
        write_server_info(edit_dir, {**info, "status": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
