#!/usr/bin/env python3
"Build a sound plan whose peaks align with visual events.\n\nRead event lists, an events object, or the project's suggested_sounds.json and write a mix plan containing sound, start, gain, and measured peak alignment. Event time is the movement's impact frame; an optional sound overrides the event type. Optional level, force, and duration fields control emphasis, collision handling, and sustained effects. Use only the current English field names shown in --help.\n\nApply minimum spacing, retaining stronger events, and respect the selected maximum. Animation mode normally permits one sound per animation subject to spacing; speech mode uses a sparse plan. Explicit forced events remain even near another sound. Defaults are calibrated, not permission to obscure narration.\n\nProject mode reads suggested events, copies chosen sounds into project/assets/sfx, and updates project.json sounds. Rebuild with build.py compose before rendering. Standalone mode writes sound_plan.json and can mix it into a specified video.\n\nUsage: uv run --directory ENGINE python helpers/sound_plan.py --project PROJECT\nUse --types to inspect supported events and --measure FILE.wav for duration, peak, and attack. Built-in and CC0 effects remain inside the engine; personal-library audio stays outside the installed skill. Preserve source rights and original video files."
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit('numpy is missing. Run this helper with the locked engine environment.')

SKILL = Path(__file__).resolve().parent.parent
WOVEN = SKILL / "assets" / "sfx" / "woven"
KIT = Path(os.environ.get("EDITONNY_SFX_KIT") or SKILL / "assets" / "sfx")
LIBRARY = Path(os.environ.get("EDITONNY_EFFECTS_LIBRARY")
           or Path.home() / ".editonny" / "effects").expanduser()

VOICE_REF_LUFS = -14.0      # Table levels assume speech near -14 LUFS.
# Actual sample peaks measured in calibration: prominent effects near -12 dBFS,
# subtle effects near -18 dBFS, with speech at -14 LUFS.
LEVELS = {"accent": -12.0, "strong": -12.0, "medium": -15.0, "subtle": -18.0, "weak": -18.0,
          "bottom": -20.0}
GAIN_MAX_DB = 12.0

# Type: usage, preferred sounds, peak dBFS, alignment, tail seconds after anchor, aliases.
TYPES_VALUE = {'pop': ('text, icon, card, or bubble appears',
         ['woven:pop-hand', 'kit:pop'],
         -18.5,
         'peak',
         0.8,
         []),
 'whoosh': ('an element slides or enters or exits quickly',
            ['woven:fast-whoosh', 'kit:whoosh'],
            -17.5,
            'peak',
            0.8,
            []),
 'large-whoosh': ('major scene change: full-screen to framed, camera turn, or '
                  'whole-panel entrance',
                  ['woven:swish-whoosh-large', 'woven:deep-whoosh'],
                  -12.5,
                  'peak',
                  0.8,
                  []),
 'impact': ('strong landing: stamp, final number, or large word',
            ['woven:bass-impact', 'kit:bass_drop'],
            -11.5,
            'peak',
            0.8,
            []),
 'reveal': ('major reveal: logo, product, or closing panel; use sparingly',
            ['woven:base-drop', 'kit:metal_hit'],
            -11.5,
            'peak',
            1.2,
            []),
 'hit': ('gentle impact or fit: stacking item or connecting part',
         ['woven:dull-thud'],
         -14.5,
         'peak',
         0.6,
         []),
 'scratch': ('an item is crossed out, cancelled, or rejected',
             ['woven:record-needle-scratch', 'woven:sideways-record-needle'],
             -12,
             'peak',
             0.8,
             []),
 'notification': ('notification, sale confirmation, or incoming message',
                  ['woven:notification-ding', 'kit:notification'],
                  -12.5,
                  'peak',
                  0.9,
                  []),
 'message': ('conversation bubble, final button, or chat reply',
             ['woven:message-pop-reply'],
             -15.5,
             'peak',
             0.8,
             []),
 'glitch': ('screen activation, AI entrance, or digital failure',
            ['woven:glitch-logo', 'kit:data'],
            -13.5,
            'peak',
            0.7,
            []),
 'click': ('cursor click or finger tap',
           ['woven:mouse-click-2', 'woven:mouse-click-1', 'kit:mouse_click'],
           -18,
           'peak',
           0.5,
           []),
 'typing': ('typing on screen; set duration to match the typing interval',
            ['woven:keyboard-typing-realistic', 'kit:keyboard'],
            -22,
            'start',
            1.0,
            []),
 'tick': ('checked item or counting',
          ['woven:tick-sound', 'kit:tick'],
          -19,
          'peak',
          0.4,
          []),
 'beep': ('short interface beep', ['woven:beep-short'], -19, 'peak', 0.5, []),
 'money': ('coins, money, or price reveal',
           ['woven:several-coins', 'kit:cash', 'woven:money-chalking'],
           -15,
           'peak',
           0.9,
           []),
 'camera': ('photo or screenshot',
            ['woven:camera-shutter-1-shot', 'kit:click'],
            -15,
            'peak',
            0.5,
            []),
 'rise': ('suspense rising toward a reveal; the peak is at the end',
          ['kit:rise', 'kit:metal_riser'],
          -15,
          'peak',
          0.3,
          []),
 'success': ('success, achieved target, or completed task',
             ['kit:success'],
             -15,
             'peak',
             0.9,
             []),
 'error': ('mistake or warning', ['kit:error'], -15, 'peak', 0.8, []),
 'surprise': ('surprised reaction; use sparingly',
              ['woven:female-gasp', 'woven:male-gasp'],
              -17,
              'peak',
              0.6,
              []),
 'applause': ('achievement or story ending; use sparingly',
              ['woven:cheers-and-applause', 'woven:applause'],
              -22,
              'start',
              2.0,
              [])}


def normal_value(txt: str) -> str:
    t = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode().lower().strip()
    return t.replace(" ", "-").replace("_", "-")


TYPE_NAMES = {}
for _type, _d in TYPES_VALUE.items():
    TYPE_NAMES[normal_value(_type)] = _type
    for _a in _d[5]:
        TYPE_NAMES.setdefault(normal_value(_a), _type)


def error_value(msg: str, code: int = 2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def needs_ffmpeg():
    for b in ("ffmpeg", "ffprobe"):
        if not shutil.which(b):
            error_value(f'not found: {b}. Install FFmpeg (on macOS: brew install ffmpeg).')


# ------------------------------------------------------------------ sound locations
def kit_index() -> dict:
    p = KIT / "sfx.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def library_index() -> dict:
    p = LIBRARY / "effects.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("effects", {})
    except Exception:
        return {}


_LIBRARY_ROTATION: dict = {}


def resolve_sound(ref: str) -> Path | None:
    'Resolve a Woven ID, kit name, library ID/category, plain name, or path to a file, or None.'
    ref = str(ref).strip()
    p = Path(os.path.expanduser(ref))
    if p.suffix.lower() in (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aif", ".aiff") and p.exists():
        return p.resolve()
    source_prefix, _, name_value = ref.partition(":")
    if not name_value:
        source_prefix, name_value = "", ref
    source_prefix = source_prefix.lower()
    if source_prefix in ("", "woven"):
        w = WOVEN / f'{name_value}.wav'
        if w.exists():
            return w
        if source_prefix == "woven":
            return None
    if source_prefix in ("", "kit"):
        k = kit_index().get(name_value)
        if k and (KIT / k["file"]).exists():
            return KIT / k["file"]
        if source_prefix == "kit":
            return None
    if source_prefix in ("", "library"):
        ind = library_index()
        if name_value in ind and (LIBRARY / ind[name_value]["file"]).exists():
            return LIBRARY / ind[name_value]["file"]
        cat, _, sub = name_value.partition(":")
        findings = [k for k, v in sorted(ind.items()) if v.get("category") == cat
                   and (not sub or normal_value(sub) in normal_value(v.get("name", "")))]
        if findings:   # Rotate through variants of the same category on successive calls.
            i = _LIBRARY_ROTATION.get(name_value, 0)
            _LIBRARY_ROTATION[name_value] = i + 1
            return LIBRARY / ind[findings[i % len(findings)]]["file"]
    return None


def id_of_file(p: Path) -> str:
    try:
        if p.parent == WOVEN:
            return "woven:" + p.stem
        if KIT in p.parents:
            for name_value, v in kit_index().items():
                if (KIT / v["file"]).resolve() == p.resolve():
                    return "kit:" + name_value
        if LIBRARY in p.parents:
            return "library:" + p.stem
    except Exception:
        pass
    return str(p)


# ------------------------------------------------------------------ measure sound
_MEASUREMENTS: dict = {}


def emphasis_point(m: "np.ndarray", sr: int) -> int:
    'Perceived attack: the first instant within 1 dB of the maximum. Repeated similar peaks can place the absolute maximum late; align visible motion to the start of the strong interval.'
    if not len(m):
        return 0
    w = max(1, int(sr * 0.005))
    n = len(m) // w
    if n == 0:
        return int(m.argmax())
    blocks_value = m[: n * w].reshape(n, w).max(axis=1)
    k = int(np.nonzero(blocks_value >= blocks_value.max() * 10 ** (-1 / 20))[0][0])
    lo = k * w
    return lo + int(m[lo: lo + 2 * w].argmax())


def measure_value(p: Path) -> dict:
    'Duration, native-rate peak time and level across channels, and sound onset.'
    key = str(p)
    if key in _MEASUREMENTS:
        return _MEASUREMENTS[key]
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=sample_rate,channels", "-of", "csv=p=0", str(p)],
                          capture_output=True, text=True).stdout.strip().split(",")
    if len(info) < 2 or not info[0]:
        error_value(f'could not read sound {p}')
    sr, ch = int(info[0]), int(info[1])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    a = a[: len(a) // ch * ch].reshape(-1, ch)
    m = np.abs(a).max(axis=1) if len(a) else np.zeros(1)
    i = emphasis_point(m, sr)
    pk = float(m.max()) or 1e-6
    peak_db = 20 * math.log10(pk)
    w = max(1, int(sr * 0.005))            # Onset: the first five-millisecond window within 35 dB of the peak.
    n = len(m) // w
    rms = np.sqrt((a[: n * w].astype(np.float64) ** 2).mean(axis=1).reshape(n, w).mean(axis=1)) if n else np.zeros(1)
    above_value = np.nonzero(20 * np.log10(rms + 1e-12) > peak_db - 35)[0]
    start_value = float(above_value[0] * w / sr) if len(above_value) else 0.0
    peak_s, origin_value = i / sr, "measured"
    cur = anchor_curated(p)
    if cur is not None and 0 <= cur < len(a) / sr:   # Bundled and library sounds already have a selected emphasis point.
        peak_s, origin_value = cur, "curated"
    nb = len(m) // w
    blocks_value = m[: nb * w].reshape(nb, w).max(axis=1) if nb else m[:1]
    _MEASUREMENTS[key] = dict(dur=round(len(a) / sr, 4), peak_s=round(peak_s, 4), peak_db=round(peak_db, 2),
                         start_s=round(min(start_value, peak_s), 4), point=origin_value, sr=sr, channels=ch,
                         _blocks=blocks_value, _block_s=w / sr)
    return _MEASUREMENTS[key]


def peak_in_interval(md: dict, t0: float, t1: float) -> float:
    'Peak dBFS of only the file interval that will play.'
    b, bs = md["_blocks"], md["_block_s"]
    i0, i1 = int(t0 / bs), max(int(t0 / bs) + 1, int(math.ceil(t1 / bs)))
    v = float(b[i0:i1].max()) if len(b[i0:i1]) else 10 ** (md['peak_db'] / 20)
    return 20 * math.log10(max(v, 1e-6))


def anchor_curated(p: Path):
    'Read anchor_s from the bundled kit or personal-library manifest when the file belongs to it.'
    try:
        rp = p.resolve()
        if KIT.resolve() in rp.parents:
            for v in kit_index().values():
                if (KIT / v["file"]).resolve() == rp:
                    return float(v["anchor_s"]) if v.get("anchor_s") is not None else None
        if LIBRARY.resolve() in rp.parents:
            for v in library_index().values():
                if (LIBRARY / v["file"]).resolve() == rp:
                    return float(v["anchor_s"]) if v.get("anchor_s") is not None else None
    except (OSError, ValueError, KeyError):
        return None
    return None


def media_duration(p: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        error_value(f'could not read duration of {p}')


def loudness(p: str, af: str = "") -> dict:
    process = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", p, "-vn", "-af",
                        af + "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    if process.returncode:
        error_value(f'could not measure loudness of {p}')
    r = process.stderr
    try:
        return json.loads(r[r.rindex("{"): r.rindex("}") + 1])
    except ValueError:
        error_value(f'could not measure loudness of {p}')


def chain_loudnorm(wav: str, target_value: float = -14.0, tp: float = -1.0) -> tuple[str, dict]:
    'Normalize to -14 LUFS in two linear passes. If true peak would exceed -1 dBTP, apply a latency-compensated peak limiter before normalization.'
    j = loudness(wav)
    gain_value = target_value - float(j["input_i"])
    pre = ""
    if float(j["input_tp"]) + gain_value > tp - 0.2:
        lim = 10 ** ((tp - gain_value - 1.0) / 20)
        pre = f'alimiter=limit={max(0.0625, min(1.0, lim)):.5f}:attack=5:release=60:level=false:latency=1,'
        j = loudness(wav, pre)
    af = (pre + f"loudnorm=I={target_value}:TP={tp}:LRA=11:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true:print_format=json,aresample=48000")
    return af, j

def finalize(video: str, wav: str, output_value: str) -> tuple[dict, str, dict]:
    'Copy the video stream, mux normalized audio, then measure encoded AAC true peaks. If they exceed -0.5 dBTP, retry with a lower ceiling while preserving -14 LUFS integrated loudness.'
    from common import ensure_distinct_output
    ensure_distinct_output(output_value, [video, wav])
    tp = -1.5
    for attempt_value in range(2):
        af, j = chain_loudnorm(wav, tp=tp)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", video, "-i", wav, "-map", "0:v:0",
                            "-map", "1:a:0", "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "256k",
                            "-movflags", "+faststart", output_value], capture_output=True, text=True)
        if r.returncode:
            error_value('FFmpeg failed to write the final video:' + r.stderr[-1500:])
        type_value = "linear"
        try:
            type_value = json.loads(r.stderr[r.stderr.rindex("{"): r.stderr.rindex("}") + 1]).get("normalization_type",
                                                                                           "linear")
        except ValueError:
            pass
        end_value = loudness(output_value)
        if math.isfinite(float(end_value['input_tp'])) and float(end_value["input_tp"]) <= -1.0:
            break
        tp = round(tp - (float(end_value["input_tp"]) + 0.8), 2)
    if not math.isfinite(float(end_value['input_tp'])) or float(end_value['input_tp']) > -1.0:
        error_value('Encoded true peak remains above -1.0 dBTP after retries; output is not a validated final')
    return j, type_value, end_value



def audio_mono(p: str, sr: int = 16000) -> "np.ndarray":
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le",
                          "-acodec", "pcm_f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def deviation_synchronization(ref: str, out: str, windows_value: int = 10, sr: int = 16000) -> float:
    'Maximum output-to-input audio shift in milliseconds, measured over one-second speech windows.'
    a, b = audio_mono(ref, sr), audio_mono(out, sr)
    n = min(len(a), len(b))
    w, L = sr, int(0.05 * sr)
    if n < 3 * w:
        return 0.0
    en = [float((a[i:i + w] ** 2).mean()) for i in range(L, n - w - L, w)]
    idx = sorted(range(len(en)), key=lambda k: -en[k])[:windows_value]
    worst_value = 0.0
    for k in idx:
        i = L + k * w
        x = a[i:i + w]
        seg = b[i - L:i + w + L]
        c = np.correlate(seg, x, mode="valid")
        lag = (int(c.argmax()) - L) / sr * 1000
        worst_value = max(worst_value, abs(lag))
    return round(worst_value, 2)


# ------------------------------------------------------------------ assemble the plan
def level_of_event(ev: dict, default_value: float) -> float:
    'Use the event level, then its suggested dB value, then the event-type default.'
    nv = ev.get("level")
    if nv is None and ev.get("db") is not None:
        nv = ev["db"]
    if nv is None:
        return float(default_value)
    if isinstance(nv, (int, float)):
        return float(nv)
    k = normal_value(nv)
    for name_value, v in LEVELS.items():
        if normal_value(name_value) == k:
            return v
    try:
        return float(str(nv).replace(",", "."))
    except ValueError:
        error_value(f"level '{nv}' does not exist. Use accent, medium, subtle, or numeric dBFS such as -15")


def assemble(events_value: list, video_duration: float, mode_value: str, max_sounds: int | None, space_value: float, vary: bool,
           adjustment_db: float = 0.0) -> dict:
    rotation_value: dict = {}
    items_value, discarded = [], []
    for n, ev in enumerate(events_value, 1):
        if "t" not in ev:
            error_value(f'event {n} lacks "t", the visual-event time in seconds: {ev}')
        t = float(ev["t"])
        type_value = TYPE_NAMES.get(normal_value(ev.get("type", ""))) if ev.get("type") else None
        if ev.get("type") and not type_value and not ev.get("sound"):
            error_value(f"type '{ev['type']}' (event {n}) does not exist. Types: {', '.join(TYPES_VALUE)}. Use --types to inspect each event's purpose")
        if not type_value and not ev.get("sound"):
            error_value(f'event {n} requires "type" or "sound": {ev}')
        base = TYPES_VALUE.get(type_value) if type_value else ("selected sound", [], -14, "peak", 0.8, [])
        if t < 0 or t > video_duration:
            discarded.append(dict(event_time=t, type=type_value or "", reason='outside the video'))
            continue
        if ev.get("sound"):
            file_value = resolve_sound(ev["sound"])
            if not file_value:
                error_value(f"sound not found: '{ev['sound']}' (event {n}). Use woven:ID, kit:NAME, library:ID, or a path.")
        else:
            cands = [c for c in (resolve_sound(s) for s in base[1]) if c]
            if not cands:
                error_value(f"no sound of type '{type_value}' is installed ({', '.join(base[1])}). Check this skill’s assets/sfx/woven and assets/sfx folders.")
            k = rotation_value.get(type_value, 0) if vary else 0
            rotation_value[type_value] = k + 1
            file_value = cands[k % len(cands)]
        md = measure_value(file_value)
        align = ev.get("align", base[3])
        anchor_value = md['peak_s'] if align == "peak" else md['start_s']
        start_value = t - anchor_value
        offset = 0.0
        if start_value < 0:                      # An early event trims the beginning of the sound.
            offset, start_value = -start_value, 0.0
        tail = float(ev["dur"]) - (anchor_value - md['start_s']) if ev.get("dur") else base[4]
        end_file = min(md["dur"], anchor_value + max(0.1, tail))
        dur = end_file - offset
        dur = min(dur, video_duration - start_value)
        if dur <= 0.02:
            discarded.append(dict(event_time=t, type=type_value or "", reason='insufficient space before the video ends'))
            continue
        trimmed = offset + dur < md["dur"] - 0.01
        fade = round(min(0.35, max(0.05, (offset + dur - anchor_value) * 0.5)), 3) if trimmed else 0.0
        level_value = level_of_event(ev, base[2])
        played_peak = peak_in_interval(md, offset, offset + dur)   # Set the level using the peak that actually plays.
        gain_value = max(-60.0, min(GAIN_MAX_DB, level_value - played_peak + adjustment_db))
        peak_tl = start_value + (md['peak_s'] - offset)
        if peak_tl > start_value + dur:          # Start alignment can leave the original file peak outside the played interval.
            peak_tl = t
        items_value.append(dict(event_time=round(t, 3), type=type_value or "custom", id=id_of_file(file_value), file=str(file_value),
                          start=round(start_value, 4), offset=round(offset, 4), dur=round(dur, 4),
                          peak=round(peak_tl, 4), peak_file_s=md['peak_s'], peak_interval_db=round(played_peak, 2),
                          align=align, point=md["point"], level_db=round(level_value, 1), gain_db=round(gain_value, 2),
                          volume=round(10 ** (gain_value / 20), 4), fade_out=fade, forced=bool(ev.get("force")),
                          note=str(ev.get("note") or ev.get("piece") or "")))

    # Density: keep forced events first, then strongest events; reject neighbors that are too close.
    if max_sounds is None:
        # Each animation can carry its own sound; minimum spacing governs the density.
        max_sounds = max(5, round(video_duration / 15)) if mode_value == "speech" else max(5, len(items_value))
    order_value = sorted(items_value, key=lambda x: (not x["forced"], -x['level_db'], x['event_time']))
    retained = []
    for it in order_value:
        near_value = [k for k in retained if abs(k['event_time'] - it['event_time']) < space_value]
        if it["forced"]:
            if any(abs(k['event_time'] - it['event_time']) < 0.05 for k in near_value):
                discarded.append(dict(event_time=it['event_time'], type=it["type"], reason="repeated on the same frame"))
                continue
            retained.append(it)
            continue
        if near_value:
            discarded.append(dict(event_time=it['event_time'], type=it["type"],
                                    reason=f"too close to the sound at {near_value[0]['event_time']:.2f} s (retain the stronger sound)"))
            continue
        retained.append(it)
    free_value = [x for x in retained if not x["forced"]]
    remainder = len(retained) - max_sounds
    if remainder > 0:   # Above the maximum, remove the weakest events while preserving forced events.
        for it in sorted(free_value, key=lambda x: (x['level_db'], -x['event_time']))[:remainder]:
            retained.remove(it)
            discarded.append(dict(event_time=it['event_time'], type=it["type"],
                                    reason=f'exceeded the maximum of {max_sounds} sounds (remove weaker sounds)'))
    retained.sort(key=lambda x: x['event_time'])
    for i, it in enumerate(retained, 1):
        it["n"] = i
    discarded.sort(key=lambda x: x['event_time'])
    return dict(version=1, created_by="helpers/sound_plan.py (editonny-engine)", video_duration=round(video_duration, 3),
                mode=mode_value, min_spacing_s=space_value, max_sounds=max_sounds, voice_adjustment_db=round(adjustment_db, 2),
                sounds=[{k: it[k] for k in ["n"] + [c for c in it if c != "n"]} for it in retained],
                discarded=discarded)


# ------------------------------------------------------------------ ffmpeg
def sound_filter(sounds_value: list, first_input: int, files_value: list) -> tuple[list, list]:
    'Return filter parts and labels for positioned 48 kHz stereo sounds.'
    uses = {}
    for s in sounds_value:
        uses[s["file"]] = uses.get(s["file"], 0) + 1
    rot_file, parts_value = {}, []
    for a, count_value in uses.items():
        idx = first_input + files_value.index(a)
        if count_value == 1:
            rot_file[a] = [f'[{idx}:a]']
        else:
            labs = [f'[f{idx}_{k}]' for k in range(count_value)]
            parts_value.append(f"[{idx}:a]asplit={count_value}{''.join(labs)};")
            rot_file[a] = labs
    labels = []
    for i, s in enumerate(sounds_value):
        src = rot_file[s["file"]].pop(0)
        ms = int(round(s["start"] * 1000))
        fade = (f",afade=t=out:st={max(0.0, s['dur'] - s['fade_out']):.4f}:d={s['fade_out']:.4f}"
                if s["fade_out"] > 0 else "")
        parts_value.append(f"{src}atrim=start={s['offset']:.4f}:end={s['offset'] + s['dur']:.4f},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume={s['gain_db']:.2f}dB{fade},adelay={ms}|{ms}[s{i}];")
        labels.append(f'[s{i}]')
    return parts_value, labels


def render_music_sfx(plan_value: dict, dur: float, output_wav: str):
    'Effects only, without voice, for checking peak placement.'
    sounds_value = plan_value["sounds"]
    files_value = sorted({s["file"] for s in sounds_value})
    parts_value, labels = sound_filter(sounds_value, 1, files_value)
    fc = "".join(parts_value) + f"[0:a]{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:duration=first[out]"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", f'{dur:.4f}', "-i",
           "anullsrc=r=48000:cl=stereo"]
    for a in files_value:
        cmd += ["-i", a]
    cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "pcm_f32le", output_wav]
    subprocess.run(cmd, check=True)


def audit_peaks(plan_value: dict, dur: float) -> dict:
    if not plan_value["sounds"]:
        return dict(checked=0)
    with tempfile.TemporaryDirectory() as td:
        w = os.path.join(td, "sfx.wav")
        render_music_sfx(plan_value, dur, w)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", w, "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                             capture_output=True).stdout
    a = np.abs(np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)).max(axis=1)
    sr = 48000
    offsets = []
    for s in plan_value["sounds"]:
        if s["align"] != "peak" or s.get("point") == "curated":
            continue
        lo, hi = int(max(0, (s['peak'] - 0.08)) * sr), int(min(len(a), (s['peak'] + 0.08) * sr))
        if hi <= lo:
            continue
        j = lo + emphasis_point(a[lo:hi], sr)
        offsets.append((abs(j / sr - s['peak']) * 1000, s['event_time']))
    worst_value = max(offsets) if offsets else (0.0, 0.0)
    return dict(checked=len(offsets), deviation_max_ms=round(worst_value[0], 1), at_event=worst_value[1])


def video_duration(p: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                          "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return media_duration(p)


def apply_value(plan_value: dict, video: str, output_value: str):
    sounds_value = plan_value["sounds"]
    files_value = sorted({s["file"] for s in sounds_value})
    parts_value, labels = sound_filter(sounds_value, 1, files_value)
    dv = video_duration(video)   # Match audio length exactly to the video, without an extra sample.
    fc = ("".join(parts_value) + f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad,atrim=end={dv:.6f}[voz];[voz]{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:duration=first[out]")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "mix.wav")
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", video]
        for a in files_value:
            cmd += ["-i", a]
        cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "pcm_f32le", tmp]
        subprocess.run(cmd, check=True)
        j, type_value, end_value = finalize(video, tmp, output_value)
    return j, type_value, end_value


def read_project(directory: Path) -> dict:
    'Read project.json, its cut edit folder, cut duration, and closing holds or cards.'
    pj = directory / "project.json"
    if not pj.exists():
        error_value(f'could not find {pj}. --project must point to the generated project folder containing project.json')
    try:
        cfg = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        error_value(f'{pj} is not valid JSON: {e}')
    edit = (directory / cfg.get("edit", "../edit")).resolve()
    from project_contract import managed_config
    managed = managed_config(edit)
    if managed and not managed['workflow']['audio']['effects']:
        error_value('Sound effects were not confirmed; propose a settings revision first')
    vdur = None
    tl = edit / "cut_timeline.json"
    if tl.exists():
        try:
            vdur = float(json.loads(tl.read_text(encoding="utf-8"))["duration"])
        except (ValueError, KeyError):
            vdur = None
    cf = cfg.get("endCard") or {}
    tail = float(cfg.get("endHold") or 0) + (float(cf.get("seconds", 1.8)) if cf.get("enabled") else 0.0)
    return dict(cfg=cfg, file=pj, edit=edit, vdur=vdur, tail=tail, video=edit / cfg.get("video", "cut.mp4"))


def license_of_sound(src: Path) -> tuple[str, Path | None]:
    'Return the license line and license file to accompany a copied sound.'
    try:
        rp = src.resolve()
        if rp.parent == WOVEN.resolve():
            return 'Woven SFX, CC0 1.0 (public domain); LICENSE-woven.md', WOVEN / "LICENSE.md"
        if KIT.resolve() in rp.parents:
            for v in kit_index().values():
                if (KIT / v["file"]).resolve() == rp:
                    file_value = KIT / v["license_file"] if v.get("license_file") else None
                    return (f"{v.get('license', '?')} ({v.get('source', 'editonny-engine sound kit')})",
                            file_value if file_value and file_value.exists() else None)
        if LIBRARY.resolve() in rp.parents:
            for v in library_index().values():
                if (LIBRARY / v["file"]).resolve() == rp:
                    return f"{v.get('license', '?')} ({v.get('source', 'effects library')})", None
    except (OSError, KeyError):
        pass
    return 'imported file: verify commercial-use rights', None


def apply_in_project(plan_value: dict, directory: Path, proj: dict, window_value: list | None) -> list:
    'Copy sounds to project assets/sfx and update project.json sounds. Replace only plan-generated cues within the current sample window; preserve manually authored cues.'
    from common import content_hash
    dest = directory / "assets" / "sfx"
    dest.mkdir(parents=True, exist_ok=True)
    licenses: dict = {}
    new_value = []
    for s in plan_value["sounds"]:
        src = Path(s["file"])
        # Distinct sounds can share a filename; never overwrite or reuse a different sound another cue may use.
        digest = content_hash(src)
        names = [src.name] + [f'{src.stem}_{digest[:10]}{f"_{k}" if k else ""}{src.suffix}' for k in range(100)]
        name_value = next(n for n in names if not (dest / n).exists() or content_hash(dest / n) == digest)
        target_value = dest / name_value
        if not target_value.exists():
            shutil.copy2(src, target_value)
        lic, file_lic = license_of_sound(src)
        licenses[name_value] = f'{name_value}: {id_of_file(src)} · {lic}'
        if file_lic is not None:
            copy_value = dest / ("LICENSE-woven.md" if file_lic.parent == WOVEN else f'licenses/{file_lic.name}')
            copy_value.parent.mkdir(parents=True, exist_ok=True)
            if not copy_value.exists():
                shutil.copy2(file_lic, copy_value)
        at_value = round(s['peak'], 3)
        new_value.append({"file": f'assets/sfx/{name_value}', "at": at_value,
                      "sourcePeak": round(at_value - s["start"] + s["offset"], 4),
                      "sourceStart": s["offset"], "dur": s["dur"], "volume": s["volume"],
                      "db": round(s['level_db'] + plan_value.get("voice_adjustment_db", 0.0), 1), "fade": s["fade_out"],
                      "type": s["type"], "note": s["note"], 'plan': True})
    (dest / "LICENSES.txt").write_text(
        'Project sound credits generated by editonny-engine sound_plan.py\n'
        + "\n".join(licenses[k] for k in sorted(licenses)) + "\n", encoding="utf-8")

    def time_value(x):
        try:
            return float(x.get("at"))
        except (TypeError, ValueError):
            return None

    def remaining(x):
        if not x.get('plan'):
            return True                      # Preserve manually specified sounds.
        t = time_value(x)
        return window_value is not None and t is not None and not (window_value[0] - 0.01 <= t <= window_value[1] + 0.01)
    old_value = proj["cfg"].get("sounds") or []
    retained = [x for x in old_value if remaining(x)]
    cfg = dict(proj["cfg"])
    cfg["sounds"] = sorted(retained + new_value, key=lambda x: (time_value(x) is None, time_value(x) or 0.0))
    proj["file"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return retained


# ------------------------------------------------------------------ commands
def cmd_types():
    print('Event types: purpose, sounds, and peak level')
    for type_value, d in TYPES_VALUE.items():
        ok = [s for s in d[1] if resolve_sound(s)]
        missing_value = [s for s in d[1] if s not in ok]
        print(f'- {type_value}: {d[0]}')
        print(f"    sounds: {', '.join(ok) or 'none installed'}" + (f" (missing: {', '.join(missing_value)})" if missing_value else ""))
        print(f"    level: {d[2]} dBFS; align with {('peak' if d[3] == 'peak' else 'beginning')}; aliases: {', '.join(d[5])}")
    print('Preset peak levels: accent -12, medium -15, subtle -18 dBFS, with voice near -14 LUFS.')


def cmd_measure(file_value: str):
    p = resolve_sound(file_value)
    if not p:
        error_value(f'sound not found {file_value}')
    md = {k: v for k, v in measure_value(p).items() if not k.startswith("_")}
    print(json.dumps(dict(file=str(p), id=id_of_file(p), **md), ensure_ascii=False, indent=1))


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # Flush progress immediately, including redirected logs.
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='Plan sound effects so each sound peak aligns with its visual event.')
    ap.add_argument('events_value', metavar='EVENTS', nargs="*", help='visual-event JSON files; with --project, default to suggested_sounds.json and events.json in the project')
    ap.add_argument("--output", help='mix-plan JSON (default: sound_plan.json beside events or inside the project)', dest='output_value')
    ap.add_argument("--video", help='final video for duration and voice-level measurement', dest='video')
    ap.add_argument("--duration", type=float, help='video duration in seconds when --video is omitted', dest='duration_value')
    ap.add_argument("--mode", choices=["visuals", "speech"], default="visuals",
                    help='visuals: one sound per animation; speech: approximately five or six supporting sounds overall', dest='mode_value')
    ap.add_argument("--max", type=int, help='maximum sounds; visuals defaults to minimum-spacing limits, speech to about one every 15 seconds', dest='max')
    ap.add_argument("--spacing", type=float, default=0.65, help='minimum spacing between peaks in seconds (default 0.65)', dest='space_value')
    ap.add_argument("--vary", action="store_true", help="alternate sounds within the same type", dest='vary')
    ap.add_argument("--apply", metavar="VIDEO", help='mix effects into this video, preserving speech and normalizing to -14 LUFS', dest='apply_value')
    ap.add_argument("--video-output", help='output file for --apply', dest='video_output')
    ap.add_argument("--project", metavar="DIRECTORY", help='project folder; copy sounds into assets/sfx and update project.json sounds', dest='project')
    ap.add_argument("--types", action="store_true", help="list sound types and available sounds", dest='types_value')
    ap.add_argument("--measure", metavar="SOUND", help='measure sound duration, peak, and onset', dest='measure_value')
    a = ap.parse_args(argv)
    needs_ffmpeg()
    if a.types_value:
        return cmd_types()
    if a.measure_value:
        return cmd_measure(a.measure_value)
    proj = read_project(Path(a.project).expanduser().resolve()) if a.project else None
    files_value = [Path(x) for x in a.events_value]
    if proj and not files_value:
        directory = proj["file"].parent
        files_value = [x for x in (directory / "suggested_sounds.json", directory / "events.json") if x.exists()]
        if not files_value:
            error_value(f"could not find {directory / 'suggested_sounds.json'}. Run python3 build.py compose in the project first")
    if not files_value:
        ap.error('provide an events file or --project, or use --types / --measure')
    events_value, window_value = [], None
    for file_value in files_value:
        try:
            data_value = json.loads(file_value.read_text(encoding="utf-8"))
        except FileNotFoundError:
            error_value(f'could not find {file_value}')
        except json.JSONDecodeError as e:
            error_value(f'{file_value} is not valid JSON: {e}')
        # Also accept project-template suggested_sounds.json: {"window": [...], "sounds": [{"type", "at", ...}]}.
        listing = (data_value.get("events") or data_value.get("sounds") or []) if isinstance(data_value, dict) else data_value
        if isinstance(data_value, dict) and data_value.get("window"):
            window_value = [float(x) for x in data_value["window"]]
        if not isinstance(listing, list):
            error_value(f'{file_value}: unrecognized format. Use [{{"t": 1.23, "type": "pop"}}]')
        for e in listing:
            if isinstance(e, dict) and "t" not in e and "at" in e:
                e["t"] = e["at"]
        events_value += listing
        if proj:
            print(f'events from {file_value.name}: {len(listing)}')
    if not events_value:
        error_value('events file is empty; expected a list such as [{"t": 1.23, "type": "pop"}]')
    video_ref = a.video or a.apply_value or (str(proj["video"]) if proj and proj["video"].exists() else None)
    adjustment = 0.0
    if video_ref:
        dur = media_duration(video_ref)
        lv = float(loudness(video_ref)["input_i"])
        if lv > -70:
            adjustment = lv - VOICE_REF_LUFS
    elif a.duration_value:
        dur = a.duration_value
    else:
        dur = max(float(e.get("t", 0)) for e in events_value) + 2.0
        print(f'notice: without --video or --duration, assumed video duration {dur:.1f} s')
    if proj and not a.duration_value:
        # Project duration includes the cut, endHold, and end card.
        dur = (proj["vdur"] or dur) + proj["tail"]
    if proj and window_value and proj["vdur"] and (window_value[0] > 0.05 or window_value[1] < proj["vdur"] - 0.05):
        print(f'notice: suggested_sounds.json covers a sample ({window_value[0]:.2f} to {window_value[1]:.2f} seconds). Plan cues outside that window are preserved. For the complete video, run python3 build.py compose and retry')
    plan_value = assemble(events_value, dur, a.mode_value, a.max, a.space_value, a.vary, adjustment)
    chk = audit_peaks(plan_value, dur)
    plan_value["verification"] = chk
    if a.output_value:
        output_value = Path(a.output_value)
    elif proj:
        output_value = proj["file"].parent / "sound_plan.json"
    else:
        output_value = files_value[0].resolve().parent / "sound_plan.json"
    output_value.write_text(json.dumps(plan_value, ensure_ascii=False, indent=1))
    maximum_value = 'one per animation' if a.mode_value == "visuals" and a.max is None else str(plan_value["max_sounds"])
    print(f"{len(plan_value['sounds'])} sounds planned, {len(plan_value['discarded'])} discarded (mode {plan_value['mode']}, maximum {maximum_value}, spacing {plan_value['min_spacing_s']} s)")
    if adjustment:
        print(f'video voice level {VOICE_REF_LUFS + adjustment:.1f} LUFS: gains adjusted by {adjustment:+.1f} dB')
    for s in plan_value["sounds"]:
        print(f"  {s['n']:>2}. {s['event_time']:7.2f} s  {s['type']:<13} {s['id']:<32} peak {s['level_db']:>5.1f} dBFS; starts {s['start']:.3f}" + ('  (forced)' if s["forced"] else ""))
    for d in plan_value["discarded"]:
        print(f"  excluded: {d['event_time']:7.2f} s  {d['type']:<13} {d['reason']}")
    if chk.get("checked"):
        print(f"verification: {chk['checked']} measured peaks, maximum offset {chk['deviation_max_ms']} ms (event {chk['at_event']} s)")
    print(f'plan saved at {output_value}')
    if proj:
        retained = apply_in_project(plan_value, proj["file"].parent, proj, window_value)
        print(f"""project.json: {len(plan_value['sounds'])} plan sounds in "sounds"; files in assets/sfx, notices in assets/sfx/LICENSES.txt""" + (f'; {len(retained)} existing sounds preserved' if retained else ""))
        print('next: python3 build.py compose, then render')
    if a.apply_value:
        vs = a.video_output or str(Path(a.apply_value).with_name(Path(a.apply_value).stem + "_sfx.mp4"))
        if Path(vs).resolve() == Path(a.apply_value).resolve():
            error_value('output must differ from the input video')
        j, type_value, end_value = apply_value(plan_value, a.apply_value, vs)
        d_in, d_out = video_duration(a.apply_value), media_duration(vs)
        sync_value = deviation_synchronization(a.apply_value, vs)
        print(f'video with effects: {vs}')
        print(f"  duration {d_out:.3f} s (input {d_in:.3f} s) · final loudness {end_value['input_i']} LUFS, peak {end_value['input_tp']} dBTP (adjustment {type_value}) · voice alignment offset {sync_value} ms")
        if type_value != "linear":
            print('  notice: normalization became dynamic; verify that speech remains natural')


if __name__ == "__main__":
    main()
