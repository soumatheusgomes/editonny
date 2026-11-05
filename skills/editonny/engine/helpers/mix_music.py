#!/usr/bin/env python3
'Mix continuous music below the voice in an already assembled video.\n\nApply this after pause tightening so music remains continuous across cuts. Measure integrated voice loudness, then solve track gain for the requested below-voice level after ducking. Use speech-driven sidechain compression, optional delayed music entry and opening emphasis, and an end fade capped to one third of the audible music interval unless explicitly overridden. Normalize final audio in two linear passes and verify duration, voice alignment, and resulting levels.\n\nPad and trim every sidechain input to the exact video duration because some FFmpeg versions otherwise truncate output when one input ends. Preserve original files and write a separate output.\n\nUsage: uv run --directory ENGINE python helpers/mix_music.py --video final.mp4 --music music.wav --below-voice 22\nUse --input-at, --breath, --breath-db, --duck, --fade-end, --start-music, and --output as documented by --help. Listen to the mix; numerical loudness checks alone do not prove intelligibility.'
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit('numpy is missing. Run this helper with the locked engine environment.')

LOOP_CROSSFADE = 1.0   # Crossfade seconds when a short music track must loop.
FADE_END_DEFAULT = 1.5     # Music fade-out duration in seconds.
FADE_END_FRACTION = 1 / 3   # Maximum fraction of the music duration used by the default ending fade.


def error_value(msg: str, code: int = 2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def run(cmd: list, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        error_value("FFmpeg failed:\n" + r.stderr[-2000:])
    return r


def probe_value(p: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,duration,sample_rate:format=duration", "-of", "json", p],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode:
        error_value(f'Could not open {p}')
    d = json.loads(r.stdout)
    out = dict(format=float(d.get("format", {}).get("duration", 0) or 0), video=None, audio=None)
    for s in d.get("streams", []):
        if s.get("codec_type") in ("video", "audio") and out[s["codec_type"]] is None:
            out[s["codec_type"]] = float(s.get("duration") or out["format"])
    return out


def loudness(p: str, af: str = "") -> dict:
    process = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", p, "-vn", "-af",
                        af + "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if process.returncode:
        error_value(f'could not measure loudness of {p}')
    r = process.stderr
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
    from common import ensure_distinct_output
    ensure_distinct_output(output_value, [video, wav])
    tp = -1.5
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
        c = np.correlate(b[i - L:i + w + L], a[i:i + w], mode="valid")
        worst_value = max(worst_value, abs((int(c.argmax()) - L) / sr * 1000))
    return round(worst_value, 2)


def env_db(x: "np.ndarray", sr: int, hop: float) -> "np.ndarray":
    w = max(1, int(sr * hop))
    n = len(x) // w
    return 10 * np.log10((x[: n * w].astype(np.float64).reshape(n, w) ** 2).mean(axis=1) + 1e-12)


def adjust_fade_end(playing_seconds: float, fade_end: float) -> tuple[float, str | None]:
    'Return the usable final fade and warning. Limit the fade to one third of the audible music interval so short music entries retain a steady-level section.'
    maximum_value = round(max(0.1, playing_seconds * FADE_END_FRACTION), 2)
    if fade_end <= maximum_value:
        return fade_end, None
    return maximum_value, (f'music plays for only {playing_seconds:.2f} s; an ending fade of {fade_end:g} seconds would consume almost the entire interval. Using {maximum_value:.2f} seconds, one third of its interval. Override with --fade-end if needed')


# ------------------------------------------------------------------ filter
def chain_music(dur_music: float, start_music: float, needs: float, input_value: float, dur_video: float,
                  gain_db_value: float, breath: float, breath_db: float, fade_end: float) -> tuple[str, int]:
    'Build the music filter: loop, gain, opening boost, fades, delay, then pad and trim to video duration. Return the filter and required copy count.'
    usable = dur_music - start_music
    parts_value = []
    if usable >= needs + 0.05:
        parts_value.append(f'[1:a]atrim=start={start_music:.4f}:duration={needs:.4f},asetpts=PTS-STARTPTS[t0];')
        copies = 1
    else:   # Loop the music with crossfades until it covers the video.
        extra = needs - usable
        copies = 1 + math.ceil(extra / max(1.0, dur_music - LOOP_CROSSFADE)) + 1
        labs = "".join(f'[c{k}]' for k in range(copies))
        parts_value.append(f'[1:a]asplit={copies}{labs};')
        parts_value.append(f'[c0]atrim=start={start_music:.4f},asetpts=PTS-STARTPTS[x0];')
        for k in range(1, copies):
            parts_value.append(f'[x{k - 1}][c{k}]acrossfade=d={LOOP_CROSSFADE}:c1=tri:c2=tri[x{k}];')
        parts_value.append(f'[x{copies - 1}]atrim=duration={needs:.4f},asetpts=PTS-STARTPTS[t0];')
    chain = ['aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo', f'volume={gain_db_value:.3f}dB']
    if breath > 0:
        rg = 10 ** (breath_db / 20)
        ramp = 0.6
        chain.append(f"volume='if(lt(t,{breath:.3f}),{rg:.5f},if(lt(t,{breath + ramp:.3f}),{rg:.5f}+(1-{rg:.5f})*(t-{breath:.3f})/{ramp},1))':eval=frame")
    chain.append(f'afade=t=in:st=0:d={(0.4 if input_value > 0 else 0.15)}')
    if fade_end > 0:
        chain.append(f'afade=t=out:st={max(0.0, needs - fade_end):.4f}:d={min(fade_end, needs):.4f}')
    end_value = ["apad", f'atrim=end={dur_video:.6f}', "asetpts=PTS-STARTPTS"]
    if input_value > 0:
        # Concatenate silence before the music: adelay followed by asetpts can reset the delay
        # in FFmpeg 8, causing music intended for a later entry to start at zero.
        parts_value.append("[t0]" + ",".join(chain) + "[tpre];")
        parts_value.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={input_value:.4f},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[sil];')
        parts_value.append("[sil][tpre]concat=n=2:v=0:a=1," + ",".join(end_value) + "[mus];")
    else:
        parts_value.append("[t0]" + ",".join(chain + end_value) + "[mus];")
    return "".join(parts_value), copies


def complete_filter(args, dur_video: float, dur_music: float, gain_db_value: float, threshold_value: float,
                    duck_db: float | None = None) -> str:
    needs = dur_video - args.input_at
    music_value, _ = chain_music(dur_music, args.start_music, needs, args.input_at, dur_video, gain_db_value,
                              args.breath, args.breath_db, args.fade_end)
    mix = 1 - 10 ** (-(args.duck if duck_db is None else duck_db) / 20)
    return (music_value +
            f'[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad,atrim=end={dur_video:.6f},asetpts=PTS-STARTPTS,asplit=2[voice][sc];[mus][sc]sidechaincompress=threshold={threshold_value:.6f}:ratio=20:attack=15:release=350:knee=2:detection=rms:link=maximum:mix={mix:.4f}[duck];[duck]asplit=2[d1][d2];[voice][d1]amix=inputs=2:normalize=0:duration=first[mix]')


def render_output(args, dur_video, dur_music, gain_db_value, threshold_value, mix_wav, stem_wav, duck_db=None):
    fc = complete_filter(args, dur_video, dur_music, gain_db_value, threshold_value, duck_db)
    run(["ffmpeg", "-v", "error", "-y", "-i", args.video, "-i", args.music_value, "-filter_complex", fc,
         "-map", "[mix]", "-c:a", "pcm_f32le", mix_wav, "-map", "[d2]", "-c:a", "pcm_f32le", stem_wav])


def measure_duck(voice_path: str, stem_path: str, dry_path: str, after_start: float) -> dict:
    'Compare ducked and unducked music in 100 ms windows: attenuation during speech, time attenuated, and voice level above music.'
    sr = 16000
    v, m, s = audio_mono(voice_path, sr), audio_mono(stem_path, sr), audio_mono(dry_path, sr)
    n = min(len(v), len(m), len(s))
    ev, at_value, es = env_db(v[:n], sr, 0.1), env_db(m[:n], sr, 0.1), env_db(s[:n], sr, 0.1)
    k0 = int(after_start / 0.1) + 1
    ev, at_value, es = ev[k0:], at_value[k0:], es[k0:]
    live = es > -80     # Only where the music plays.
    if live.sum() < 20:
        return {}
    speech_value = (ev > np.percentile(ev, 90) - 18) & live
    res = {}
    if speech_value.sum() > 5:
        red = es[speech_value] - at_value[speech_value]
        res['speech_duck_db'] = round(float(np.median(red)), 1)
        mf = 10 * np.log10((10 ** (at_value[speech_value] / 10)).mean())
        vf = 10 * np.log10((10 ** (ev[speech_value] / 10)).mean())
        res['voice_music_gap_db'] = round(float(vf - mf), 1)
    res['attenuated_time_pct'] = round(float(((es - at_value) > 3)[live].mean() * 100), 0)
    return res


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # Flush progress immediately, including redirected logs.
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description='Mix continuous music under speech, with ducking and measured voice-relative level.')
    ap.add_argument("--video", required=True, help='finished video without music; sound effects may already be present', dest='video')
    ap.add_argument("--music", required=True, help='music file: WAV, MP3, M4A, or another supported format', dest='music_value')
    ap.add_argument("--below-voice", type=float, default=22.0,
                    help='music attenuation below voice in dB (default 22; 28 or more may be inaudible on phones)', dest='below_of_voice')
    ap.add_argument("--input-at", type=float, default=0.0, help='music entrance time in video seconds (default 0)', dest='input_at')
    ap.add_argument("--breath", type=float, nargs="?", const=2.0, default=0.0,
                    help='initial music boost duration in seconds (default 2), followed by the normal level', dest='breath')
    ap.add_argument("--breath-db", type=float, default=10.0, help='opening music boost above its normal level (default 10 dB)', dest='breath_db')
    ap.add_argument("--duck", type=float, default=8.0, help='maximum speech-triggered ducking (default 8 dB)', dest='duck')
    ap.add_argument("--fade-end", type=float, default=None,
                    help='ending music fade in seconds (default 1.5, limited to one third of short music intervals)', dest='fade_end')
    ap.add_argument("--start-music", type=float, default=0.0, help='music source start in seconds (default 0)', dest='start_music')
    ap.add_argument("--output", help='output video (default: <video>_music.mp4 beside the source)', dest='output_value')
    a = ap.parse_args(argv)
    from project_contract import managed_config
    managed = managed_config(Path(a.video).resolve().parent)
    if managed:
        policy = managed['workflow']['audio']
        if not policy['music']:
            error_value('Music was not confirmed; propose a settings revision first')
        if policy['mode'] != 'normalize' or policy['targetLufs'] != -14 or policy['truePeakLimit'] != -1:
            error_value('Use compose_final for the confirmed custom/preserved audio policy; this standalone mixer targets -14 LUFS/-1 dBTP')

    for b in ("ffmpeg", "ffprobe"):
        if not shutil.which(b):
            error_value(f'not found: {b}. Install FFmpeg (on macOS: brew install ffmpeg).')
    for p in (a.video, a.music_value):
        if not Path(p).exists():
            error_value(f'could not find {p}')
    output_value = a.output_value or str(Path(a.video).with_name(Path(a.video).stem + "_music.mp4"))
    if Path(output_value).resolve() in (Path(a.video).resolve(), Path(a.music_value).resolve()):
        error_value('output must differ from input')
    if not 6 <= a.below_of_voice <= 40:
        error_value('--below-voice must be between 6 and 40 dB; 20 to 24 is typical')
    sv, st = probe_value(a.video), probe_value(a.music_value)
    if sv["audio"] is None:
        error_value('video has no voice audio against which to measure music')
    if st["audio"] is None:
        error_value('music file has no audio')
    D = sv["video"] or sv["format"]
    Dm = st["audio"] or st["format"]
    if a.input_at >= D - 1:
        error_value(f'--input-at {a.input_at} exceeds the video ending ({D:.2f} s)')
    if a.start_music >= Dm - 1:
        error_value(f'--start-music {a.start_music} exceeds the music ending ({Dm:.2f} s)')
    if a.fade_end is None:
        a.fade_end, notice_fade = adjust_fade_end(D - a.input_at, FADE_END_DEFAULT)
        if notice_fade:
            print('notice: ' + notice_fade)
    elif adjust_fade_end(D - a.input_at, a.fade_end)[1]:
        print(f'notice: --fade-end {a.fade_end:g} seconds exceeds one third of the {D - a.input_at:.2f} seconds of audible music; retained the explicitly requested value')
    if a.breath and a.breath + 0.6 > D - a.input_at - a.fade_end:
        print(f'notice: opening boost of {a.breath:g} seconds does not fit within {D - a.input_at:.2f} seconds of music; the boosted level lasts until the final fade. Shorten --breath or begin earlier')

    jv = loudness(a.video)
    Lv = float(jv["input_i"])
    if Lv < -60:
        error_value('video is effectively silent; voice level cannot be measured')
    threshold_value = max(0.000977, min(0.5, 10 ** ((Lv - 30) / 20)))   # Active voice stands about 20 dB above this level.
    target_value = Lv - a.below_of_voice
    print(f'video voice level: {Lv:.1f} LUFS · music target: {target_value:.1f} LUFS ({a.below_of_voice:g} dB below)')
    if Dm - a.start_music < D - a.input_at:
        print(f'music duration ({Dm:.1f} seconds) is shorter than the requested interval ({D - a.input_at:.1f} seconds; looping with crossfade of {LOOP_CROSSFADE:g} s')

    with tempfile.TemporaryDirectory() as td:
        mix_wav, stem_wav = os.path.join(td, "mix.wav"), os.path.join(td, "stem.wav")
        gain_value = 0.0
        diff = None
        for pass_index in range(3):   # Ducking depends only on voice, so the gain is linear and one correction suffices.
            render_output(a, D, Dm, gain_value, threshold_value, mix_wav, stem_wav)
            Ls = float(loudness(stem_wav)["input_i"])
            diff = Lv - Ls
            if abs(diff - a.below_of_voice) <= 0.3:
                break
            gain_value += target_value - Ls
        dry_wav = os.path.join(td, "dry.wav")
        render_output(a, D, Dm, gain_value, threshold_value, os.path.join(td, "mix_dry.wav"), dry_wav, duck_db=0.0)
        duck = measure_duck(a.video, stem_wav, dry_wav, a.input_at + (a.breath + 0.6 if a.breath else 0.0))
        jm, type_value, end_value = finalize(a.video, mix_wav, output_value)

    only_value = probe_value(output_value)
    sync_value = deviation_synchronization(a.video, output_value)
    print(f'music: gain {gain_value:+.1f} dB; measured {diff:.1f} dB below voice in integrated loudness, including ducking')
    if duck.get('speech_duck_db') is not None:
        print(f"ducking: music attenuates by {duck['speech_duck_db']:.1f} dB during speech (attenuated for {duck['attenuated_time_pct']:.0f}% of the music interval; recovers during long pauses and at the end. During speech, voice remains {duck['voice_music_gap_db']:.1f} dB above music")
    print(f'output: {output_value}')
    print(f"  video duration {only_value['video']:.3f} seconds, audio {only_value['audio']:.3f} s (input {D:.3f} s) · final loudness {end_value['input_i']} LUFS, peak {end_value['input_tp']} dBTP (adjustment {type_value}) · voice alignment offset {sync_value} ms")
    if abs(only_value["audio"] - D) > 0.05:
        print('  warning: audio and video durations differ; verify before delivery')
    if sync_value > 5:
        print('  warning: speech timing shifted; verify before delivery')
    if type_value != "linear":
        print('  notice: normalization became dynamic; verify that speech remains natural')
    rel = dict(video=a.video, music=a.music_value, output=output_value, voice_lufs=Lv, below_request_db=a.below_of_voice,
               below_measured_db=round(diff, 2), gain_music_db=round(gain_value, 2), input_at=a.input_at,
               breath_s=a.breath, breath_db=a.breath_db, duck_max_db=a.duck, fade_end_s=a.fade_end, **duck,
               duration_input=round(D, 3), duration_output_video=round(only_value["video"], 3),
               duration_output_audio=round(only_value["audio"], 3), final_lufs=float(end_value["input_i"]),
               peak_final_dbtp=float(end_value["input_tp"]), adjustment=type_value, deviation_voice_ms=sync_value)
    Path(output_value + ".music.json").write_text(json.dumps(rel, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
