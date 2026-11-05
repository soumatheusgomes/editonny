"""Procedural takes for the sound effect library (effects.py): variations the CC0 packs have few of.

Every sound is made here with numpy (make_sfx building blocks), from a fixed seed: the same command gives the
same files. Own code, so the files can go anywhere (package included). Types and where they are filed:

    whoosh   (transition)  band-passed noise sweeping up or down, panning across, 0.35 to 1.2 s
    swipe    (transition)  short fast sweep, 0.16 to 0.3 s
    riser    (reveal)  noise and a tone climbing to a stop, 1.2 to 3 s (the strong point is the end)
    swell    (reveal)  reverse-cymbal-like swell that stops dead, 0.9 to 2 s
    boom     (impact)    sub drop with a noise hit, 0.8 to 1.6 s
    boom808  (humor)      saturated 808 thump with a room tail (the "boom" of memes, generic)
    glitch   (technology) stuttered digital slices, 0.3 to 0.7 s

Usage:
    uv run python helpers/generate_effects.py DIRECTORY [--per-type 8]
    uv run python helpers/effects.py import DIRECTORY --origin synthetic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_sfx as M  # noqa: E402

SR = M.SR


def _t(d: float) -> np.ndarray:
    return np.arange(int(d * SR)) / SR


def _pan(mono: np.ndarray, start: float, end: float) -> np.ndarray:
    p = np.linspace(start, end, len(mono))           # -1 left .. 1 right, equal power
    a = (p + 1) * np.pi / 4
    return np.stack([mono * np.cos(a), mono * np.sin(a)])


def whoosh(rng) -> np.ndarray:
    d = rng.uniform(0.35, 1.2)
    t = _t(d)
    up = rng.random() < 0.6
    f0, f1 = (rng.uniform(250, 600), rng.uniform(2500, 6000)) if up else (rng.uniform(2500, 5000), rng.uniform(250, 600))
    peak = rng.uniform(0.45, 0.7)
    cut = f0 * (f1 / f0) ** (t / d)
    x = M._svf_bandpass(rng.standard_normal(len(t)), cut, rng.uniform(1.2, 3.0))
    env = np.where(t < peak * d, (t / (peak * d)) ** 2.2, np.exp(-(t - peak * d) / (0.18 * d)))
    body = M._lp(rng.standard_normal(len(t)), 300.0) * 0.35
    mono = M._fade_tail(M._unit(x) * env + M._unit(body) * env * 0.4, 30)
    s = rng.choice([-1, 1])
    return _pan(mono, -0.7 * s, 0.7 * s)


def swipe(rng) -> np.ndarray:
    d = rng.uniform(0.16, 0.3)
    t = _t(d)
    cut = rng.uniform(1500, 3000) * (rng.uniform(3, 5)) ** (t / d)
    x = M._svf_bandpass(rng.standard_normal(len(t)), cut, rng.uniform(2, 4))
    env = np.sin(np.pi * t / d) ** 1.5
    s = rng.choice([-1, 1])
    return _pan(M._fade_tail(M._unit(x) * env, 10), -0.8 * s, 0.8 * s)


def riser(rng) -> np.ndarray:
    d = rng.uniform(1.2, 3.0)
    t = _t(d)
    cut = 200 * (rng.uniform(25, 45)) ** (t / d)
    noise = M._unit(M._svf_bandpass(rng.standard_normal(len(t)), cut, 1.5))
    f = rng.uniform(120, 220) * (rng.uniform(4, 7)) ** ((t / d) ** 1.4)
    tone = np.sign(np.sin(M._phase(f))) * 0.25 + np.sin(M._phase(f * 1.005)) * 0.5
    tone = M._lp(tone, 3000.0)
    env = (t / d) ** rng.uniform(1.8, 3.0)
    mono = (noise * 0.8 + M._unit(tone) * 0.45) * env
    mono[-int(0.01 * SR):] *= np.linspace(1, 0, int(0.01 * SR))
    return _pan(mono, 0, 0) * 0.9 + np.stack([np.roll(mono, 240), mono]) * 0.1


def swell(rng) -> np.ndarray:
    d = rng.uniform(0.9, 2.0)
    t = _t(d)
    x = M._bp(rng.standard_normal(len(t)), rng.uniform(2500, 4000), rng.uniform(9000, 14000))
    env = np.exp((t - d) / (rng.uniform(0.18, 0.35) * d))
    mono = M._unit(x) * env
    mono[-int(0.008 * SR):] *= np.linspace(1, 0, int(0.008 * SR))
    return np.stack([mono, np.roll(mono, 180)])


def boom(rng) -> np.ndarray:
    d = rng.uniform(0.8, 1.6)
    t = _t(d)
    lo = rng.uniform(30, 42)
    f = lo + (rng.uniform(90, 140) - lo) * np.exp(-t / rng.uniform(0.05, 0.12))
    body = np.tanh(rng.uniform(1.5, 3.0) * np.sin(M._phase(f))) * np.exp(-t / (0.35 * d))
    hit = M._lp(rng.standard_normal(len(t)), rng.uniform(1500, 3500)) * np.exp(-t / 0.05)
    mono = M._fade_tail(M._fade_in(M._unit(body) + M._unit(hit) * 0.35, 0.5), 60)
    return np.stack([mono, mono])


def boom808(rng) -> np.ndarray:
    d = rng.uniform(1.1, 1.6)
    t = _t(d)
    f = rng.uniform(45, 60) + rng.uniform(110, 160) * np.exp(-t / 0.04)
    dry = np.tanh(rng.uniform(3.0, 5.0) * np.sin(M._phase(f))) * np.exp(-t / rng.uniform(0.35, 0.55))
    wet = np.zeros_like(dry)
    for k, g in ((0.031, 0.5), (0.047, 0.4), (0.071, 0.3), (0.113, 0.22), (0.17, 0.15)):
        n = int(k * SR)
        wet[n:] += dry[:-n] * g
    mono = M._fade_tail(M._fade_in(M._unit(dry + M._lp(wet, 900.0)), 0.5), 80)
    return np.stack([mono, np.roll(mono, 90)])


def glitch(rng) -> np.ndarray:
    d = rng.uniform(0.3, 0.7)
    n = int(d * SR)
    out = np.zeros(n)
    k = 0
    while k < n:
        L = int(rng.uniform(0.012, 0.06) * SR)
        kind = rng.integers(3)
        tt = np.arange(L) / SR
        if kind == 0:
            seg = np.sign(np.sin(2 * np.pi * rng.uniform(200, 2400) * tt))
        elif kind == 1:
            seg = rng.standard_normal(L)
        else:
            seg = np.sin(2 * np.pi * rng.uniform(800, 5000) * tt)
        seg *= rng.uniform(0.3, 1.0) * (rng.random() > 0.15)
        out[k:k + L] = seg[: max(0, min(L, n - k))]
        k += L
    out = M._fade_tail(M._lp(out, 7000.0), 15)
    return _pan(M._unit(out), rng.uniform(-0.5, 0), rng.uniform(0, 0.5))


TYPES = {"whoosh": whoosh, "swipe": swipe, "riser": riser, "swell": swell, "boom": boom, "boom808": boom808,
         "glitch": glitch}
SEEDS = {name: 7000 + 100 * i for i, name in enumerate(TYPES)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument('directory', type=Path)
    ap.add_argument("--per-type", type=int, default=8, dest='per_type')
    a = ap.parse_args()
    a.directory.mkdir(parents=True, exist_ok=True)
    for name, fn in TYPES.items():
        for i in range(a.per_type):
            rng = np.random.default_rng(SEEDS[name] + i)
            x = M._normalize(fn(rng), -3.0)
            M.write_wav(a.directory / f'{name}_{i + 1:02d}.wav', x)
    print(f'{len(TYPES) * a.per_type} sounds in {a.directory}')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
