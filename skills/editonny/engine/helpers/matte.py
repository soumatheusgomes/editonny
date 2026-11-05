"Create and cache a speaker matte per cut interval.\n\nUse the bundled MediaPipe selfie_multiclass_256x256 model converted to ONNX with unchanged weights and its Apache-2.0 terms. ONNX Runtime executes on CPU or supported CoreML hardware. Person probability is one minus the background-class probability.\n\nInfer overlapping square tiles along the output's long dimension and blend their overlaps. At half resolution, use optical-flow-guided temporal smoothing weighted by photometric error, resetting at every cut. Carry a reliable later matte back through initial frames to remove background-connected startup artifacts without opening holes inside the body. Keep the largest body and nearby hands/microphones, fill small holes, remove distant blobs, then refine the full-resolution edge band with a guided filter and soft contrast.\n\nCache lossless full-resolution gray FFV1 mattes plus metadata in edit/renders/matte/. Keys include source signature, interval, decode chain, engine version, and settings so unchanged intervals can be reused. Decode from source through the cut renderer's chain and compare boundary frames with cut.mp4; use the current cut when the source is unavailable or does not align.\n\nUsage: uv run python helpers/matte.py --edit-dir EDIT --video cut.mp4 --json\nUse --force to rebuild, --check to inspect readiness, or --image PHOTO -o cutout.png for an image. Inspect hair, hands, moving edges, and cut boundaries before extending a sample."

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ASSETS_DIR, file_signature, fmt_time, load_json, now_iso, probe_video, require_binary, save_json  # noqa: E402

ENGINE_VERSION = "mc256-3"          # bump when the model or the post-processing changes (every cached matte is redone)
MODEL_NAME = "selfie_multiclass_256x256.onnx"
MODEL_PATH = ASSETS_DIR / "models" / MODEL_NAME
MODEL_SHA256 = "35549e14dc62c73b34f0fd04ec1765d9f1577230a2d068cb25c39f600350f477"
MODEL_SIDE = 256
SECONDS_PER_SECOND = 3.0            # what to tell the person: about 3 minutes of work per minute of video
SETTINGS = {
    "tiles": "long_side",
    "levels": [0.18, 0.75],
    "temporal": {"strength": 0.85, "flow_scale": 0.5, "err_sigma": 0.06, "dual": True, "still": [0.012, 0.03]},
    "cleanup": {"near_px": 28, "min_piece": 0.0006, "hole_small": 0.0005, "hole_max": 0.012, "hole_prob": 0.25},
    "refine": {"r": 3, "eps": 0.002, "band_px": 10, "curve": [0.12, 0.88], "erode": 0.6, "gf_div": 3},
    "start": {"frames": 12, "min_range": 8, "err_floor": 0.01, "err_sigma": 0.02, "bg": [0.1, 0.3], "open_px": 6,
              "min_drop": 0.2},
}
REF_WORK_SHORT = 540                # the settings above are in px of a 540 wide work frame
SYNC_FRAMES = 10                    # frames compared with cut.mp4 at each end of a range read from the source
SYNC_SIZE = 96                      # short side of the frames compared
SYNC_MAX_MAD = 2.0                  # mean gray difference (0-255) above which the frames are not the same picture
                                    # (same frame measured at 0.24-0.28 on a real cut; x264 noise is averaged away)
PROGRESS_EVERY = 5.0                # seconds between progress lines
CACHE_DIR = Path("renders") / "matte"
COREML_ENV = "EDITONNY_MATTE_COREML"
THREADS_ENV = "EDITONNY_MATTE_THREADS"


class MatteError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _cv2():
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    try:
        import cv2  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - depends on the install
        raise MatteError(f'OpenCV is not installed ({e}). Run the installer again or uv sync in the engine folder', 2)
    return cv2


def _short(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ================================================================== engine check

_MODEL_OK: dict[str, str] = {}


def import_ort():
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except ImportError:
        raise MatteError('Segmentation requires onnxruntime. Run the installer again or uv sync in the engine folder', 2) from None
    try:
        ort.set_default_logger_severity(3)      # no engine notes on the person's terminal
    except Exception:  # noqa: BLE001
        pass
    return ort


def check_model(path: Path = MODEL_PATH) -> None:
    """The model file is there and is the one this version was measured with."""
    if not path.is_file():
        raise MatteError(f'person-segmentation model not found ({path}). Reinstall the skill', 2)
    sig = file_signature(path)
    if _MODEL_OK.get(str(path)) == sig:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != MODEL_SHA256:
        raise MatteError(f'segmentation model file ({path.name}) differs from the original and may be corrupt. Reinstall the skill', 2)
    _MODEL_OK[str(path)] = sig


def check_engine() -> dict:
    """Raise errors when the default engine cannot run here. -> {"onnxruntime": version, "model": name}."""
    ort = import_ort()
    check_model()
    _cv2()
    return {"onnxruntime": getattr(ort, "__version__", "?"), "model": MODEL_NAME}


def use_coreml() -> bool:
    """CoreML on Macs with an Apple chip (measured 16/09/2026: 25% faster, same matte, IoU 0.9997 against the CPU);
    EDITONNY_MATTE_COREML=0 keeps the CPU, =1 also tries it on an Intel Mac."""
    raw = (os.environ.get(COREML_ENV) or "").strip()
    if raw in ("0", "1"):
        return raw == "1" and sys.platform == "darwin"
    import platform  # noqa: PLC0415
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _threads_per_session() -> int:
    raw = (os.environ.get(THREADS_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, min(4, (os.cpu_count() or 4) // 3))


# ================================================================== model + post-processing

def _levels(p: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((p - lo) / (hi - lo), 0.0, 1.0)


def smoothstep(x: np.ndarray, e0: float, e1: float) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def tile_origins(length: int, side: int) -> list[int]:
    """Start of each square tile along one axis: as few as cover it, evenly spread."""
    if length <= side:
        return [0]
    n = int(math.ceil(length / side))
    n = max(2, n)
    return [int(round((length - side) * k / (n - 1))) for k in range(n)]


class OnnxSegmenter:
    """selfie_multiclass_256x256 on onnxruntime: person probability of a frame, tile by tile."""

    name = "onnx"

    def __init__(self, sessions: int = 2, threads: int | None = None, coreml: bool | None = None):
        ort = import_ort()
        check_model()
        threads = threads or _threads_per_session()
        if coreml is None:
            coreml = use_coreml()
        cpu: list = ["CPUExecutionProvider"]
        provs = cpu
        if coreml and sys.platform == "darwin" and "CoreMLExecutionProvider" in ort.get_available_providers():
            provs = [("CoreMLExecutionProvider", {"ModelFormat": "NeuralNetwork", "MLComputeUnits": "ALL"})] + cpu
        self.sessions = self._open(ort, sessions, threads, provs)
        if provs is not cpu:
            try:   # CoreML compiles on the first run: a failure there falls back to the CPU
                for s in self.sessions:
                    s.run(None, {s.get_inputs()[0].name: np.zeros((1, MODEL_SIDE, MODEL_SIDE, 3), np.float32)})
            except Exception:  # noqa: BLE001
                self.sessions = self._open(ort, sessions, threads, cpu)
        self.provider = self.sessions[0].get_providers()[0]
        self.input_name = self.sessions[0].get_inputs()[0].name
        self.pool = ThreadPoolExecutor(max_workers=len(self.sessions))
        self.infer_time = 0.0
        self.threads = threads

    @staticmethod
    def _open(ort, sessions: int, threads: int, provs: list) -> list:
        out = []
        for _ in range(max(1, sessions)):
            only_value = ort.SessionOptions()
            only_value.log_severity_level = 3
            only_value.intra_op_num_threads = threads
            only_value.inter_op_num_threads = 1
            try:
                out.append(ort.InferenceSession(str(MODEL_PATH), only_value, providers=provs))
            except Exception as e:  # noqa: BLE001
                if provs[0] != "CPUExecutionProvider":
                    return OnnxSegmenter._open(ort, sessions, threads, ["CPUExecutionProvider"])
                raise MatteError(f'onnxruntime could not open the segmentation model ({e.__class__.__name__}). Reinstall the skill', 2) from None
        return out

    def raw(self, tile_rgb: np.ndarray, k: int = 0) -> np.ndarray:
        """(256, 256, 6) logits of one square RGB tile (any size, resized here)."""
        cv2 = _cv2()
        x = cv2.resize(tile_rgb, (MODEL_SIDE, MODEL_SIDE), interpolation=cv2.INTER_AREA).astype(np.float32)
        x = x / 127.5 - 1.0
        return self.sessions[k % len(self.sessions)].run(None, {self.input_name: x[None]})[0][0]

    def _person(self, tile_rgb: np.ndarray, k: int, out_size: tuple[int, int]) -> np.ndarray:
        cv2 = _cv2()
        y = self.raw(tile_rgb, k)
        e = np.exp(y - y.max(axis=-1, keepdims=True))
        bg = e[..., 0] / e.sum(axis=-1)
        return cv2.resize((1.0 - bg).astype(np.float32), out_size, interpolation=cv2.INTER_LINEAR)

    def person_prob(self, rgb: np.ndarray, work: tuple[int, int]) -> np.ndarray:
        """rgb: HxWx3 uint8 -> float32 person probability at work (w, h)."""
        t0 = time.perf_counter()
        h, w = rgb.shape[:2]
        ww, wh = work
        vertical = h >= w
        side = w if vertical else h
        length, total = (h, wh) if vertical else (w, ww)
        sc = total / length                                    # work px per frame px along the long side
        origins = tile_origins(length, side)
        span = min(total, max(1, int(round(side * sc))))       # tile length at work res
        starts = [min(total - span, int(round(o * sc))) for o in origins]
        jobs = []
        for k, o in enumerate(origins):
            tile = rgb[o:o + side] if vertical else rgb[:, o:o + side]
            size = (ww, span) if vertical else (span, wh)
            jobs.append(self.pool.submit(self._person, tile, k, size))
        acc = np.zeros(total, np.float32)
        out = np.zeros((wh, ww), np.float32)
        for k, (start, job) in enumerate(zip(starts, jobs)):
            p = job.result()
            ramp = np.ones(span, np.float32)
            if k > 0:                                          # overlap with the previous tile: fade in
                ov = max(0, starts[k - 1] + span - start)
                if ov:
                    ramp[:ov] = np.linspace(0, 1, ov, dtype=np.float32)
            if k < len(starts) - 1:                            # overlap with the next tile: fade out
                ov = max(0, start + span - starts[k + 1])
                if ov:
                    ramp[span - ov:] = np.minimum(ramp[span - ov:], np.linspace(1, 0, ov, dtype=np.float32))
            acc[start:start + span] += ramp
            if vertical:
                out[start:start + span] += p * ramp[:, None]
            else:
                out[:, start:start + span] += p * ramp[None, :]
        weight = np.maximum(acc, 1e-6)
        out /= weight[:, None] if vertical else weight[None, :]
        self.infer_time += time.perf_counter() - t0
        return out

    def close(self) -> None:
        self.pool.shutdown(wait=False)


class Temporal:
    """Optical-flow guided EMA at work resolution."""

    def __init__(self, strength: float, flow_scale: float, err_sigma: float, dual: bool = True,
                 still: tuple[float, float] | None = None):
        cv2 = _cv2()
        self.strength, self.flow_scale, self.err_sigma, self.dual = strength, flow_scale, err_sigma, dual
        self.still = tuple(still) if still else None
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.prev_gray = None
        self.prev_alpha = None
        self._grid = None

    def reset(self) -> None:
        self.prev_gray = self.prev_alpha = None

    def carry(self, gray: np.ndarray, prev_gray: np.ndarray, prev_alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """prev_alpha moved onto the frame `gray` (optical flow from prev_gray) -> (alpha, photometric error 0..1)."""
        cv2 = _cv2()
        h, w = gray.shape
        fw, fh = max(8, int(w * self.flow_scale)), max(8, int(h * self.flow_scale))
        g1 = cv2.resize(gray, (fw, fh), interpolation=cv2.INTER_AREA)
        g0 = cv2.resize(prev_gray, (fw, fh), interpolation=cv2.INTER_AREA)
        flow = self.dis.calc(g1, g0, None)                 # cur(x) ~ prev(x + flow)
        flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) * (w / fw)
        if self._grid is None or self._grid[0].shape != (h, w):
            self._grid = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        gx, gy = self._grid
        mx, my = gx + flow[..., 0], gy + flow[..., 1]
        warped_a = cv2.remap(prev_alpha, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        warped_g = cv2.remap(prev_gray, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        g = gray.astype(np.float32)
        err_w = cv2.GaussianBlur(np.abs(g - warped_g.astype(np.float32)) / 255.0, (0, 0), 2)
        if not self.dual:
            return warped_a, err_w
        # where the picture did not move, the previous matte as it was is the better guess
        err_0 = cv2.GaussianBlur(np.abs(g - prev_gray.astype(np.float32)) / 255.0, (0, 0), 2)
        use0 = err_0 <= err_w
        return np.where(use0, prev_alpha, warped_a).astype(np.float32), np.minimum(err_0, err_w)

    def __call__(self, gray: np.ndarray, p: np.ndarray) -> np.ndarray:
        if self.prev_gray is None or self.strength <= 0:
            self.prev_gray, self.prev_alpha = gray, p
            return p
        prev_a, err = self.carry(gray, self.prev_gray, self.prev_alpha)
        conf = np.exp(-err / self.err_sigma)
        # a big disagreement the flow cannot explain is a real change: follow the model. Where the picture did
        # not change at all, nothing came in or went out: a jump of the model there is its own flicker (a patch
        # of room it takes for the body for a few frames), so the matte keeps easing instead of jumping
        dis = np.abs(p - prev_a)
        jump = smoothstep(dis, 0.35, 0.8)
        if self.still:
            jump = jump * smoothstep(err, self.still[0], self.still[1])
        wprev = self.strength * conf * (1.0 - jump)
        out = ((1 - wprev) * p + wprev * prev_a).astype(np.float32)
        self.prev_gray, self.prev_alpha = gray, out
        return out


def cleanup(a: np.ndarray, prob: np.ndarray | None, near_px: float, min_piece: float, hole_small: float,
            hole_max: float, hole_prob: float) -> np.ndarray:
    """Keep the body and the pieces close to it; fill the holes the model was unsure about.

    A background gap the model saw clearly (between an arm and the body, behind glasses) stays open: only
    tiny holes, or holes whose mean model probability is at least hole_prob, are filled."""
    cv2 = _cv2()
    h, w = a.shape
    b = (a > 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    if n <= 1:
        return a
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    keep = (lab == main).astype(np.uint8)
    if n > 2:
        dist = cv2.distanceTransform(1 - keep, cv2.DIST_L2, 3)
        for i in range(1, n):
            if i == main or stats[i, cv2.CC_STAT_AREA] < min_piece * h * w:
                continue
            comp = lab == i
            if dist[comp].min() <= near_px:
                keep[comp] = 1
    inv = (1 - keep).astype(np.uint8)
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    holes = np.zeros_like(keep)
    src = a if prob is None else prob
    for i in range(1, n2):
        x, y, ww, hh, ar = st2[i]
        touches = x == 0 or y == 0 or x + ww == w or y + hh == h
        if touches or ar >= hole_max * h * w:
            continue
        comp = lab2 == i
        if ar < hole_small * h * w or float(src[comp].mean()) >= hole_prob:
            holes[comp] = 1
    solid = keep | holes
    # soft keep mask: 1 on the kept pieces and 4 px around them, fading to 0 by 8 px
    dout = cv2.distanceTransform(1 - solid, cv2.DIST_L2, 3)
    keep_soft = np.clip((8.0 - dout) / 4.0, 0, 1)
    out = a * keep_soft
    if holes.any():
        out = np.maximum(out, cv2.GaussianBlur(holes.astype(np.float32), (0, 0), 1.0) * holes)
    return out.astype(np.float32)


def _box(x, r):
    cv2 = _cv2()
    return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)


def fast_guided_filter(guide_full: np.ndarray, p_low: np.ndarray, r: int, eps: float) -> np.ndarray:
    """Colour guided filter (He et al.): coefficients at the size of p_low, applied with the full size guide."""
    cv2 = _cv2()
    H, W = guide_full.shape[:2]
    h, w = p_low.shape
    I = cv2.resize(guide_full, (w, h), interpolation=cv2.INTER_AREA)  # noqa: E741
    p = p_low
    mI = _box(I, r)
    mean_probability = _box(p, r)
    mIp = _box(I * p[..., None], r)
    cov = mIp - mI * mean_probability[..., None]
    rr = _box(I[..., 0] * I[..., 0], r) - mI[..., 0] * mI[..., 0] + eps
    rg = _box(I[..., 0] * I[..., 1], r) - mI[..., 0] * mI[..., 1]
    rb = _box(I[..., 0] * I[..., 2], r) - mI[..., 0] * mI[..., 2]
    gg = _box(I[..., 1] * I[..., 1], r) - mI[..., 1] * mI[..., 1] + eps
    gb = _box(I[..., 1] * I[..., 2], r) - mI[..., 1] * mI[..., 2]
    bb = _box(I[..., 2] * I[..., 2], r) - mI[..., 2] * mI[..., 2] + eps
    inv_rr = gg * bb - gb * gb
    inv_rg = gb * rb - rg * bb
    inv_rb = rg * gb - gg * rb
    inv_gg = rr * bb - rb * rb
    inv_gb = rb * rg - rr * gb
    inv_bb = rr * gg - rg * rg
    det = rr * inv_rr + rg * inv_rg + rb * inv_rb
    c0, c1, c2 = cov[..., 0], cov[..., 1], cov[..., 2]
    a0 = (inv_rr * c0 + inv_rg * c1 + inv_rb * c2) / det
    a1 = (inv_rg * c0 + inv_gg * c1 + inv_gb * c2) / det
    a2 = (inv_rb * c0 + inv_gb * c1 + inv_bb * c2) / det
    A = np.dstack([a0, a1, a2]).astype(np.float32)
    b = (mean_probability - (A * mI).sum(axis=2)).astype(np.float32)
    A = _box(A, r)
    b = _box(b, r)
    A_full = cv2.resize(A, (W, H), interpolation=cv2.INTER_LINEAR)
    q = cv2.resize(b, (W, H), interpolation=cv2.INTER_LINEAR)
    for c in range(3):
        q += cv2.multiply(A_full[..., c], guide_full[..., c])
    return q


class Refiner:
    def __init__(self, r: int, eps: float, band_px: float, curve, erode: float, gf_div: int):
        self.r, self.eps, self.band_px, self.curve, self.erode, self.gf_div = r, eps, band_px, tuple(curve), erode, gf_div

    def __call__(self, rgb_full: np.ndarray, a_low: np.ndarray) -> np.ndarray:
        cv2 = _cv2()
        H, W = rgb_full.shape[:2]
        guide = rgb_full.astype(np.float32) / 255.0
        gf_size = (max(8, W // self.gf_div), max(8, H // self.gf_div))
        p_gf = cv2.resize(a_low, gf_size, interpolation=cv2.INTER_AREA)
        q = fast_guided_filter(guide, p_gf, self.r, self.eps)
        coarse = cv2.resize(a_low, (W, H), interpolation=cv2.INTER_LINEAR)
        b = (a_low > 0.5).astype(np.uint8)
        d_in = cv2.distanceTransform(b, cv2.DIST_L2, 3)
        d_out = cv2.distanceTransform(1 - b, cv2.DIST_L2, 3)
        d = np.maximum(d_in, d_out)                          # distance to the contour (work px)
        band = np.clip((self.band_px + 2 - d) / 4.0, 0, 1).astype(np.float32)
        band = cv2.resize(band, (W, H), interpolation=cv2.INTER_LINEAR)
        a = band * np.clip(q, 0, 1) + (1 - band) * coarse
        a = smoothstep(a, *self.curve)
        if self.erode > 0:   # pull the edge in a little so the old background does not fringe
            a = cv2.erode(a, np.ones((3, 3), np.uint8)) * self.erode + a * (1 - self.erode)
        return a.astype(np.float32)


class MatteEngine:
    """Frame by frame person matte. process() must see the frames of one range in order; reset() between ranges."""

    version = ENGINE_VERSION

    def __init__(self, size: tuple[int, int], segmenter: OnnxSegmenter | None = None):
        W, H = size
        self.size = (W, H)
        self.work = (max(2, (W // 2) // 2 * 2), max(2, (H // 2) // 2 * 2))
        k = min(self.work) / REF_WORK_SHORT
        t, c, r = SETTINGS["temporal"], SETTINGS["cleanup"], SETTINGS["refine"]
        self.seg = segmenter or OnnxSegmenter()
        self.provider = self.seg.provider
        self.temporal = Temporal(t["strength"], t["flow_scale"], t["err_sigma"], t["dual"], t.get("still"))
        self.clean = dict(c, near_px=c["near_px"] * k)
        self.refiner = Refiner(r["r"], r["eps"], r["band_px"] * k, r["curve"], r["erode"], r["gf_div"])

    def reset(self) -> None:
        self.temporal.reset()

    def coarse_parts(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(temporal matte, model probability, gray) of the next frame at work size (sequential)."""
        cv2 = _cv2()
        p = self.seg.person_prob(rgb, self.work)
        lo, hi = SETTINGS["levels"]
        p = _levels(p, lo, hi)
        gray = cv2.cvtColor(cv2.resize(rgb, self.work, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
        return self.temporal(gray, p), p, gray

    def finish(self, a: np.ndarray, p: np.ndarray) -> np.ndarray:
        return cleanup(a, p, **self.clean)

    def coarse(self, rgb: np.ndarray) -> np.ndarray:
        """Work-size matte of the next frame (sequential: it feeds the temporal filter)."""
        a, p, _ = self.coarse_parts(rgb)
        return self.finish(a, p)

    def settle_start(self, first: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                     anchor: tuple[np.ndarray, np.ndarray]) -> list[np.ndarray]:
        """The first frames of a range, (temporal matte, probability, gray) each, with the matte and gray of the
        frame right after them (anchor): the anchor matte is carried back frame by frame; where the picture
        barely changed along the way and it clearly says background, the start matte drops to it. Only
        patches of START open_px or more that touch the background change (a thin moving hand, a hole inside
        the body never do), fading in over the last half before the anchor. -> corrected temporal mattes."""
        cv2 = _cv2()
        st = SETTINGS["start"]
        n = len(first)
        if n == 0:
            return []
        b, gprev = anchor
        k = min(self.work) / REF_WORK_SHORT
        r = max(1, int(round(st["open_px"] * k)))
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        near = np.ones((5, 5), np.uint8)
        cerr = None
        out: list[np.ndarray] = [None] * n  # type: ignore[list-item]
        for i in range(n - 1, -1, -1):
            a, _, gray = first[i]
            b, err = self.temporal.carry(gray, gprev, b)
            gprev = gray
            cerr = err if cerr is None else np.maximum(cerr, err)
            w = np.exp(-np.maximum(0.0, cerr - st["err_floor"]) / st["err_sigma"])
            w *= 1.0 - smoothstep(b, *st["bg"])
            w *= min(1.0, (n - i) / (n / 2.0))
            drop = (w * np.maximum(0.0, a - b)).astype(np.float32)
            m = cv2.morphologyEx((drop > st["min_drop"]).astype(np.uint8), cv2.MORPH_OPEN, ker)
            if m.any():
                cnt, lab = cv2.connectedComponents(m, connectivity=8)
                touch = cv2.dilate((a < 0.5).astype(np.uint8), near) > 0
                keep = np.zeros(cnt, bool)
                keep[np.unique(lab[touch & (m > 0)])] = True
                keep[0] = False
                m = keep[lab].astype(np.uint8)
            if not m.any():
                out[i] = a
                continue
            soft = cv2.GaussianBlur(cv2.dilate(m, ker).astype(np.float32), (0, 0), r / 2.0)
            out[i] = (a - drop * soft).astype(np.float32)
        return out

    def refine(self, rgb: np.ndarray, coarse: np.ndarray) -> np.ndarray:
        return self.refiner(rgb, coarse)

    def process(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        a = self.coarse(rgb)
        return a, self.refine(rgb, a)

    def close(self) -> None:
        self.seg.close()


# Engines can be swapped (tests use a fake one so they do not depend on the model).
EngineFactory = Callable[[tuple[int, int]], object]
_ENGINE_FACTORY: EngineFactory | None = None
_ENGINE_VERSION_OVERRIDE: str | None = None


def set_engine(factory: EngineFactory | None, version: str | None = None) -> None:
    """Use another engine (an object with version, provider, reset(), coarse(rgb), refine(rgb, coarse), close()).
    None goes back to the ONNX engine."""
    global _ENGINE_FACTORY, _ENGINE_VERSION_OVERRIDE
    _ENGINE_FACTORY = factory
    _ENGINE_VERSION_OVERRIDE = version if factory is not None else None


def engine_version() -> str:
    return _ENGINE_VERSION_OVERRIDE or ENGINE_VERSION


def make_engine(size: tuple[int, int]):
    if _ENGINE_FACTORY is not None:
        return _ENGINE_FACTORY(size)
    return MatteEngine(size)


def default_engine_ready() -> None:
    """Raise MatteError when the engine that would run cannot (a fake engine is always ready)."""
    if _ENGINE_FACTORY is None:
        check_engine()


# ================================================================== stats

def frame_stats(a: np.ndarray) -> tuple | None:
    """Per frame numbers used to place the person (work-size matte): None when nobody is there.

    -> (top, left, right, head_cx, touch_left, touch_right, touch_bottom) as fractions of the frame / bools."""
    m = a > 0.5
    rows = np.flatnonzero(m.any(axis=1))
    if rows.size == 0:
        return None
    h, w = a.shape
    cols = np.flatnonzero(m.any(axis=0))
    r0 = int(rows[0])
    r1 = r0 + max(1, int((h - r0) * 0.35))
    band = a[r0:r1]
    mass = band.sum(axis=0)
    cx = float((mass * np.arange(w)).sum() / max(1e-6, mass.sum()))
    edge = max(1, w // 270)
    return (r0 / h, int(cols[0]) / w, (int(cols[-1]) + 1) / w, cx / w,
            bool(m[:, :edge].any()), bool(m[:, w - edge:].any()), bool(m[h - max(1, h // 480):].any()))


def summarize_stats(rows: list[tuple | None]) -> dict:
    found = [r for r in rows if r is not None]
    n = len(rows)
    if not found:
        return {"frames": n, "person_frames": 0}
    arr = np.array([r[:4] for r in found], np.float64)
    touch = np.array([r[4:] for r in found], np.float64)
    return {
        "frames": n,
        "person_frames": len(found),
        "top": round(float(np.percentile(arr[:, 0], 5)), 5),
        "top_median": round(float(np.median(arr[:, 0])), 5),
        "left": round(float(np.percentile(arr[:, 1], 5)), 5),
        "right": round(float(np.percentile(arr[:, 2], 95)), 5),
        "head_cx": round(float(np.median(arr[:, 3])), 5),
        "touch_left": round(float(touch[:, 0].mean()), 4),
        "touch_right": round(float(touch[:, 1].mean()), 4),
        "touch_bottom": round(float(touch[:, 2].mean()), 4),
    }


# ================================================================== ranges

@dataclass
class RangeJob:
    index: int
    rid: str
    f0: int                     # first frame on the base video (output frame grid)
    f1: int
    size: tuple[int, int]       # full output size (the matte is stored at this size)
    fps: int
    key: str = ""
    method: str = "cut"       # Source video or rendered cut.mp4.
    source: Path | None = None
    source_args: list[str] = field(default_factory=list)   # FFmpeg input and filter arguments for the source method.
    source_hw: bool = False
    base: Path | None = None
    base_pre: list[str] = field(default_factory=list)      # filters that put the base on the output grid
    base_fps: float = 0.0
    note: str = ""              # why the range is read from cut.mp4
    path: Path = field(default_factory=Path)
    meta_path: Path = field(default_factory=Path)
    meta: dict = field(default_factory=dict)
    cached: bool = False
    seconds: float = 0.0

    @property
    def frames(self) -> int:
        return self.f1 - self.f0


def base_pre_filters(base_fps: float, base_size: tuple[int, int], fps: int, size: tuple[int, int]) -> list[str]:
    """The same frame-grid filters compose_final.run_video puts on the base."""
    pre = []
    if abs(base_fps - fps) > 0.01:
        pre.append(f'fps={fps}')
    if tuple(base_size) != tuple(size):
        W, H = size
        pre.append(f'scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,crop={W}:{H},setsar=1')
    return pre


def _timeline_segment(timeline: dict | None, f0: int, f1: int, fps: int) -> dict | None:
    for s in (timeline or {}).get("segments") or []:
        try:
            a = int(s["out_start_frame"]) if "out_start_frame" in s else int(round(float(s["out_start"]) * fps))
            b = int(s["out_end_frame"]) if "out_end_frame" in s else int(round(float(s["out_end"]) * fps))
        except (KeyError, TypeError, ValueError):
            continue
        if a == f0 and b == f1:
            return s
    return None


def _source_plan(job: RangeJob, edit_dir: Path, timeline: dict, seg: dict, sources: dict | None,
                 base_info: dict) -> str:
    """Fill the source decode of a job. Returns "" or why the source cannot be used."""
    import render_cut as rc  # noqa: PLC0415
    from grade import resolve_grade  # noqa: PLC0415

    W, H = job.size
    try:
        tl_fps = int(round(float(timeline.get("fps"))))
        tl_size = (int(timeline.get("width")), int(timeline.get("height")))
    except (TypeError, ValueError):
        return 'cut_timeline.json lacks frame rate or dimensions'
    if timeline.get("preview"):
        return 'the cut is a preview'
    if tl_fps != job.fps or tl_size != (W, H):
        return "the cut has a different size or frame rate"
    if abs(float(base_info.get("fps") or 0) - job.fps) > 0.01 or tuple(base_info.get("size") or ()) != (W, H):
        return 'base-video dimensions differ from the cut'
    other = bool(seg.get("picture_source"))   # the picture came from another camera (render_cut "picture_source")
    try:
        src_start = float(seg["picture_src_start"] if other else seg["src_start"])
        frames = int(seg.get("frames") or job.frames)
    except (KeyError, TypeError, ValueError):
        return 'cut_timeline.json has a stale schema version'
    if frames != job.frames:
        return 'the timeline interval does not match the video'
    if other:
        src = Path(str(seg.get("picture_path") or ""))
        if not src.is_file():
            return 'alternate-camera original video not found'
    elif not sources or str(seg.get("source")) not in sources:
        return 'original video not found'
    else:
        src = sources[str(seg.get("source"))]
    try:
        info = probe_video(src)
    except Exception:  # noqa: BLE001
        return f'could not read {src.name}'
    if not info.get("has_video"):
        return f'{src.name} has no image'
    try:
        grade_filter = resolve_grade(timeline.get("grade"))
    except ValueError:
        return "the cut's grade no longer exists"
    try:
        vf = rc.build_vf(info, W, H, job.fps, grade_filter, seg.get("crop"))
    except Exception:  # noqa: BLE001
        return 'could not prepare original-video decoding'
    snapped = bool(seg.get("pic_snapped", True))
    src_fps = float(info.get("fps") or job.fps)
    seek = max(0.0, src_start - (0.25 / max(src_fps, job.fps) if snapped else 0.0))
    job.method = "source"
    job.source = src
    job.source_args = ["-noaccurate_seek", "-ss", f'{seek:.6f}', "-i", str(src), "-map", "0:v:0", "-vf", vf]
    try:
        job.source_hw = bool(rc.hwaccel_for(src, info))
    except Exception:  # noqa: BLE001
        job.source_hw = False
    job.key = _short({"v": engine_version(), "s": SETTINGS, "how": "source", "src": str(src.resolve()),
                      "sig": file_signature(src), "pic": round(src_start, 6), "snap": snapped, "frames": job.frames,
                      "vf": vf, "size": [W, H], "fps": job.fps})
    return ""


def plan_jobs(edit_dir: Path, base: Path, bounds: list[tuple[int, int]], fps: int, size: tuple[int, int],
              timeline: dict | None, base_fps: float | None = None, base_size: tuple[int, int] | None = None) -> list[RangeJob]:
    """One job per cut range (bounds on the output frame grid)."""
    edit_dir = Path(edit_dir).resolve()
    base = Path(base).resolve()
    if base_fps is None or base_size is None:
        info = probe_video(base)
        base_fps = float(info.get("fps") or fps)
        base_size = (int(info.get("display_width") or info.get("width") or 0),
                     int(info.get("display_height") or info.get("height") or 0))
    base_info = {"fps": base_fps, "size": tuple(base_size)}
    pre = base_pre_filters(base_fps, base_size, fps, size)
    sources = None
    sources_note = ""
    if timeline and timeline.get("segments"):
        try:
            import denoise  # noqa: PLC0415
            sources, sources_note = denoise.resolve_segment_sources(edit_dir, timeline)
        except Exception as e:  # noqa: BLE001
            sources, sources_note = None, f'could not locate original videos ({e.__class__.__name__})'
    base_sig = file_signature(base)
    out_dir = edit_dir / CACHE_DIR
    jobs = []
    for i, (a, b) in enumerate(bounds):
        seg = _timeline_segment(timeline, a, b, fps)
        rid = str(seg.get("id")) if seg and seg.get("id") else f't{i + 1}'
        job = RangeJob(index=i, rid=rid, f0=a, f1=b, size=tuple(size), fps=fps, base=base, base_pre=pre,
                       base_fps=float(base_fps))
        why = 'no cut_timeline.json' if not timeline else ('interval is absent from cut_timeline.json' if seg is None else "")
        if seg is not None:
            why = sources_note if sources is None and sources_note else ""
            if not why:
                why = _source_plan(job, edit_dir, timeline, seg, sources, base_info)
        if job.method != "source":
            job.note = why
            job.key = _short({"v": engine_version(), "s": SETTINGS, "how": "cut", "base": base.name,
                              "sig": base_sig, "f0": a, "f1": b, "size": list(size), "fps": fps, "pre": pre})
        job.path = out_dir / f'm_{job.key}.mkv'
        job.meta_path = out_dir / f'm_{job.key}.json'
        jobs.append(job)
    return jobs


# ================================================================== decode / encode

class FrameReader:
    """Raw frames from an ffmpeg command, read in a thread (the decode runs while the engine works)."""

    def __init__(self, cmd: list[str], size: tuple[int, int], channels: int, what: str, queue_len: int = 6):
        self.size, self.channels, self.what = size, channels, what
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.frame_bytes = size[0] * size[1] * channels
        self.buf: deque = deque()
        self.cond = threading.Condition()
        self.done = False
        self.max = queue_len
        self.err = b""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.err_thread = threading.Thread(target=self._drain_err, daemon=True)
        self.err_thread.start()

    def _drain_err(self) -> None:
        try:
            self.err = self.proc.stderr.read() if self.proc.stderr else b""
        except (OSError, ValueError):
            pass

    def _run(self) -> None:
        W, H = self.size
        shape = (H, W, self.channels) if self.channels > 1 else (H, W)
        try:
            while True:
                data = self.proc.stdout.read(self.frame_bytes) if self.proc.stdout else b""
                if len(data) < self.frame_bytes:
                    break
                frame = np.frombuffer(data, np.uint8).reshape(shape)
                with self.cond:
                    while len(self.buf) >= self.max and not self.done:
                        self.cond.wait()
                    if self.done:
                        break
                    self.buf.append(frame)
                    self.cond.notify_all()
        finally:
            with self.cond:
                self.done = True
                self.cond.notify_all()

    def read(self) -> np.ndarray | None:
        with self.cond:
            while not self.buf and not self.done:
                self.cond.wait()
            if self.buf:
                frame = self.buf.popleft()
                self.cond.notify_all()
                return frame
            return None

    def close(self) -> str:
        with self.cond:
            self.done = True
            self.buf.clear()
            self.cond.notify_all()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.err_thread.join(timeout=2)
        return self.err.decode("utf-8", "replace").strip()


def _ff() -> list[str]:
    return ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error"]


def source_cmd(job: RangeJob, hw: bool, size: tuple[int, int] | None = None, frames: int | None = None) -> list[str]:
    args = list(job.source_args)
    vf_i = args.index("-vf") + 1
    W, H = size or job.size
    extra = "" if (W, H) == tuple(job.size) else f',scale={W}:{H}:flags=area'
    args[vf_i] = args[vf_i] + extra + ",format=rgb24"
    cmd = _ff() + (["-hwaccel", "videotoolbox"] if hw else []) + args
    return cmd + ["-frames:v", str(frames or job.frames), "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def base_cmd(base: Path, pre: list[str], base_fps: float, fps: int, f0: int, n: int,
             size: tuple[int, int] | None = None, full_size: tuple[int, int] | None = None, pix_fmt: str = "rgb24") -> list[str]:
    """Frames f0..f0+n of the base on the output grid (exact: accurate seek without an fps change, else from 0)."""
    filters = list(pre)
    if size is not None and full_size is not None and tuple(size) != tuple(full_size):
        filters.append(f'scale={size[0]}:{size[1]}:flags=area')
    seek: list[str] = []
    if f0 > 0 and abs(base_fps - fps) <= 0.01:
        seek = ["-ss", f'{max(0.0, (f0 - 0.25) / fps):.6f}']
    elif f0 > 0:
        filters.append(f"select='gte(n\\,{f0})'")
    filters.append(f'format={pix_fmt}')
    return _ff() + seek + ["-i", str(base), "-map", "0:v:0", "-vf", ",".join(filters), "-fps_mode", "passthrough",
                           "-frames:v", str(n), "-an", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]


def matte_cmd(path: Path, size: tuple[int, int] | None = None, full_size: tuple[int, int] | None = None) -> list[str]:
    vf = ["format=gray"]
    if size is not None and full_size is not None and tuple(size) != tuple(full_size):
        vf.insert(0, f'scale={size[0]}:{size[1]}:flags=area')
    return _ff() + ["-i", str(path), "-map", "0:v:0", "-vf", ",".join(vf), "-fps_mode", "passthrough",
                    "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]


def _encoder(tmp: Path, size: tuple[int, int], fps: int) -> subprocess.Popen:
    W, H = size
    return subprocess.Popen(_ff() + ["-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f'{W}x{H}', "-framerate", str(fps),
                                     "-i", "-", "-c:v", "ffv1", "-level", "3", "-slices", "4", "-g", "1",
                                     "-pix_fmt", "gray", "-f", "matroska", str(tmp)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def count_frames(path: Path) -> int:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                           "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        return int(proc.stdout.strip().split(",")[0])
    except ValueError:
        return -1


def _small(frame: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    h, w = frame.shape[:2]
    k = SYNC_SIZE / min(h, w)
    size = (max(8, int(round(w * k))), max(8, int(round(h * k))))
    g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) if frame.ndim == 3 else frame
    return cv2.resize(g, size, interpolation=cv2.INTER_AREA).astype(np.float32)


def _read_all(cmd: list[str], size: tuple[int, int], what: str) -> list[np.ndarray]:
    W, H = size
    proc = subprocess.run(cmd, capture_output=True)
    n = W * H * 3
    data = proc.stdout
    if proc.returncode != 0 and not data:
        raise MatteError(f"FFmpeg failed to {what}: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return [np.frombuffer(data[i:i + n], np.uint8).reshape(H, W, 3) for i in range(0, len(data) - n + 1, n)]


def sync_check(job: RangeJob, got_head: list[np.ndarray], got_tail: list[np.ndarray]) -> tuple[bool, dict]:
    """Do the first and last frames read from the source match cut.mp4 at the same positions?"""
    out = {}
    ok = True
    for name, frames, start in (("start", got_head, job.f0), ("end", got_tail, job.f1 - len(got_tail))):
        if not frames:
            continue
        lo = max(0, start - 2)
        n = len(frames) + 4 + (start - 2 - lo)
        ref = _read_all(base_cmd(job.base, job.base_pre, job.base_fps, job.fps, lo, n), job.size,
                        'read the cut to verify segmentation')
        ref_s = [_small(f) for f in ref]
        got_s = [_small(f) for f in frames]
        mads = {}
        for off in (-2, -1, 0, 1, 2):
            diffs = []
            for i, g in enumerate(got_s):
                j = start + off + i - lo
                if 0 <= j < len(ref_s) and ref_s[j].shape == g.shape:
                    diffs.append(float(np.abs(ref_s[j] - g).mean()))
            if diffs:
                mads[off] = sum(diffs) / len(diffs)
        if 0 not in mads:
            ok = False
            out[name] = {"mad": None}
            continue
        best = min(mads.values())
        good = mads[0] <= SYNC_MAX_MAD and mads[0] <= best * 1.3 + 0.1
        out[name] = {"mad": round(mads[0], 3), "best_offset": min(mads, key=mads.get), "ok": good}
        ok = ok and good
    return ok, out


# ================================================================== computing one range

def _progress_line(msg: str, progress: Callable[[str], None] | None) -> None:
    if progress:
        progress(msg)


def compute_job(job: RangeJob, engine, progress: Callable[[str], None] | None = None,
                on_frames: Callable[[int], None] | None = None) -> dict:
    """Run the engine over one range and write its matte file. Returns the meta."""
    W, H = job.size
    job.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.path.with_name(".tmp-" + job.path.name)
    started = time.time()
    method = job.method
    if method == "source":
        cmd = source_cmd(job, job.source_hw)
    else:
        cmd = base_cmd(job.base, job.base_pre, job.base_fps, job.fps, job.f0, job.frames)
    reader = FrameReader(cmd, (W, H), 3, 'read video')
    enc = _encoder(tmp, (W, H), job.fps)
    rows: list = []
    head: list[np.ndarray] = []
    tail: deque = deque(maxlen=SYNC_FRAMES)
    pending: deque = deque()
    written = 0
    engine.reset()
    pool = ThreadPoolExecutor(max_workers=2)
    err = ""

    def write_ready(block: bool) -> None:
        nonlocal written
        while pending and (block or pending[0].done()):
            alpha = pending.popleft().result()
            enc.stdin.write((np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).tobytes())
            written += 1
            if on_frames:
                on_frames(1)

    st = SETTINGS["start"]
    settle = getattr(engine, "settle_start", None)
    n_start = min(st["frames"], job.frames // 3) if settle is not None and job.frames >= st["min_range"] else 0
    first: list[tuple] = []           # (frame, temporal matte, probability, gray) of the first n_start frames

    def emit(frame: np.ndarray, coarse: np.ndarray) -> None:
        rows.append(frame_stats(coarse))
        pending.append(pool.submit(engine.refine, frame, coarse))
        write_ready(len(pending) >= 3)

    try:
        for i in range(job.frames):
            frame = reader.read()
            if frame is None:
                break
            if method == "source":
                if len(head) < SYNC_FRAMES:
                    head.append(frame)
                tail.append(frame)
            if not n_start:
                emit(frame, engine.coarse(frame))
                continue
            a, p, gray = engine.coarse_parts(frame)
            if i < n_start:
                first.append((frame, a, p, gray))
                continue
            if i == n_start:
                fixed = settle([(fa, fp, fg) for _, fa, fp, fg in first], (a, gray))
                for (f_rgb, _, fp, _), fa in zip(first, fixed):
                    emit(f_rgb, engine.finish(fa, fp))
                first = []
            emit(frame, engine.finish(a, p))
        for f_rgb, fa, fp, _ in first:             # a range that ended before the anchor frame
            emit(f_rgb, engine.finish(fa, fp))
        write_ready(True)
    except BrokenPipeError:
        pass
    finally:
        pool.shutdown(wait=True)
        err = reader.close()
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc_err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        code = enc.wait()
    if code != 0:
        tmp.unlink(missing_ok=True)
        raise MatteError(f'FFmpeg failed to write the cutout for interval {job.rid}: {enc_err.strip()[-300:]}')
    if written == 0:
        tmp.unlink(missing_ok=True)
        if method == "source":
            return {"retry": 'no frame decoded from the original video', "written": 0}
        raise MatteError(f"could not read frames for interval {job.rid} ({err[-300:] or 'empty video'})")
    sync = None
    if method == "source":
        ok, sync = sync_check(job, head, list(tail))
        if not ok or written != job.frames:
            tmp.unlink(missing_ok=True)
            why = 'original-video frames do not match the cut' if not ok else 'original video ended early'
            return {"retry": why, "sync": sync, "written": written}
    if written < job.frames:
        # the base ended early: hold the last matte so the frame count stays exact
        tmp.unlink(missing_ok=True)
        raise MatteError(f'interval {job.rid} has {written} frames; the cut expects {job.frames}. Render the cut again')
    got = count_frames(tmp)
    if got != job.frames:
        tmp.unlink(missing_ok=True)
        raise MatteError(f'cutout for interval {job.rid} produced {got} frames; expected {job.frames}')
    os.replace(tmp, job.path)
    seconds = time.time() - started
    meta = {"key": job.key, "engine": engine_version(), "provider": getattr(engine, "provider", "?"),
            "id": job.rid, "method": method, "source": str(job.source) if job.source else None,
            "base": job.base.name if job.base else None, "note": job.note, "frames": job.frames,
            "size": list(job.size), "fps": job.fps, "stats": summarize_stats(rows), "sync": sync,
            "seconds": round(seconds, 2), "created_at": now_iso()}
    save_json(job.meta_path, meta)
    job.seconds = seconds
    return meta


def _load_cached(job: RangeJob) -> dict | None:
    meta = load_json(job.meta_path) if job.meta_path.exists() else None
    if (isinstance(meta, dict) and meta.get("key") == job.key and job.path.exists()
            and job.path.stat().st_size > 0 and int(meta.get("frames") or -1) == job.frames):
        return meta
    return None


@dataclass
class MatteSet:
    jobs: list[RangeJob]
    size: tuple[int, int]
    fps: int
    seconds: float = 0.0
    computed: int = 0
    warnings: list[str] = field(default_factory=list)
    provider: str = ""

    @property
    def cached(self) -> int:
        return sum(1 for j in self.jobs if j.cached)

    def report(self, rel: Callable[[Path], str] = str) -> dict:
        return {"engine": engine_version(), "model": MODEL_NAME, "provider": self.provider or None,
                "seconds": round(self.seconds, 2), "cached": self.cached, "computed": self.computed,
                "ranges": [{"id": j.rid, "start_frame": j.f0, "end_frame": j.f1, "method": j.method,
                            "note": j.note or None, "file": rel(j.path), "cached": j.cached,
                            "seconds": j.meta.get("seconds"), "stats": j.meta.get("stats")} for j in self.jobs]}

    def frames(self, size: tuple[int, int] | None = None) -> Iterator[np.ndarray]:
        """Every matte frame in order (uint8 HxW), optionally scaled to `size`."""
        W, H = size or self.size
        for job in self.jobs:
            reader = FrameReader(matte_cmd(job.path, (W, H), self.size), (W, H), 1, "read the matte")
            got = 0
            last = None
            try:
                for _ in range(job.frames):
                    fr = reader.read()
                    if fr is None:
                        break
                    last = fr
                    got += 1
                    yield fr
            finally:
                err = reader.close()
            if got < job.frames:
                if last is None:
                    raise MatteError(f'could not read segmented interval {job.rid} ({err[-200:]})')
                for _ in range(job.frames - got):
                    yield last


def ensure(edit_dir: Path, base: Path, bounds: list[tuple[int, int]], fps: int, size: tuple[int, int],
           timeline: dict | None, progress: Callable[[str], None] | None = print, force: bool = False,
           base_fps: float | None = None, base_size: tuple[int, int] | None = None) -> MatteSet:
    """The matte of every range, computed now when it is not in the cache."""
    require_binary("ffmpeg")
    jobs = plan_jobs(edit_dir, base, bounds, fps, size, timeline, base_fps, base_size)
    mset = MatteSet(jobs=jobs, size=tuple(size), fps=fps)
    all_value = []
    for job in jobs:
        meta = None if force else _load_cached(job)
        if meta is not None:
            job.cached, job.meta = True, meta
            mset.provider = mset.provider or str(meta.get("provider") or "")
        else:
            all_value.append(job)
    if not all_value:
        return mset
    default_engine_ready()
    total_frames = sum(j.frames for j in all_value)
    video_s = total_frames / fps
    est = video_s * SECONDS_PER_SECOND
    cached_note = f', {len(jobs) - len(all_value)} already ready' if len(all_value) < len(jobs) else ""
    _progress_line(f'person cutout: {len(all_value)} intervals, {video_s:.1f} seconds of video (approximately {_duration_words(est)}{cached_note})', progress)
    started = time.time()
    done_frames = [0]
    last_print = [started]

    def tick(n: int) -> None:
        done_frames[0] += n
        now = time.time()
        if now - last_print[0] >= PROGRESS_EVERY and done_frames[0] < total_frames:
            last_print[0] = now
            pct = 100.0 * done_frames[0] / max(1, total_frames)
            rate = done_frames[0] / max(1e-6, now - started)
            left = (total_frames - done_frames[0]) / max(1e-6, rate)
            _progress_line(f'person cutout: {pct:.0f}% (about {_duration_words(left)})', progress)

    engine = make_engine(tuple(size))
    mset.provider = str(getattr(engine, "provider", "") or "")
    try:
        for job in all_value:
            meta = compute_job(job, engine, progress, tick)
            if "retry" in meta:
                why = meta["retry"]
                # back to cut.mp4 for this range: frames already counted are counted again
                done_frames[0] -= min(done_frames[0], int(meta.get("written") or 0))
                job.method, job.note, job.source = "cut", why, None
                job.key = _short({"v": engine_version(), "s": SETTINGS, "how": "cut", "base": job.base.name,
                                  "sig": file_signature(job.base), "f0": job.f0, "f1": job.f1, "size": list(size),
                                  "fps": fps, "pre": job.base_pre})
                job.path = job.path.with_name(f'm_{job.key}.mkv')
                job.meta_path = job.path.with_suffix(".json")
                cached = None if force else _load_cached(job)
                if cached is not None:
                    job.cached, job.meta = True, cached
                    continue
                meta = compute_job(job, engine, progress, tick)
            job.meta = meta
            mset.computed += 1
    finally:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass
    mset.seconds = time.time() - started
    notes = sorted({j.note for j in jobs if j.method == "cut" and j.note and not j.cached})
    if notes and timeline:
        mset.warnings.append('segmentation used cut.mp4 rather than the original recording (' + "; ".join(notes) + ")")
    _progress_line(f'person cutout complete: {len(jobs)} intervals ({mset.cached} cached) in {mset.seconds:.1f}s ({mset.seconds / max(video_s, 1e-06):.1f} times its duration)', progress)
    return mset


def _duration_words(seconds: float) -> str:
    if seconds < 90:
        return f'{max(1, int(round(seconds)))} s'
    return f'{int(round(seconds / 60.0))} min'


def estimate_seconds(duration: float) -> float:
    return duration * SECONDS_PER_SECOND


# ================================================================== CLI

def _segments_bounds(timeline: dict | None, nf: int, fps: int) -> list[tuple[int, int]]:
    starts = []
    for s in (timeline or {}).get("segments") or []:
        try:
            starts.append(int(round(float(s["out_start"]) * fps + 1e-6)))
        except (KeyError, TypeError, ValueError):
            continue
    cuts = sorted({f for f in starts if 0 < f < nf})
    b = [0] + cuts + [nf]
    return [(x, y) for x, y in zip(b, b[1:]) if y > x]


def _base_facts(base: Path) -> tuple[float, tuple[int, int], int]:
    info = probe_video(base)
    fps = float(info.get("fps") or 30.0)
    size = (int(info.get("display_width") or info.get("width") or 0), int(info.get("display_height") or info.get("height") or 0))
    frames = count_frames(base)
    if frames <= 0:
        frames = int(round(float(info.get("duration") or 0) * fps))
    return fps, size, frames


def run_edit(edit_dir: Path, video: Path | None, force: bool) -> tuple[MatteSet, dict]:
    edit_dir = edit_dir.resolve()
    base = (video or edit_dir / "cut.mp4").resolve()
    if not base.exists():
        raise MatteError(f'video not found: {base}', 2)
    timeline = load_json(edit_dir / "cut_timeline.json") if base == edit_dir / "cut.mp4" else None
    base_fps, base_size, nf = _base_facts(base)
    from common import output_fps_for  # noqa: PLC0415
    fps = int(round(float((timeline or {}).get("fps") or output_fps_for(base_fps))))
    size = (int((timeline or {}).get("width") or base_size[0]), int((timeline or {}).get("height") or base_size[1]))
    size = (size[0] - size[0] % 2, size[1] - size[1] % 2)
    if abs(base_fps - fps) > 0.01:
        nf = int(round(nf / base_fps * fps))
    bounds = _segments_bounds(timeline, nf, fps)
    mset = ensure(edit_dir, base, bounds, fps, size, timeline, progress=lambda m: print(m, flush=True), force=force,
                  base_fps=base_fps, base_size=base_size)
    return mset, {"base": base, "frames": nf, "fps": fps}


def image_matte(src: Path, out: Path) -> dict:
    """One photo: the cut-out as a PNG with transparency (no temporal filter)."""
    cv2 = _cv2()
    bgr = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if bgr is None:
        raise MatteError(f'could not open image {src}', 2)
    h, w = bgr.shape[:2]
    rgb = np.ascontiguousarray(bgr[..., ::-1])
    rgb = np.ascontiguousarray(rgb[:h - h % 2, :w - w % 2])
    engine = make_engine((rgb.shape[1], rgb.shape[0]))
    try:
        alpha = engine.refine(rgb, engine.coarse(rgb))
    finally:
        engine.close()
    a8 = (np.clip(alpha, 0, 1) * 255 + 0.5).astype(np.uint8)
    rgba = np.dstack([rgb[..., ::-1], a8])
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), rgba)
    return {"file": str(out), "width": rgb.shape[1], "height": rgb.shape[0],
            "person_share": round(float((alpha > 0.5).mean()), 4)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description='Segment the person in each cut interval and cache the result for compose_final.py. Processing can take approximately three minutes per minute of video.')
    ap.add_argument("--edit-dir", help='session edit folder', dest='edit_dir')
    ap.add_argument("--video", help='video to segment (default: edit/cut.mp4)', dest='video')
    ap.add_argument("--force", action="store_true", help='recompute segmentation even when cached', dest='force')
    ap.add_argument("--json", action="store_true", help="print the summary as JSON", dest='json')
    ap.add_argument("--check", action="store_true", help='only check whether segmentation works on this computer', dest='check')
    ap.add_argument("--image", help='segment a still image as a quick check', dest='image')
    ap.add_argument("-o", "--output", help='with --image: output PNG', dest='output')
    args = ap.parse_args(argv)
    try:
        if args.check:
            info = check_engine()
            seg = OnnxSegmenter(sessions=1)
            t0 = time.perf_counter()
            y = seg.raw(np.zeros((64, 64, 3), np.uint8))
            ms = (time.perf_counter() - t0) * 1000
            seg.close()
            print(f"ok: person cutout ready (onnxruntime {info['onnxruntime']}, {seg.provider}, output {tuple(y.shape)}, {ms:.0f} ms on the first frame)")
            return 0
        if args.image:
            if not args.output:
                print('error: provide -o output.png with --image', file=sys.stderr)
                return 2
            res = image_matte(Path(args.image).expanduser(), Path(args.output).expanduser())
            print(f"ok: {res['file']} ({res['width']}x{res['height']}, person in {res['person_share'] * 100:.0f}% of the image)")
            return 0
        if not args.edit_dir:
            print('error: supply --edit-dir or use --check', file=sys.stderr)
            return 2
        edit = Path(args.edit_dir).expanduser()
        if not edit.is_dir():
            print(f'error: edit folder not found: {edit}', file=sys.stderr)
            return 2
        mset, facts = run_edit(edit, Path(args.video).expanduser() if args.video else None, args.force)
        rep = mset.report(lambda p: str(p.relative_to(edit.resolve())) if p.is_relative_to(edit.resolve()) else str(p))
        for w in mset.warnings:
            print(f'notice: {w}')
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0
        for j in mset.jobs:
            st = j.meta.get("stats") or {}
            who = 'no person' if not st.get("person_frames") else f"head at {st.get('top', 0):.2f} of height"
            print(f"  {j.rid:>5} {fmt_time(j.f0 / facts['fps'])}-{fmt_time(j.f1 / facts['fps'])}  {('cache' if j.cached else j.method):>6}  {who}")
        print(f'ok: {len(mset.jobs)} intervals cut out in {edit.resolve() / CACHE_DIR}')
        return 0
    except MatteError as e:
        print(f'error: {e}', file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
