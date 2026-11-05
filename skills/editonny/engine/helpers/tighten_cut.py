#!/usr/bin/env python3
'Tighten unintended pauses in an already rendered video without separating picture from sound.\n\nCut all layers on the same frame so captions, graphics, and effects retain their speech alignment. Reduce only pauses above the configured minimum to the requested remaining duration. Measure boundaries against the waveform, extend through weak consonants within the limit, and keep safety margins around words. Prefer the visually quietest part of each pause; an optional graphics-only render isolates animation changes. Avoid cutting the attack of a planned sound effect.\n\nUsage: uv run --directory ENGINE python helpers/tighten_cut.py FINAL_VIDEO --pause-max 0.15\nOptional controls include --without-graphics, --sfx, --voice, --trim, --output, --pause-min, --start, --threshold, --only-plan, and --crf. Use voice-only audio when music would obscure pause measurements.\n\nWrite a separate tightened video with normalized audio and a cut/time-map JSON. Explicitly protected content and intentional pauses take precedence over the default cleanup policy. Verify cut edges and synchronization before delivery.'
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit('numpy is missing. Run this helper with the locked engine environment.')

HOP = 0.01            # 10 ms envelope.
PRE0, POS0 = 0.06, 0.07   # Minimum padding after a word and before the next threshold edge.
POS1 = 0.04           # Padding before the next word's onset, including weak consonants.
WALK_VALUE = 0.12          # Maximum backward search while weak consonant energy remains.
FADE = 0.008          # Short splice fade prevents clicks.
SR_ENV = 16000


def error_value(msg: str, code: int = 2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def notice(msg: str):
    print('notice: ' + msg)


def run(cmd: list, text_value=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=text_value, **kw)
    if r.returncode:
        err = r.stderr if text_value else r.stderr.decode(errors="replace")
        error_value("FFmpeg failed:\n" + err[-2000:])
    return r


# ------------------------------------------------------------------ reading
def probe_value(p: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=index,codec_type,r_frame_rate,avg_frame_rate,width,height,duration,start_time,"
                        "sample_rate,channels,nb_frames,color_space,color_primaries,color_transfer,color_range"
                        ":format=duration", "-of", "json", p], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode:
        error_value(f'Could not open {p}')
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = float(d.get("format", {}).get("duration", 0) or 0)
    info = dict(format=fmt, v=v, a=a)
    if v:
        fr = Fraction(v.get("r_frame_rate") or v.get("avg_frame_rate") or "30/1")
        if fr <= 0:
            fr = Fraction(30, 1)
        info["fps"] = fr
        info["dur_v"] = float(v.get("duration") or fmt)
        info["v0"] = float(v.get("start_time") or 0)
        info["w"], info["h"] = int(v.get("width", 0)), int(v.get("height", 0))
    if a:
        info["dur_a"] = float(a.get("duration") or fmt)
        info["a0"] = float(a.get("start_time") or 0)
        info["sr"] = int(a.get("sample_rate", 48000))
        info["ch"] = int(a.get("channels", 2))
    return info


def audio_mono(p: str, sr: int = SR_ENV) -> "np.ndarray":
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le",
                          "-acodec", "pcm_f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def env_db(x: "np.ndarray", sr: int = SR_ENV, hop: float = HOP) -> "np.ndarray":
    w = int(sr * hop)
    n = len(x) // w
    return 10 * np.log10((x[: n * w].astype(np.float64).reshape(n, w) ** 2).mean(axis=1) + 1e-12)


def frames_value(p: str, width_value: int, height_value: int):
    'Stream small grayscale video frames without retaining the whole recording in memory.'
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", p, "-an", "-vf",
                             f'scale={width_value}:{height_value}:flags=area,format=gray', "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE)
    n = width_value * height_value
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        yield np.frombuffer(buf, dtype=np.uint8)
    proc.wait()


def activity(p: str, w: int, h: int, store_value: bool = False):
    'Measure frame-to-frame change; optionally retain 40-pixel thumbnails for join inspection.'
    lw = 96
    lh = max(2, int(round(lw * h / max(1, w) / 2)) * 2)
    act, mini, previous = [], [], None
    for f in frames_value(p, lw, lh):
        g = f.astype(np.int16)
        act.append(0.0 if previous is None else float(np.abs(g - previous).mean()))
        previous = g
        if store_value:
            mini.append(f.reshape(lh, lw)[::2, ::2].copy())
    return np.array(act), (np.stack(mini) if store_value and mini else None)


def load_sfx(p: str) -> list:
    "Protected intervals from each sound's attack through its peak plus 0.1 seconds."
    try:
        d = json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        error_value(f'could not read sound plan {p}: {e}')
    items_value = d.get("sounds", d.get("events", [])) if isinstance(d, dict) else d
    out = []
    for s in items_value:
        peak_value = s.get('peak', s.get("peak", s.get("t")))
        if peak_value is None:
            continue
        peak_value = float(peak_value)
        opening = float(s["start"]) if "start" in s else peak_value - 0.06
        out.append((max(opening, peak_value - 0.3) - 0.04, peak_value + 0.06))
    return out


def intervals_value(mask: "np.ndarray", hop: float = HOP) -> list:
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out.append((i * hop, j * hop))
            i = j
        else:
            i += 1
    return out


def lag_ms(a: "np.ndarray", b: "np.ndarray", t_a: float, t_b: float, middle: float = 0.25, search_value: float = 0.05,
           sr: int = SR_ENV) -> float | None:
    'Measure relative audio offset in milliseconds between two centered windows.'
    i, j, L, w = int(t_a * sr), int(t_b * sr), int(search_value * sr), int(middle * sr)
    x = a[i - w:i + w]
    y = b[j - w - L:j + w + L]
    if i - w < 0 or j - w - L < 0 or len(x) < 2 * w or len(y) < 2 * w + 2 * L or float((x ** 2).mean()) < 1e-7:
        return None
    c = np.correlate(y, x, mode="valid")
    return (int(c.argmax()) - L) / sr * 1000


def loudness(p: str, af: str = "") -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", p, "-vn", "-af",
                        af + "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding='utf-8', errors='replace').stderr
    try:
        return json.loads(r[r.rindex("{"): r.rindex("}") + 1])
    except ValueError:
        error_value(f'could not measure loudness of {p}')


def chain_loudnorm(wav: str, target_value: float = -14.0, tp: float = -1.0) -> tuple[str, dict]:
    'Normalize to -14 LUFS in two linear passes. Apply a latency-compensated peak limiter first if true peak would exceed -1 dBTP.'
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
    tp = -1.0
    for attempt_value in range(2):
        af, j = chain_loudnorm(wav, tp=tp)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", video, "-i", wav, "-map", "0:v:0",
                            "-map", "1:a:0", "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "256k",
                            "-movflags", "+faststart", output_value], capture_output=True, text=True, encoding='utf-8', errors='replace')
        if r.returncode:
            error_value('FFmpeg failed to write the final video:' + r.stderr[-1500:])
        type_value = "linear"
        try:
            type_value = json.loads(r.stderr[r.stderr.rindex("{"): r.stderr.rindex("}") + 1]).get("normalization_type",
                                                                                           "linear")
        except ValueError:
            pass
        end_value = loudness(output_value)
        if float(end_value["input_tp"]) <= -0.5:
            break
        tp = round(tp - (float(end_value["input_tp"]) + 0.8), 2)
    return j, type_value, end_value



# ------------------------------------------------------------------ plan
def create_plan(a, info: dict, det: "np.ndarray", mix_env: "np.ndarray", uses_voice: bool, act: "np.ndarray",
             protected_intervals: list, n_frames_value: int) -> dict:
    fps = float(info["fps"])
    fd = 1.0 / fps
    floor5, floor10, speech_value = (float(np.percentile(det, q)) for q in (5, 10, 90))
    thr = a.threshold_value if a.threshold_value is not None else max(floor5 + 10, speech_value - 30)
    weak_value = max(floor10 + 6, thr - 12)
    if speech_value - floor5 < 40 and a.threshold_value is None:
        notice(f'the difference between speech ({speech_value:.0f} dB) and background ({floor5:.0f} dB) is small. Trim pauses before adding music, or provide aligned voice-only audio with --voice')
    sil = det < thr
    dur_det = len(det) * HOP
    gaps = [(x, y) for x, y in intervals_value(sil) if (y - x) >= a.pause_min - 1e-6 and y < dur_det - 0.05]
    if not gaps and not a.trim_value:
        print('no pause longer than', a.pause_min, "seconds: nothing to cut")

    # Protect planned effect attacks and, with --voice, nonvoice sound attacks.
    # An effect tail may be trimmed with the splice fade; its attack must remain.
    protected = list(protected_intervals)
    if uses_voice:
        n = min(len(det), len(mix_env))
        protected += [(x - 0.04, min(y, x + 0.10)) for x, y in intervals_value((mix_env[:n] > thr) & (det[:n] < thr))
                if y - x >= 0.03]

    def is_protected(s0, s1):
        return any(s0 < y and s1 > x for x, y in protected)

    def k_env(t):
        return min(len(det) - 1, max(0, int(t / HOP)))

    cuts_value, skipped = [], []
    for x, y in gaps:
        opening = x < 0.02
        # The next word may start with a weak consonant below the detection threshold.
        # Search backward up to 0.12 seconds and protect that weak-sound interval.
        y2 = y
        while y2 > y - WALK_VALUE and det[k_env(y2 - HOP)] > weak_value:
            y2 -= HOP
        if opening:   # At the opening, start close to the first word.
            k_lo, k_end = 0, math.floor((y2 - a.start_value) * fps + 1e-6)
            nL = k_end
        else:
            lo = x + PRE0
            hi = min(y - POS0, y2 - POS1)
            k_lo, k_end = math.ceil(lo * fps - 1e-6), math.floor(hi * fps + 1e-6)   # Frames that can hold the cut.
            nL = min(math.floor(((y - x) - a.pause_max) * fps + 1e-6), k_end - k_lo)
        if nL < 2:
            skipped.append(dict(pause=[round(x, 3), round(y, 3)], reason="pause too short after padding"))
            continue

        def search_value(nL):
            best = None
            center_value = (k_lo + k_end - nL) / 2
            for k in range(k_lo, (k_lo if opening else k_end - nL) + 1):
                if k + nL >= n_frames_value:
                    break
                s0, s1 = k * fd, (k + nL) * fd
                cost = float(act[k: k + nL + 1].sum()) if len(act) else 0.0
                if is_protected(s0, s1):
                    cost += 1000
                cost += 1e-3 * abs(k - center_value)
                if best is None or cost < best[0]:
                    best = (cost, k, nL)
            return best

        best = search_value(nL)
        while best is not None and best[0] >= 1000 and nL > 3:   # Shrink until the cut avoids the sound effect.
            nL -= 1
            best = search_value(nL)
        if best is None or best[0] >= 1000:
            skipped.append(dict(pause=[round(x, 3), round(y, 3)], reason='every possible join would cross a sound-effect attack'))
            continue
        cost, k, nL = best
        # Safety check: removed audio must contain no voice above the threshold.
        s0, s1 = k * fd, (k + nL) * fd
        if float(det[k_env(s0):k_env(s1) + 1].max()) >= thr:
            skipped.append(dict(pause=[round(x, 3), round(y, 3)], reason="would touch speech"))
            continue
        cuts_value.append(dict(pause=[round(x, 3), round(y, 3)], q=[k, k + nL], removed=[round(s0, 4), round(s1, 4)],
                           dur=round(nL * fd, 4), remainder=round((y - x) - nL * fd, 3), image=round(cost, 2),
                           start=opening))

    # Explicit cuts, including repeated takes merged into one transcript word.
    forced_cuts = []
    if a.trim_value:
        for pair in a.trim_value.split(","):
            try:
                p0, p1 = (float(v) for v in pair.strip().split("-"))
            except ValueError:
                error_value(f"--trim '{pair}' is invalid. Expected 41.06-41.76,50.2-51.0 in input-video seconds")
            if p1 <= p0:
                error_value(f"--trim '{pair}': end must exceed start")
            k0, k1 = int(round(p0 * fps)), int(round(p1 * fps))
            forced_cuts.append(dict(pause=[p0, p1], q=[k0, k1], removed=[round(k0 * fd, 4), round(k1 * fd, 4)],
                             dur=round((k1 - k0) * fd, 4), forced=True))
        cuts_value = [c for c in cuts_value if not any(c["q"][0] < f["q"][1] and c["q"][1] > f["q"][0] for f in forced_cuts)]
        cuts_value += forced_cuts
    cuts_value.sort(key=lambda c: c["q"][0])
    # Merge touching cuts.
    joined_value = []
    for c in cuts_value:
        if joined_value and c["q"][0] <= joined_value[-1]["q"][1]:
            joined_value[-1]["q"][1] = max(joined_value[-1]["q"][1], c["q"][1])
            joined_value[-1]["removed"][1] = round(joined_value[-1]["q"][1] * fd, 4)
            joined_value[-1]["dur"] = round((joined_value[-1]["q"][1] - joined_value[-1]["q"][0]) * fd, 4)
        else:
            joined_value.append(c)
    cuts_value = joined_value
    retain, t = [], 0
    for c in cuts_value:
        if c["q"][0] > t:
            retain.append([t, c["q"][0]])
        t = max(t, c["q"][1])
    if t < n_frames_value:
        retain.append([t, n_frames_value])
    return dict(thresholdDb=round(thr, 1), weak_db=round(weak_value, 1), background_db=round(floor5, 1), speech_db=round(speech_value, 1),
                cuts=cuts_value, skipped=skipped, retain_q=retain)


# ------------------------------------------------------------------ render
def render_output(a, info: dict, plan_value: dict, output_value: str, td: str) -> dict:
    fps = info["fps"]
    num, den = fps.numerator, fps.denominator
    retain = plan_value["retain_q"]
    # Select video frames by index using one decoder for frame accuracy.
    sel = "+".join(f'between(n\\,{k0}\\,{k1 - 1})' for k0, k1 in retain)
    vf = f'select={sel},setpts=N*{den}/{num}/TB'
    script = os.path.join(td, "vf.txt")
    Path(script).write_text(vf, encoding='utf-8')
    v_tmp = os.path.join(td, "video.mp4")
    extra = []
    v = info["v"]
    for key_value, opt in (("color_space", "-colorspace"), ("color_primaries", "-color_primaries"),
                       ("color_transfer", "-color_trc"), ("color_range", "-color_range")):
        if v.get(key_value) and v[key_value] not in ("unknown", "reserved"):
            extra += [opt, v[key_value]]
    preset = "slow" if info["dur_v"] <= 180 else "medium"
    run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-an", "-/vf", script, "-fps_mode", "cfr", "-r", f'{num}/{den}',
         "-c:v", "libx264", "-preset", preset, "-crf", str(a.crf), "-pix_fmt", "yuv420p", *extra, v_tmp])
    # Cut audio by sample with a short fade at each splice.
    sr, ch = info["sr"], info["ch"]
    raw = run(["ffmpeg", "-v", "error", "-i", a.video, "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
              text_value=False).stdout
    au = np.frombuffer(raw, dtype=np.float32).reshape(-1, ch)
    off = info["v0"] - info["a0"]          # Audio starts after video; uncommon in rendered output.
    nf = int(FADE * sr)
    pieces_value = []
    for i, (k0, k1) in enumerate(retain):
        s0 = max(0, int(round((k0 * den / num + off) * sr)))
        s1 = min(len(au), int(round((k1 * den / num + off) * sr)))
        p = au[s0:s1].copy()
        ramp = np.linspace(0, 1, min(nf, len(p)), dtype=np.float32)[:, None]
        if i > 0 and len(p):
            p[: len(ramp)] *= ramp
        if i < len(retain) - 1 and len(p):
            p[len(p) - len(ramp):] *= ramp[::-1]
        pieces_value.append(p)
    target_value = sum(k1 - k0 for k0, k1 in retain) * den / num
    au_out = np.concatenate(pieces_value) if pieces_value else np.zeros((0, ch), np.float32)
    missing_value = int(round(target_value * sr)) - len(au_out)
    if missing_value > 0:   # Pad audio with silence when it ends before the video.
        au_out = np.concatenate([au_out, np.zeros((missing_value, ch), np.float32)])
    a_tmp = os.path.join(td, "audio.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
                    "-c:a", "pcm_f32le", a_tmp], input=au_out.tobytes(), check=True)
    j, type_value, _ = finalize(v_tmp, a_tmp, output_value)
    return dict(original_volume=float(j["input_i"]), adjustment=type_value)


def map_time(retain: list, fd: float) -> list:
    'Map each interval as input start, input end, and output start, in seconds.'
    out, t = [], 0
    for k0, k1 in retain:
        out.append([round(k0 * fd, 4), round(k1 * fd, 4), round(t * fd, 4)])
        t += k1 - k0
    return out


def audit(a, info: dict, plan_value: dict, output_value: str, mini_src, thr_input: float, lufs_before: float) -> dict:
    fps = float(info["fps"])
    fd = 1 / fps
    retain = plan_value["retain_q"]
    res = {}
    only_value = probe_value(output_value)
    expected = sum(k1 - k0 for k0, k1 in retain) * fd
    res['expected_duration'] = round(expected, 3)
    res['video_duration'] = round(only_value.get("dur_v", 0), 3)
    res['audio_duration'] = round(only_value.get("dur_a", 0), 3)
    # Verify every output frame matches its intended source frame.
    if mini_src is not None:
        idx = np.concatenate([np.arange(k0, k1) for k0, k1 in retain]) if retain else np.zeros(0, int)
        lh, lw = mini_src.shape[1] * 2, mini_src.shape[2] * 2
        correct = total = 0
        worst_value = []
        for j, f in enumerate(frames_value(output_value, lw, lh)):
            if j >= len(idx):
                break
            m = f.reshape(lh, lw)[::2, ::2].astype(np.int16)
            e = int(idx[j])
            neighbor_errors = {d: float(np.abs(m - mini_src[e + d]).mean()) for d in (-2, -1, 0, 1, 2) if 0 <= e + d < len(mini_src)}
            total += 1
            if neighbor_errors[0] <= min(neighbor_errors.values()) + 0.35:
                correct += 1
            else:
                worst_value.append(j)
        res['checked_frames'] = total
        res['aligned_frames'] = correct
        res['misaligned_frames'] = [round(j * fd, 3) for j in worst_value[:10]]
    # Verify retained audio aligns with the picture using source correlation.
    input_audio, exits = audio_mono(a.video), audio_mono(output_value)
    lags = []
    for (t_in0, t_in1, t_out0) in map_time(retain, fd):
        if t_in1 - t_in0 < 0.7:
            continue
        middle = (t_in1 - t_in0) / 2
        l = lag_ms(input_audio, exits, t_in0 + middle, t_out0 + middle)
        if l is not None:
            lags.append(abs(l))
    res['checked_audio_intervals'] = len(lags)
    res['audio_offset_ms'] = round(max(lags), 2) if lags else None
    # Measure remaining pauses in the output.
    end_value = loudness(output_value)
    res["final_lufs"] = float(end_value["input_i"])
    res['final_peak_dbtp'] = float(end_value["input_tp"])
    gain_value = res["final_lufs"] - lufs_before      # Normalization changed the gain; move the threshold by the same amount.
    es = env_db(exits)
    remainders = [(x, y) for x, y in intervals_value(es < thr_input + gain_value)
              if (y - x) > a.pause_max + 0.07 and x > 0.02 and y < len(es) * HOP - 0.05]
    res['remaining_pauses'] = [[round(x, 2), round(y - x, 2)] for x, y in remainders]
    return res


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # Flush progress immediately, including redirected logs.
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='Trim unnecessary pauses from a completed video while protecting complete words.')
    ap.add_argument('video', help='rendered final video, preferably before music')
    ap.add_argument("--pause-max", type=float, default=0.15, help='remaining silence between phrases (default 0.15 seconds)', dest='pause_max')
    ap.add_argument("--pause-min", type=float, default=0.25, help='minimum pause eligible for trimming (default 0.25 seconds)', dest='pause_min')
    ap.add_argument("--without-graphics", metavar="RENDER", help='graphics-only render without the speaker, with matching duration', dest='without_graphics')
    ap.add_argument("--sfx", metavar="PLAN", help='sound_plan.json protects sound-effect attacks', dest='sfx')
    ap.add_argument("--voice", metavar="AUDIO", help='voice-only audio aligned with the video, such as the original cut', dest='voice_value')
    ap.add_argument("--trim", help="explicit intervals to remove, in seconds: 41.06-41.76,50.2-51.0", dest='trim_value')
    ap.add_argument("--start", type=float, default=0.08, help='silence before the first word (default 0.08 seconds)', dest='start_value')
    ap.add_argument("--threshold", type=float, help='silence threshold in dBFS; measured from the video by default', dest='threshold_value')
    ap.add_argument("--crf", type=int, default=16, help='video quality; CRF 16 is high quality', dest='crf')
    ap.add_argument("--output", help='output file (default: <video>_tightened.mp4)', dest='output_value')
    ap.add_argument("--only-plan", action="store_true", help='show and save the plan without rendering', dest='only_plan')
    a = ap.parse_args(argv)

    for b in ("ffmpeg", "ffprobe"):
        if not shutil.which(b):
            error_value(f'not found: {b}. Install FFmpeg (on macOS: brew install ffmpeg).')
    if not Path(a.video).exists():
        error_value(f'could not find {a.video}')
    if not 0.05 <= a.pause_max <= 1.0:
        error_value('--pause-max must be between 0.05 and 1.0 seconds')
    if a.pause_min < a.pause_max + 0.05:
        a.pause_min = round(a.pause_max + 0.1, 3)
    output_value = a.output_value or str(Path(a.video).with_name(Path(a.video).stem + "_tightened.mp4"))
    if Path(output_value).resolve() == Path(a.video).resolve():
        error_value('output must differ from input')
    info = probe_value(a.video)
    if not info.get("v") or not info.get("a"):
        error_value('video must contain both image and audio')
    fps = float(info["fps"])
    fd = 1 / fps

    print(f"reading {Path(a.video).name}: {info['dur_v']:.2f} s, {info['w']}x{info['h']}, {fps:g} fps")
    mix = audio_mono(a.video)
    mix_env = env_db(mix)
    uses_voice = False
    det = mix_env
    if a.voice_value:
        if not Path(a.voice_value).exists():
            error_value(f'could not find {a.voice_value}')
        voice_value = audio_mono(a.voice_value)
        lags = []
        for t in np.linspace(2, min(len(voice_value), len(mix)) / SR_ENV - 2, 8):
            l = lag_ms(voice_value, mix, float(t), float(t))
            if l is not None:
                lags.append(abs(l))
        if lags and max(lags) <= 20:
            det, uses_voice = env_db(voice_value), True
        else:
            notice('--voice audio is not aligned; measuring pauses in the video itself')
    protected_intervals = load_sfx(a.sfx) if a.sfx else []

    act_final, mini = activity(a.video, info["w"], info["h"], store_value=not a.only_plan)
    n_frames_value = len(act_final) or int(round(info["dur_v"] * fps))
    act = act_final
    picture_source = 'frame differences from the video itself'
    if a.without_graphics:
        ig = probe_value(a.without_graphics)
        if not ig.get("v"):
            error_value(f'{a.without_graphics} has no video')
        act_g, _ = activity(a.without_graphics, ig["w"], ig["h"])
        if abs(len(act_g) - n_frames_value) <= 1:
            act = act_g[:n_frames_value] if len(act_g) >= n_frames_value else np.pad(act_g, (0, n_frames_value - len(act_g)))
            picture_source = 'graphics-only render'
        else:
            notice(f'the graphics-only render has {len(act_g)} frames while the video has {n_frames_value}; using the video itself')

    plan_value = create_plan(a, info, det, mix_env, uses_voice, act, protected_intervals, n_frames_value)
    cuts_value = plan_value["cuts"]
    removed = sum(c["dur"] for c in cuts_value)
    dur_output = sum(k1 - k0 for k0, k1 in plan_value["retain_q"]) * fd
    print(f"silence threshold {plan_value['thresholdDb']} dB (speech {plan_value['speech_db']}, background {plan_value['background_db']}) · cut point from {picture_source}" + (' · pauses measured from voice-only audio' if uses_voice else ""))
    print(f"{len(cuts_value)} cuts, {removed:.2f} s removed: {info['dur_v']:.2f} -> {dur_output:.2f} s")
    for c in cuts_value:
        if c.get("forced"):
            print(f"  requested cut  {c['removed'][0]:7.3f} - {c['removed'][1]:7.3f}  ({c['dur']:.3f} s)")
        else:
            print(f"  pause {c['pause'][0]:7.2f}-{c['pause'][1]:6.2f}  remove {c['removed'][0]:7.3f}-{c['removed'][1]:7.3f}  ({c['dur']:.3f} s, remainder {c['remainder']:.2f} s, picture {c['image']:.1f})"
                  + ('  beginning' if c.get("start") else ""))
    for p in plan_value["skipped"]:
        print(f"  retained pause {p['pause'][0]:.2f}-{p['pause'][1]:.2f} ({p['reason']})")
    rel = dict(video=a.video, output=output_value, pause_max=a.pause_max, pause_min=a.pause_min, fps=fps,
               point_start_cut=picture_source, sfx=a.sfx, voice=a.voice_value if uses_voice else None,
               thresholdDb=plan_value["thresholdDb"], weak_db=plan_value["weak_db"], cuts=cuts_value, retained=plan_value["skipped"],
               removed_s=round(removed, 3), duration_input=round(info["dur_v"], 3), duration_output=round(dur_output, 3),
               time_map=map_time(plan_value["retain_q"], fd))
    rel_path = (output_value + ".cut.json")
    if a.only_plan:
        Path(rel_path).write_text(json.dumps(rel, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'plan saved at {rel_path} (nothing rendered)')
        return
    with tempfile.TemporaryDirectory() as td:
        r = render_output(a, info, plan_value, output_value, td)
    conf = audit(a, info, plan_value, output_value, mini, plan_value["thresholdDb"], r['original_volume'])
    rel["verification"] = conf
    rel['volume_adjustment'] = r["adjustment"]
    Path(rel_path).write_text(json.dumps(rel, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'output: {output_value}')
    print(f"  video duration {conf['video_duration']:.3f} seconds, audio {conf['audio_duration']:.3f} s (expected {conf['expected_duration']:.3f}) · volume {conf['final_lufs']} LUFS, peak {conf['final_peak_dbtp']} dBTP (adjustment {r['adjustment']})")
    if "checked_frames" in conf:
        print(f"  picture: {conf['aligned_frames']} of {conf['checked_frames']} aligned frames; audio: {conf['checked_audio_intervals']} intervals checked; maximum offset {conf['audio_offset_ms']} ms")
    if conf['remaining_pauses']:
        print(f'  pauses over {a.pause_max + 0.07:.2f} seconds retained to protect sound effects or weak boundary sounds: '
              + ", ".join(f'{x:.2f} s ({d:.2f})' for x, d in conf['remaining_pauses'][:12]))
    else:
        print(f'  no pause over {a.pause_max + 0.07:.2f} s remains')
    ok = True
    if abs(conf['video_duration'] - conf['expected_duration']) > 1.5 * fd or abs(conf['audio_duration'] - conf['video_duration']) > 0.05:
        notice('duration differs from the expected result; verify before delivery')
        ok = False
    if conf.get('audio_offset_ms') is not None and conf['audio_offset_ms'] > 5:
        notice('audio shifted relative to the picture; verify before delivery')
        ok = False
    if "checked_frames" in conf and conf['aligned_frames'] < conf['checked_frames'] * 0.98:
        notice(f"misaligned frames at {conf['misaligned_frames']}; inspect")
        ok = False
    print("  all checks passed" if ok else "  WARNING: review the findings above")
    print(f'report: {rel_path}')


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
