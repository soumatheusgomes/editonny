"Built-in sound-effect registry and deterministic procedural synthesis.\n\nassets/sfx/sfx.json records each effect's file, English label, source/license, measured levels, and anchor_s: the point of full strength placed on the visual event frame. Entries refer either to an included recording or a procedural file in assets/sfx/synthetic/. Resolve only current English names and documented labels; do not retain old product aliases.\n\nThe NumPy generator writes deterministic 48 kHz, 16-bit stereo WAV files using fixed seeds. Calibrated effects retain their kit gain rules; do not change levels merely while renaming metadata. synthetic_registry() describes the generated subset.\n\nmeasure() reports BS.1770 K-weighted loudness maxima over 200 ms, 400 ms, and three-second windows, sample peak, and the first 50 ms envelope point within 2 dB of maximum.\n\nUsage: uv run python helpers/make_sfx.py --out-dir assets/sfx/synthetic --only whoosh,pop\nUse --measure for existing files and --synthetic-registry to write the procedural registry. Preserve rights records and inspect timing with the actual movement."

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import unicodedata
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SFX_DIR  # noqa: E402

SR = 48000
PEAK_DB = -3.0
# every built-in effect name, in the order of assets/sfx/sfx.json (the first seven are the 15/09/2026 set)
NAMES = ("whoosh", "pop", "click", "tick", "rise", "cash", "coin",
         "notification", "success", "error", "count", "keyboard", "snap", "mouse_click",
         "bass_drop", "metal_hit", "synth_hit", "flash", "charge", "metal_riser", "wind", "rewind", "gears", "data")
# the procedural set: what this file generates into assets/sfx/synthetic/
SYNTH_NAMES = ("whoosh", "pop", "click", "tick", "rise", "cash", "coin",
               "error", "bass_drop", "synth_hit", "flash", "charge", "wind", "rewind", "data")
SYNTH_DIR = SFX_DIR / "synthetic"        # procedural files (make_sfx output)
MANIFEST = SFX_DIR / "sfx.json"            # built-in name -> file, anchor_s, source, levels
SYNTH_REGISTRY = SFX_DIR / "sfx_synthetic.json"   # the same for the procedural set only (the package's sfx.json)
SYNTH_SOUNDS = {                           # PT-BR description of each procedural sound (duration added)
    "whoosh": 'fast synthetic wind',
    "pop": 'synthetic popping bubble',
    "click": 'short synthetic click',
    "tick": 'short, dry wooden tap',
    "rise": 'short synthetic rise toward its peak',
    "cash": 'cash register: mechanical click followed by a bell',
    "coin": 'synthetic coin chime',
    "error": 'error horn: a short note followed by a lower, longer note',
    "bass_drop": 'bass drop: tone falls from 150 Hz to 26 Hz',
    "synth_hit": 'synthetic impact: falling zap ending in 808 bass',
    "flash": 'flash: rising air, bright burst, bass impact, and fading shimmer',
    "charge": 'energy charge: electrical snap and rising whistle',
    "wind": 'gentle wind passing from left to right',
    "rewind": 'rewinding tape: tangled sound accelerating to an abrupt stop',
    "data": 'computer data: rapid high beeps',
}
SFX_CALIBRATION = 1.4          # linear gain of an effect at sfx.volume 1.0 over a voice at the target loudness (compose_final)
ANCHOR_WINDOW_S = 0.05         # anchor: 50 ms K-weighted power envelope ...
ANCHOR_DROP_DB = 2.0           # ... first reaches 2 dB under its maximum
# Level rule of the kit, at sfx.volume 1.0 over a -14 LUFS voice: short UI accents -14 LUFS on the loudest 200 ms
# window, a soft whoosh -12, impacts -10, and never above -20 LUFS on the 3 s window (long sounds, like the risers,
# are set by that window alone: 6 dB under the voice).
LONG_CAP_LUFS = -20.0
KIT_PEAK_MAX_DB = -1.0
# the procedural sounds of 16/09/2026 come out of the generator already at their kit level (200 ms target or None)
KIT_LEVELS = {"error": -14.0, "bass_drop": -10.0, "synth_hit": -10.0, "flash": -10.0, "charge": None,
              "wind": -12.0, "rewind": None, "data": None}


# ---------------------------------------------------------------- building blocks

def _t(dur: float) -> np.ndarray:
    return np.arange(int(round(dur * SR))) / SR


def _attack_env(n: int, attack_s: float, decay_s: float, hold_s: float = 0.0) -> np.ndarray:
    """Fast linear attack from 0, optional hold, exponential decay (time constant decay_s)."""
    t = np.arange(n) / SR
    env = np.ones(n)
    a = max(1, int(attack_s * SR))
    env[:a] = np.linspace(0.0, 1.0, a, endpoint=False) ** 0.7
    tail = t - attack_s - hold_s
    env = np.where(tail > 0, env * np.exp(-np.maximum(tail, 0) / max(decay_s, 1e-4)), env)
    return env


def _fade_tail(x: np.ndarray, ms: float = 12.0) -> np.ndarray:
    n = min(len(x), int(SR * ms / 1000))
    if n > 1:
        x = x.copy()
        x[-n:] *= np.linspace(1.0, 0.0, n) ** 2
    return x


def _svf_bandpass(x: np.ndarray, cutoff: np.ndarray, q: float) -> np.ndarray:
    """Chamberlin state-variable band-pass with a per-sample cutoff (Hz)."""
    y = np.zeros_like(x)
    low = band = 0.0
    damp = 1.0 / q
    f = 2.0 * np.sin(np.pi * np.clip(cutoff, 20.0, SR / 6.5) / SR)
    for i in range(len(x)):
        high = x[i] - low - damp * band
        band += f[i] * high
        low += f[i] * band
        y[i] = band
    return y


def _one_pole_lowpass(x: np.ndarray, hz: float) -> np.ndarray:
    a = math.exp(-2.0 * math.pi * hz / SR)
    y = np.zeros_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a) * x[i] + a * acc
        y[i] = acc
    return y


def _highpass(x: np.ndarray, hz: float) -> np.ndarray:
    return x - _one_pole_lowpass(x, hz)


def _normalize(stereo: np.ndarray, peak_db: float) -> np.ndarray:
    peak = float(np.max(np.abs(stereo))) or 1.0
    return stereo * (10 ** (peak_db / 20.0) / peak)


# ---------------------------------------------------------------- sounds

def whoosh() -> np.ndarray:
    """Airy swish: noise through a band-pass sweeping down, quick attack, 0.5 s tail."""
    rng = np.random.default_rng(1207)
    dur = 0.55
    n = int(dur * SR)
    t = np.arange(n) / SR
    p = t / dur
    sweep = 5200.0 * (380.0 / 5200.0) ** (p ** 0.6)
    chans = []
    for side, detune in ((0, 1.0), (1, 1.07)):
        noise = rng.standard_normal(n)
        band = _svf_bandpass(noise, sweep * detune, q=1.6)
        air = _highpass(rng.standard_normal(n), 3000.0) * 0.12
        env = _attack_env(n, 0.006, 0.16, hold_s=0.02)
        chans.append((band + air) * env)
    x = np.stack(chans)
    return np.stack([_fade_tail(c, 30) for c in x])


def pop() -> np.ndarray:
    """Bubble pop: sine with a fast pitch drop plus a tiny click on top."""
    rng = np.random.default_rng(88)
    dur = 0.16
    t = _t(dur)
    n = len(t)
    freq = 240.0 + 820.0 * np.exp(-t / 0.018)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * _attack_env(n, 0.0015, 0.045)
    click = _highpass(rng.standard_normal(n), 2500.0) * _attack_env(n, 0.0005, 0.004) * 0.35
    mono = _fade_tail(body + click, 20)
    return np.stack([mono, mono])


def click() -> np.ndarray:
    """Short UI click: bright filtered noise burst with a 2.9 kHz blip."""
    rng = np.random.default_rng(5)
    dur = 0.06
    t = _t(dur)
    n = len(t)
    noise = _highpass(rng.standard_normal(n), 1800.0) * _attack_env(n, 0.0004, 0.006)
    blip = np.sin(2 * np.pi * 2900.0 * t) * _attack_env(n, 0.0006, 0.012) * 0.6
    low = np.sin(2 * np.pi * 180.0 * t) * _attack_env(n, 0.001, 0.01) * 0.35
    mono = _fade_tail(noise + blip + low, 10)
    return np.stack([mono, mono])


def tick() -> np.ndarray:
    """Woody tick: two inharmonic partials with a very short decay."""
    dur = 0.07
    t = _t(dur)
    n = len(t)
    mono = (np.sin(2 * np.pi * 1750.0 * t) + 0.55 * np.sin(2 * np.pi * 3620.0 * t + 0.6)
            + 0.25 * np.sin(2 * np.pi * 820.0 * t)) * _attack_env(n, 0.0008, 0.011)
    mono = _fade_tail(mono, 10)
    return np.stack([mono, mono])


def rise() -> np.ndarray:
    """Upward swell: a pitched hit at t=0, then a tone and noise band climbing for 0.9 s."""
    rng = np.random.default_rng(314)
    dur = 0.9
    t = _t(dur)
    n = len(t)
    p = t / dur
    freq = 220.0 * (4.0 ** (p ** 1.4))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    tone = (np.sin(phase) + 0.3 * np.sin(2 * phase)) * 0.5
    swell = 0.35 + 0.65 * (np.sin(np.pi * np.clip(p / 0.8, 0, 1) / 2.0) ** 2)
    swell *= np.where(p > 0.8, np.exp(-(p - 0.8) * 18.0), 1.0)
    start = np.linspace(0.0, 1.0, int(0.003 * SR), endpoint=False)
    swell[: len(start)] *= start
    # the onset at t=0: a short pitched thump with a bright edge, as loud as the swell peak
    hit_f = 170.0 + 330.0 * np.exp(-t / 0.03)
    hit = np.sin(2 * np.pi * np.cumsum(hit_f) / SR) * _attack_env(n, 0.002, 0.07) * 1.25
    chans = []
    for side in (0, 1):
        noise = _svf_bandpass(rng.standard_normal(n), 900.0 + 5200.0 * p ** 1.5, q=2.0) * 0.8
        edge = _highpass(rng.standard_normal(n), 2200.0) * _attack_env(n, 0.001, 0.02) * 0.5
        chans.append(_fade_tail((tone + noise) * swell + hit + edge, 25))
    return np.stack(chans)


def _partial(t: np.ndarray, hz: float, amp: float, decay_s: float, phase: float = 0.0,
             attack_s: float = 0.0012) -> np.ndarray:
    """One struck metallic partial: sine with a short attack and an exponential decay."""
    return amp * np.sin(2 * np.pi * hz * t + phase) * _attack_env(len(t), attack_s, decay_s)


def _at(x: np.ndarray, seconds: float, n: int) -> np.ndarray:
    """Place a short sound x at `seconds` inside a silent buffer of n samples."""
    out = np.zeros(n)
    i = int(round(seconds * SR))
    if i < n:
        m = min(len(x), n - i)
        out[i:i + m] = x[:m]
    return out


def _clack(rng: np.random.Generator, center_hz: float, dur: float, decay_s: float,
           body_hz: float, body_amp: float) -> np.ndarray:
    """Mechanical clack: band-passed noise burst plus a short resonant knock."""
    t = _t(dur)
    n = len(t)
    burst = _svf_bandpass(rng.standard_normal(n), np.full(n, center_hz), q=1.3) * _attack_env(n, 0.0003, decay_s)
    grit = _highpass(rng.standard_normal(n), 3500.0) * _attack_env(n, 0.0002, decay_s * 0.4) * 0.5
    knock = np.sin(2 * np.pi * body_hz * t) * _attack_env(n, 0.0005, decay_s * 2.2) * body_amp
    return burst + grit + knock


CASH_CHING_AT = 0.085          # seconds from the "ka" clack to the bell strike


def cash() -> np.ndarray:
    """Cash register ka-ching: drawer clack at t=0, bright bell 85 ms later, a light coin rattle."""
    rng = np.random.default_rng(1971)
    dur = 1.15
    t = _t(dur)
    n = len(t)

    # "ka": two quick mechanical clacks (lever and drawer latch) over a low thump, done by ~60 ms
    ka = _at(_clack(rng, 2300.0, 0.05, 0.006, 760.0, 0.35), 0.0, n)
    ka += _at(_clack(rng, 1500.0, 0.035, 0.007, 520.0, 0.3) * 0.6, 0.024, n)
    thump_t = _t(0.06)
    thump_f = 105.0 + 120.0 * np.exp(-thump_t / 0.012)
    thump = np.sin(2 * np.pi * np.cumsum(thump_f) / SR) * _attack_env(len(thump_t), 0.0008, 0.018)
    ka += _at(_fade_tail(thump, 8), 0.0, n) * 0.6
    # the cabinet keeps a faint ring into the bell, so the gap is not digital silence
    ka += _at(np.sin(2 * np.pi * 610.0 * _t(0.12)) * _attack_env(int(0.12 * SR), 0.001, 0.03) * 0.05, 0.0, n)
    ka = _fade_tail(ka[: int(0.12 * SR)], 20)
    ka = np.concatenate([ka, np.zeros(n - len(ka))]) * 1.2

    # "ching": inharmonic bell partials between 2 and 6 kHz; detuned pairs beat slowly, highs die first.
    # Same pitches on both sides (a phone plays it in mono), only the phases differ.
    bell_t = _t(dur - CASH_CHING_AT)
    partials = (   # (Hz, amplitude, decay time constant in s)
        (2217.0, 1.00, 0.62), (2221.5, 0.30, 0.58),
        (3046.0, 0.70, 0.50),
        (3981.0, 0.58, 0.42), (3987.0, 0.22, 0.40),
        (4870.0, 0.44, 0.33),
        (5723.0, 0.34, 0.26),
    )
    # coins settling in the drawer: a few tiny high clinks, quieter each time, alternating sides
    clinks = []
    when = CASH_CHING_AT + 0.17
    for j in range(6):
        when += float(rng.uniform(0.035, 0.07))
        clinks.append((when, float(rng.uniform(5600.0, 7400.0)), 0.16 * (0.8 ** j)))
    chans = []
    for side in (0, 1):
        bell = np.zeros(len(bell_t))
        for k, (hz, amp, decay) in enumerate(partials):
            bell += _partial(bell_t, hz, amp, decay, phase=0.7 * k + 0.5 * side * (k % 2))
        strike = _highpass(rng.standard_normal(len(bell_t)), 4200.0) * _attack_env(len(bell_t), 0.0003, 0.0025) * 0.9
        ching = _at(bell * 0.42 + strike, CASH_CHING_AT, n)
        rattle = np.zeros(n)
        ct = _t(0.05)
        for j, (at_s, f1, amp) in enumerate(clinks):
            clink = (_partial(ct, f1, 1.0, 0.012, attack_s=0.0004)
                     + _partial(ct, f1 * 1.47, 0.6, 0.008, attack_s=0.0004))
            rattle += _at(clink * amp * (1.0 if (j + side) % 2 else 0.55), at_s, n)
        chans.append(_fade_tail(ka + ching + rattle, 180))
    return np.stack(chans)


def coin() -> np.ndarray:
    """Single coin clink: a tiny strike and three high inharmonic partials, about 0.3 s."""
    rng = np.random.default_rng(424)
    dur = 0.32
    t = _t(dur)
    n = len(t)
    chans = []
    for side in (0, 1):
        # same pitches on both sides (no beating when the phone plays it in mono), phases differ
        ring = (_partial(t, 3890.0, 1.00, 0.110, phase=0.3 * side, attack_s=0.0005)
                + _partial(t, 5930.0, 0.62, 0.070, phase=0.4 + 0.9 * side, attack_s=0.0005)
                + _partial(t, 8610.0, 0.34, 0.040, phase=2.0 - 0.7 * side, attack_s=0.0005))
        strike = _highpass(rng.standard_normal(n), 5000.0) * _attack_env(n, 0.0002, 0.0018) * 1.2
        chans.append(_fade_tail(ring + strike, 60))
    return np.stack(chans)


# ---------------------------------------------------------------- helpers for the 16/09/2026 sounds

def _pad_filter(x: np.ndarray, gain) -> np.ndarray:
    """Zero-phase filter in the frequency domain (gain: freqs -> linear gain), padded with 0.25 s of silence."""
    x = np.asarray(x, dtype=np.float64)
    pad = int(0.25 * SR)
    y = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(pad, pad)])
    n = y.shape[-1]
    out = np.fft.irfft(np.fft.rfft(y, axis=-1) * gain(np.fft.rfftfreq(n, 1.0 / SR)), n=n, axis=-1)
    return out[..., pad:pad + x.shape[-1]]


def _lp(x: np.ndarray, hz: float, order: int = 2) -> np.ndarray:
    """Butterworth-shaped low-pass magnitude, zero phase."""
    return _pad_filter(x, lambda f: 1.0 / np.sqrt(1.0 + (f / hz) ** (2 * order)))


def _hp(x: np.ndarray, hz: float, order: int = 2) -> np.ndarray:
    """Butterworth-shaped high-pass magnitude, zero phase."""
    return _pad_filter(x, lambda f: 1.0 / np.sqrt(1.0 + (hz / np.maximum(f, 1e-3)) ** (2 * order)))


def _bp(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    return _lp(_hp(x, lo, order), hi, order)


def _env_db(t: np.ndarray, points) -> np.ndarray:
    """Piecewise-linear envelope in dB: points = ((seconds, dB), ...)."""
    return 10 ** (np.interp(t, [p[0] for p in points], [p[1] for p in points]) / 20.0)


def _phase(freq: np.ndarray) -> np.ndarray:
    return 2 * np.pi * np.cumsum(freq) / SR


def _one_pole_lowpass_var(x: np.ndarray, hz: np.ndarray) -> np.ndarray:
    """One-pole low-pass with a per-sample cutoff (Hz)."""
    a = np.exp(-2.0 * np.pi * np.clip(hz, 1.0, SR / 2.5) / SR)
    y = np.zeros_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a[i]) * x[i] + a[i] * acc
        y[i] = acc
    return y


def _unit(x: np.ndarray) -> np.ndarray:
    return x / (float(np.max(np.abs(x))) or 1.0)


def _fade_in(x: np.ndarray, ms: float) -> np.ndarray:
    n = min(x.shape[-1], max(1, int(SR * ms / 1000)))
    x = x.copy()
    x[..., :n] *= np.linspace(0.0, 1.0, n)
    return x


# ---------------------------------------------------------------- sounds added on 16/09/2026

ERROR_FIRST_GAIN, ERROR_BELL, ERROR_HP = 0.55, 1.0, 200.0


def error() -> np.ndarray:
    """Denied buzz: a short note, then a longer one a fourth lower; odd harmonics pushed toward 400-900 Hz."""
    dur = 0.66
    t = _t(dur)

    def note(f0: float, start: float, length: float, fall_db: float) -> np.ndarray:
        tt = t - start
        ph = 2 * np.pi * f0 * np.maximum(tt, 0.0)
        wave = np.zeros_like(t)
        for k in range(1, 60, 2):          # band-limited square wave
            if k * f0 < 9000.0:
                wave += np.sin(k * ph) / k
        env = (np.clip(tt / 0.006, 0, 1) * np.clip((length - tt) / 0.02, 0, 1)
               * 10 ** (fall_db * np.clip(tt / length, 0, 1) / 20.0))
        return np.where((tt >= 0) & (tt < length), wave * env, 0.0)

    x = ERROR_FIRST_GAIN * note(139.0, 0.0, 0.15, -3.0) + note(104.0, 0.18, 0.46, -30.0)

    def shape(f):
        bell = 1.0 + ERROR_BELL * np.exp(-0.5 * (np.log2(np.maximum(f, 1.0) / 560.0) / 0.5) ** 2)
        return bell / np.sqrt(1.0 + (ERROR_HP / np.maximum(f, 1e-3)) ** 4) / np.sqrt(1.0 + (f / 2600.0) ** 4)

    mono = _fade_tail(_pad_filter(x, shape), 15)
    return np.stack([mono, mono])


def bass_drop() -> np.ndarray:
    """Sub drop: a sine gliding from 150 Hz to 26 Hz with light saturation (heard on phones) and a soft click."""
    rng = np.random.default_rng(2604)
    dur = 1.05
    t = _t(dur)
    freq = 26.0 + (150.0 - 26.0) * np.exp(-t / 0.075)
    body = np.tanh(2.2 * np.sin(_phase(freq))) / np.tanh(2.2)
    body *= _env_db(t, ((0, -60), (0.003, -3), (0.05, 0), (0.12, -5), (0.3, -7), (0.5, -14), (0.65, -15),
                        (0.85, -21), (1.0, -27), (1.05, -40)))
    n = int(0.015 * SR)
    click = _bp(rng.standard_normal(n), 400.0, 1200.0) * np.exp(-np.arange(n) / (0.004 * SR))
    body[:n] += _unit(click) * 10 ** (-30 / 20)
    mono = _fade_tail(_fade_in(body, 0.5), 60)
    return np.stack([mono, mono])


def synth_hit() -> np.ndarray:
    """Synth hit: a sawtooth zap sliding down for 0.33 s straight into a saturated 808 slide (108 -> 32 Hz)."""
    rng = np.random.default_rng(808)
    dur, zap_len = 1.5, 0.33
    x = np.zeros((2, int(dur * SR)))
    nz = int(zap_len * SR)
    tz = np.arange(nz) / SR
    fz = 480.0 + (850.0 - 480.0) * np.exp(-tz / 0.1)
    ph = _phase(fz)
    saw = np.zeros(nz)
    for k in range(1, 16):
        saw += np.where(k * fz < 7000.0, 1.0 / k, 0.0) * np.sin(k * ph)
    saw = _unit(_lp(saw, 3000.0)) * _env_db(tz, ((0, -34), (zap_len * 0.8, -6), (zap_len - 0.01, 0), (zap_len, -40)))
    saw *= 10 ** (-6 / 20)
    x[0, :nz] += saw
    x[1, :nz] += np.roll(saw, int(0.0007 * SR))
    tb = np.arange(x.shape[1] - nz) / SR
    fb = 32.0 + (108.0 - 32.0) * np.exp(-tb / 0.8)
    body = np.tanh(2.2 * np.sin(_phase(fb))) / np.tanh(2.2)
    body *= _env_db(tb, ((0, -60), (0.004, 0), (0.55, -3), (0.8, -18), (1.05, -45), (dur, -60)))
    n = int(0.010 * SR)
    click = _bp(rng.standard_normal(n), 1500.0, 6000.0) * np.exp(-np.arange(n) / (0.002 * SR))
    body[:n] += _unit(click) * 10 ** (-18 / 20)
    x[:, nz:] += body
    return np.stack([_fade_tail(_fade_in(c, 0.5), 50) for c in x])


FLASH_HIT_AT = 0.28            # seconds from the start of the flash sound to the burst
FLASH_DRIVE = 2.5
FLASH_MIX_DB = (-5.0, -7.0, -2.0, -4.0, -10.0)   # thump, swell, burst, shimmer, low tail


def flash() -> np.ndarray:
    """Flash: a noise swell into a bright burst with a low thump, a shimmer that darkens and a short low tail."""
    rng = np.random.default_rng(1826)
    dur, pre = 1.4, FLASH_HIT_AT
    t = _t(dur)
    n = len(t)
    th = t - pre
    after = th >= 0
    thp = np.maximum(th, 0.0)
    f = 52.0 + 45.0 * np.exp(-thp / 0.06)
    thump = np.tanh(1.5 * np.sin(_phase(f))) / np.tanh(1.5)
    thump = np.where(after, thump * _env_db(th, ((0, -60), (0.004, 0), (0.05, -5), (0.11, -16), (0.16, -24),
                                                 (0.4, -34), (0.6, -60), (dur, -60))), 0.0)
    swell, burst, shimmer, tail = (np.zeros((2, n)) for _ in range(4))
    sweep = 350.0 * (3200.0 / 350.0) ** np.clip(t / pre, 0, 1)
    swell_env = _env_db(t, ((0, -50), (pre * 0.5, -30), (pre * 0.9, -6), (pre, 0), (pre + 0.03, -70), (dur, -70)))
    tail_env = np.where(after, _env_db(th, ((0, -70), (0.06, -10), (0.14, 0), (0.35, -8), (0.65, -18),
                                            (dur - pre, -40))), 0.0)
    common = rng.standard_normal((4, n))

    def noise() -> np.ndarray:     # partly shared between the sides, so the flash stays full in mono
        return 0.77 * common[k] + 0.64 * rng.standard_normal(n)

    for side in (0, 1):
        k = 0
        swell[side] = _svf_bandpass(noise(), sweep, q=1.2) * swell_env
        k = 1
        burst[side] = (_bp(noise(), 500.0, 14000.0)
                       * np.where(after, np.exp(-thp / 0.035) * np.clip(th / 0.002, 0, 1), 0.0))
        gate = np.zeros(n)
        for at in pre + np.sort(rng.exponential(0.22, 80)):
            i = int(at * SR)
            if i < n:
                m = min(n - i, int(0.05 * SR))
                gate[i:i + m] += rng.uniform(0.3, 1.0) * np.exp(-np.arange(m) / (0.008 * SR))
        k = 2
        grains = _hp(noise(), 1500.0) * gate
        shimmer[side] = (_one_pole_lowpass_var(grains, 2500.0 + 9500.0 * np.exp(-thp / 0.25))
                         * np.where(after, np.exp(-thp / 0.35), 0.0))
        k = 3
        tail[side] = _bp(noise(), 45.0, 220.0) * tail_env
    w = FLASH_MIX_DB
    x = (10 ** (w[0] / 20) * _unit(thump) + 10 ** (w[1] / 20) * _unit(swell) + 10 ** (w[2] / 20) * _unit(burst)
         + 10 ** (w[3] / 20) * _unit(shimmer) + 10 ** (w[4] / 20) * _unit(tail))
    x = np.tanh(FLASH_DRIVE * _unit(x)) / np.tanh(FLASH_DRIVE)      # soft saturation: a denser, louder hit
    return np.stack([_fade_tail(_fade_in(c, 0.5), 60) for c in x])


CHARGE_OVERTONES, CHARGE_SNAP_TAU, CHARGE_SNAP_GAIN = (0.8, 0.5), 0.08, 2.5


def charge() -> np.ndarray:
    """Energy charge: an electric snap, then a whine that climbs from 3.8 to 10 kHz with two overtones and fades."""
    rng = np.random.default_rng(7)
    dur = 2.4
    t = _t(dur)
    f0 = 3800.0 * (10000.0 / 3800.0) ** (t / dur)
    ph = _phase(f0)
    tone = np.zeros_like(t)
    for k, amp in ((1, 1.0), (2, CHARGE_OVERTONES[0]), (3, CHARGE_OVERTONES[1])):
        tone += amp * np.sin(k * ph) * np.clip((19000.0 - k * f0) / 2000.0, 0, 1)
    tone *= np.exp(-t / 1.1) * (1 - np.exp(-t / 0.004))
    burst = _bp(rng.standard_normal(len(t)), 800.0, 7000.0, 2) * np.exp(-t / CHARGE_SNAP_TAU)
    mono = _fade_tail(0.55 * tone + CHARGE_SNAP_GAIN * burst, 30)
    return np.stack([mono, mono])


WIND_PEAK_AT = 0.42            # seconds to the top of the wind swell
WIND_BODY, WIND_AIR = (30.0, 110.0), 0.08


def wind() -> np.ndarray:
    """Soft wind pass: a deep noise swell with an airy top at its peak, moving from left to right."""
    rng = np.random.default_rng(11)
    dur, pk = 1.3, WIND_PEAK_AT
    t = _t(dur)
    n = len(t)
    rise = np.clip(t / pk, 0, 1) ** 2.2
    fall = np.where(t > pk, np.exp(-(t - pk) / 0.13) * 0.85 + 0.15 * np.exp(-(t - pk) / 0.5), 1.0)
    env = rise * fall
    noise = rng.standard_normal(n)
    body = _bp(noise, WIND_BODY[0], WIND_BODY[1], 2)
    cutoff = 300.0 + 5200.0 * np.exp(-((t - pk) / 0.12) ** 2)
    air = _hp(_one_pole_lowpass_var(rng.standard_normal(n), cutoff), 200.0, 1)
    mono = _hp(_unit(body) + WIND_AIR * _unit(air), 40.0, 4)
    x = np.stack([mono, mono])
    pan = np.clip((t - 0.1) / 0.7, 0, 1)
    x[0] *= np.cos(pan * np.pi / 2) * 0.6 + 0.4
    x[1] *= np.sin(pan * np.pi / 2) * 0.6 + 0.4
    x *= env
    return np.stack([_fade_tail(c, 50) for c in x])


def rewind() -> np.ndarray:
    """Tape rewind: garbled audio that speeds up (pitch 80 -> 900 Hz) and gets louder, then a fast tape stop."""
    rng = np.random.default_rng(1985)
    dur, stop = 1.5, 0.22
    body_len = dur - stop
    t = _t(dur)
    n = len(t)
    u = np.clip(t / body_len, 0, 1)
    fall = np.clip((t - body_len) / stop, 0, 1)
    f0 = 80.0 * (900.0 / 80.0) ** (u ** 0.8) * (0.09 ** fall)
    knots = rng.uniform(-1, 1, int(dur * 60) + 2)
    wob = _unit(_lp(np.interp(t, np.linspace(0, dur, len(knots)), knots), 25.0))
    ph = _phase(f0 * (1 + 0.35 * wob))
    tone = np.zeros(n)
    for h, amp in ((1, 1.0), (2, 0.55), (3, 0.35), (4, 0.22), (5, 0.14), (6, 0.09), (8, 0.05)):
        tone += amp * np.sin(h * ph + rng.uniform(0, 2 * np.pi))
    tone *= 0.55 + 0.45 * np.abs(np.sin(_phase(6 + 20 * u) + 0.6 * wob))
    whirr = _svf_bandpass(rng.standard_normal(n), 400.0 + 3200.0 * u, q=0.9)
    whirr = _hp(whirr * (0.7 + 0.3 * np.sin(_phase(4 + 14 * u))), 150.0)
    sib = _bp(rng.standard_normal(n), 3000.0, 9000.0)
    gate = (np.sin(_phase(8 + 14 * u) + 3 * wob) > 0.35).astype(float)
    sib *= _one_pole_lowpass(gate, 60.0)

    def rms(v):
        return float(np.sqrt(np.mean(v ** 2))) or 1.0

    x = tone / rms(tone) + 0.25 * whirr / rms(whirr) + 0.6 * sib / rms(sib)
    x = _hp(_lp(x, 12000.0, 1), 70.0)
    tk = t - (dur - stop * 0.55)
    x += 0.35 * float(np.max(np.abs(x))) * np.where(tk > 0, np.sin(2 * np.pi * 70 * tk) * np.exp(-tk / 0.05), 0.0)
    env = np.minimum(1, t / 0.12) ** 1.5 * 10 ** ((-9.0 + 9.0 * u ** 1.5) / 20.0)
    env *= np.where(t > body_len, np.exp(-((t - body_len) / (stop * 0.45)) ** 2), 1.0)
    mono = _fade_tail(x * env, 10)
    return np.stack([mono, mono])


def _data_stream(dur: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    x = np.zeros((2, n))
    freqs = np.array([8300.0, 10900.0, 10900.0, 11600.0, 12200.0, 12900.0, 14500.0, 14500.0])
    pos = 0
    while pos < n:
        ln = int(rng.uniform(0.022, 0.055) * SR)
        if rng.random() < 0.8:
            f = float(rng.choice(freqs)) * rng.uniform(0.995, 1.005)
            k = np.arange(min(ln, n - pos))
            seg = np.sin(2 * np.pi * f * k / SR)
            g = np.ones_like(seg)
            r = min(48, len(g) // 2)
            if r:
                g[:r] = np.linspace(0, 1, r)
                g[-r:] = np.linspace(1, 0, r)
            p = rng.uniform(0.15, 0.85)
            amp = rng.uniform(0.5, 1.0)
            x[0, pos:pos + len(k)] += seg * g * amp * np.cos(p * np.pi / 2)
            x[1, pos:pos + len(k)] += seg * g * amp * np.sin(p * np.pi / 2)
        pos += ln + int(rng.uniform(0.0, 0.012) * SR)
    return x


def data() -> np.ndarray:
    """Computer data: two streams of short, random high beeps (8 to 14.5 kHz), one leaning to each side."""
    a, b = _data_stream(2.0, 23), _data_stream(2.0, 24)
    x = np.stack([a[0] + 0.25 * b[0], b[1] + 0.25 * a[1]])
    return np.stack([_fade_tail(_fade_in(c, 10), 150) for c in x])


BUILDERS = {"whoosh": whoosh, "pop": pop, "click": click, "tick": tick, "rise": rise, "cash": cash, "coin": coin,
            "error": error, "bass_drop": bass_drop, "synth_hit": synth_hit, "flash": flash, "charge": charge,
            "wind": wind, "rewind": rewind, "data": data}


# ---------------------------------------------------------------- io

def write_wav(path: Path, stereo: np.ndarray) -> None:
    data = np.clip(stereo, -1.0, 1.0)
    pcm = (data.T * 32767.0).round().astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def kit_gain_db(sound, target_200ms: float | None) -> tuple[float, str]:
    """Gain (dB) that puts a sound at the kit level at sfx.volume 1.0, and the window that set it.

    target_200ms: the LUFS wanted on the loudest 200 ms window (None: long sound, set by the 3 s window alone).
    The 3 s window never goes above LONG_CAP_LUFS."""
    m = measure(sound)
    up = 20 * math.log10(SFX_CALIBRATION)
    gain = LONG_CAP_LUFS - (m["short_term_max_lufs"] + up)
    window = "short_term_max_lufs"
    if target_200ms is not None:
        g200 = target_200ms - (m["loudness_200ms_lufs"] + up)
        if g200 <= gain:
            gain, window = g200, "loudness_200ms_lufs"
    return gain, window


def render(name: str, peak_db: float = PEAK_DB) -> np.ndarray:
    """The procedural sound as written to disk: peak at peak_db, or at its kit level for the KIT_LEVELS names."""
    raw = BUILDERS[name]()
    if name in KIT_LEVELS:            # DC out, and the first and last samples at zero
        raw = raw - raw.mean(axis=1, keepdims=True)
        raw = np.stack([_fade_tail(_fade_in(c, 1.0), 5) for c in raw])
    stereo = _normalize(raw, PEAK_DB if name in KIT_LEVELS else peak_db)
    if name in KIT_LEVELS:
        gain, _ = kit_gain_db(stereo, KIT_LEVELS[name])
        stereo = stereo * 10 ** (gain / 20.0)
        peak = 20 * math.log10(float(np.max(np.abs(stereo))))
        if peak > KIT_PEAK_MAX_DB + 1e-6:
            raise ValueError(f'{name}: the peak would reach {peak:.2f} dBFS at the calibrated kit level')
    return stereo


def make(name: str, out_dir: Path, peak_db: float = PEAK_DB) -> dict:
    """Write out_dir/<name>.wav. peak_db only applies to the first seven; the KIT_LEVELS ones come out at kit level."""
    stereo = render(name, peak_db)
    path = out_dir / f'{name}.wav'
    write_wav(path, stereo)
    peak = float(np.max(np.abs(stereo)))
    first = int(np.argmax(np.max(np.abs(stereo), axis=0) >= 0.5 * peak))
    return {"name": name, "file": str(path), "duration": stereo.shape[1] / SR,
            "peak_db": 20 * math.log10(peak), "attack_ms": 1000.0 * first / SR}


def ensure_sfx(names=SYNTH_NAMES, out_dir: Path = SYNTH_DIR) -> dict[str, Path]:
    """Create any missing procedural SFX file in out_dir. Returns name -> path."""
    out = {}
    for name in names:
        if name not in BUILDERS:
            continue
        path = Path(out_dir) / f'{name}.wav'
        if not path.exists():
            make(name, Path(out_dir))
        out[name] = path
    return out


# ---------------------------------------------------------------- built-in registry

def load_manifest(path: Path = MANIFEST) -> dict:
    """assets/sfx/sfx.json as a dict (empty when missing or unreadable)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def name_key(text: str) -> str:
    """How a name is compared: lower case, no accents, spaces and hyphens as '_' ("Estalo de Dedo" -> estalo_de_dedo)."""
    text = unicodedata.normalize("NFKD", str(text).strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[\s\-]+", "_", text)


def alias_index(manifest: dict) -> dict[str, str]:
    """name_key(id, label or alias) -> id. An id always wins over another effect's label or alias."""
    entries = {k: v for k, v in manifest.items() if isinstance(v, dict)}
    out = {name_key(name): name for name in entries}
    for name, entry in entries.items():
        for alias in [entry.get("label")] + list(entry.get("aliases") or []):
            if isinstance(alias, str) and alias.strip():
                out.setdefault(name_key(alias), name)
    return out


def resolve_name(name, manifest: dict | None = None) -> str | None:
    """The sfx.json id a name points to (the id itself, its display label or an alias), or None."""
    manifest = load_manifest() if manifest is None else manifest
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(manifest.get(name), dict):
        return name
    return alias_index(manifest).get(name_key(name))


def builtin_sfx(names, sfx_dir: Path = SFX_DIR) -> dict[str, dict]:
    """name -> {"path": Path, "anchor_s": float, "id": str} for every built-in effect in names whose file is there.

    A name can be the id ("bass_drop"), its display label or an alias ("bass"); "id" is the sfx.json entry
    it resolved to. A procedural file (under synthetic/) that is missing is generated on the spot. Names that
    are not built-in effects, or whose recording is missing, are left out (the caller warns)."""
    sfx_dir = Path(sfx_dir)
    manifest = load_manifest(sfx_dir / "sfx.json")
    synth = (sfx_dir / "synthetic").resolve()
    out: dict[str, dict] = {}
    for name in names:
        effect = resolve_name(name, manifest)
        entry = manifest.get(effect) if effect else None
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            continue
        path = (sfx_dir / entry["file"]).resolve()
        if not path.exists() and path.parent == synth and path.stem in BUILDERS:
            make(path.stem, synth)
        if not path.exists():
            continue
        try:
            anchor = max(0.0, float(entry.get("anchor_s") or 0.0))
        except (TypeError, ValueError):
            anchor = 0.0
        out[name] = {"path": path, "anchor_s": anchor, "id": effect}
    return out


# ---------------------------------------------------------------- measurement

# BS.1770 K-weighting at 48 kHz: high shelf, then the RLB high-pass
_K_STAGES = (((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585)),
             ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621)))


def read_audio(path: Path) -> np.ndarray:
    """(channels, samples) float in [-1, 1] at 48 kHz: 16-bit WAV read directly, anything else through ffmpeg."""
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() == 2 and w.getframerate() == SR:
                ch = w.getnchannels()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
                return pcm.reshape(-1, ch).T
    except (wave.Error, EOFError, OSError):
        pass
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-f", "f32le",
                           "-ac", "2", "-ar", str(SR), "-"], capture_output=True)
    if proc.returncode != 0:
        raise ValueError(f'could not read sound: {path}')
    return np.frombuffer(proc.stdout[: len(proc.stdout) // 8 * 8], dtype="<f4").reshape(-1, 2).T.astype(np.float64)


def k_weighted(x: np.ndarray) -> np.ndarray:
    """K-weighting applied in the frequency domain (x: channels x samples, 48 kHz, already padded with silence)."""
    n = x.shape[1]
    zinv = np.exp(-2j * np.pi * np.fft.rfftfreq(n, 1.0 / SR) / SR)
    h = np.ones_like(zinv)
    for b, a in _K_STAGES:
        h *= (b[0] + b[1] * zinv + b[2] * zinv * zinv) / (a[0] + a[1] * zinv + a[2] * zinv * zinv)
    return np.fft.irfft(np.fft.rfft(x, axis=1) * h, n=n, axis=1)


def measure(sound) -> dict:
    """Levels and anchor of a sound (a path or a channels x samples array at 48 kHz).

    loudness_200ms_lufs / momentary_max_lufs / short_term_max_lufs: the loudest 200 ms, 400 ms and 3 s window
    (K-weighted, channels summed, BS.1770 offset -0.691). anchor_s: the first moment the 50 ms K-weighted
    power envelope (centered) is within 2 dB of its maximum, i.e. when the sound reaches full strength."""
    x = read_audio(sound) if not isinstance(sound, np.ndarray) else np.atleast_2d(np.asarray(sound, dtype=np.float64))
    n = x.shape[1]
    pad = 3 * SR
    y = k_weighted(np.concatenate([np.zeros((x.shape[0], pad)), x, np.zeros((x.shape[0], pad))], axis=1))
    power = (y * y).sum(axis=0)
    c = np.concatenate([[0.0], np.cumsum(power)])

    def window_max(seconds: float) -> float:
        w = int(round(seconds * SR))
        return -0.691 + 10 * math.log10(max(float(((c[w:] - c[:-w]) / w).max()), 1e-20))

    w = int(ANCHOR_WINDOW_S * SR)
    lo = np.arange(pad, pad + n) - w // 2
    env = 10 * np.log10(np.maximum((c[lo + w] - c[lo]) / w, 1e-20))
    anchor = int(np.argmax(env >= env.max() - ANCHOR_DROP_DB))
    peak = float(np.max(np.abs(x))) if n else 0.0
    return {"duration_s": round(n / SR, 3), "channels": int(x.shape[0]), "anchor_s": round(anchor / SR, 3),
            "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
            "loudness_200ms_lufs": round(window_max(0.2), 2), "momentary_max_lufs": round(window_max(0.4), 2),
            "short_term_max_lufs": round(window_max(3.0), 2)}


LEVEL_KEYS = ("loudness_200ms_lufs", "momentary_max_lufs", "short_term_max_lufs")


def synthetic_registry(sfx_dir: Path = SFX_DIR) -> dict:
    """The sfx.json entries for the procedural set alone: every name -> synthetic/<name>.wav, measured.

    Stored as assets/sfx/sfx_synthetic.json (--synthetic-registry). It is the alternative made by this
    project's own code, shipped alongside the CC0 kit that sfx.json points at by default."""
    up = 20 * math.log10(SFX_CALIBRATION)
    out = {}
    for name in SYNTH_NAMES:
        path = ensure_sfx([name], Path(sfx_dir) / "synthetic")[name]
        m = measure(path)
        anchor = m.pop("anchor_s")
        dur = f"{m['duration_s']:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        out[name] = {"file": f'synthetic/{name}.wav', "anchor_s": anchor, "sound": f'{SYNTH_SOUNDS[name]} ({dur} s)',
                     "source": 'synthetic, generated by helpers/make_sfx.py', "gain_db": 0.0,
                     "level_rule": kit_level_rule(name), "measured": m,
                     "at_volume_1": {k: round(m[k] + up, 2) for k in LEVEL_KEYS}}
    return out


def _lufs_text(value: float) -> str:
    return f'{value:.1f}'.replace(".", ",")


def kit_level_rule(name: str) -> str:
    """PT-BR text of how the generator sets the level of a procedural sound (sfx.json `level_rule`)."""
    if name not in KIT_LEVELS:
        return 'unadjusted generator output with peak at -3 dBFS'
    target = KIT_LEVELS[name]
    _, window = kit_gain_db(render(name), target)
    cap = f'3 s window at {_lufs_text(LONG_CAP_LUFS)} LUFS at volume 1.0'
    if target is None:
        return f'generator-calibrated: {cap} (long sound: 6 dB below voice at -14 LUFS)'
    if window == "short_term_max_lufs":
        return (f'generator-calibrated: {cap}, the long-sound ceiling ({_lufs_text(target)} LUFS in the 200 ms window would exceed the ceiling in the 3 s window)')
    return (f'generator-calibrated 200 ms loudness: {_lufs_text(target)} LUFS at volume 1.0, with a ceiling of {_lufs_text(LONG_CAP_LUFS)} in the 3 s window')


def _print_measure(label: str, m: dict) -> None:
    up = 20 * math.log10(SFX_CALIBRATION)
    print(f"{label}: {m['duration_s']:.2f}s  anchor {m['anchor_s']:.3f}s  peak {m['peak_dbfs']:.1f} dBFS  200 ms {m['loudness_200ms_lufs']:.1f} LUFS (volume 1.0: {m['loudness_200ms_lufs'] + up:.1f})  400 ms {m['momentary_max_lufs']:.1f} ({m['momentary_max_lufs'] + up:.1f})  3 s {m['short_term_max_lufs']:.1f} ({m['short_term_max_lufs'] + up:.1f})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"Generate synthetic sound effects ({', '.join(SYNTH_NAMES)}) as 48 kHz WAV, or measure the loudness and anchor of effects (--measure).")
    ap.add_argument("--out-dir", default=str(SYNTH_DIR), help='output folder (default: engine assets/sfx/synthetic)', dest='out_dir')
    ap.add_argument("--only", help='only these comma-separated sound IDs, for example whoosh,pop', dest='only')
    ap.add_argument("--peak-db", type=float, default=PEAK_DB,
                    help='peak dBFS (default -3); calibrated kit sounds retain their authored level', dest='peak_db')
    ap.add_argument("--measure", nargs="*", metavar="FILE",
                    help='measure 200 ms, 400 ms, and 3 s loudness and peak timing; without a file, measure the bundled kit', dest='measure')
    ap.add_argument("--synthetic-registry", action="store_true",
                    help='measure generated effects and write assets/sfx/sfx_synthetic.json', dest='synthetic_registry')
    args = ap.parse_args(argv)

    if args.synthetic_registry:
        text = json.dumps(synthetic_registry(), ensure_ascii=False, indent=2) + "\n"
        SYNTH_REGISTRY.write_text(text, encoding="utf-8")
        print(f'ok: {SYNTH_REGISTRY}')
        return 0

    if args.measure is not None:
        if args.measure:
            items = [(p, Path(p)) for p in args.measure]
        else:
            found = builtin_sfx(NAMES)
            items = [(name, found[name]["path"]) for name in NAMES if name in found]
        for label, path in items:
            if not Path(path).exists():
                print(f'error: file not found: {path}', file=sys.stderr)
                return 2
            try:
                _print_measure(str(label), measure(Path(path)))
            except ValueError as e:
                print(f'error: {e}', file=sys.stderr)
                return 2
        return 0

    names = [n.strip() for n in args.only.split(",")] if args.only else list(SYNTH_NAMES)
    bad = [n for n in names if n not in BUILDERS]
    if bad:
        print(f"error: unknown effect: {', '.join(bad)}. Options: {', '.join(SYNTH_NAMES)}", file=sys.stderr)
        return 2
    if not -20.0 <= args.peak_db <= 0.0:
        print('error: --peak-db must be between -20 and 0', file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f'error: could not create folder {out_dir}: {e}', file=sys.stderr)
        return 2
    for name in names:
        info = make(name, out_dir, args.peak_db)
        print(f"ok: {name}.wav  {info['duration']:.2f}s  peak {info['peak_db']:.1f} dBFS  attack {info['attack_ms']:.1f} ms")
    print(f'folder: {out_dir}')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
