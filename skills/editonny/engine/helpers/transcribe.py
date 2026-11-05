# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
'Transcribe one video locally with word-level timing.\n\nDecode to 16 kHz mono while avoiding silent or phase-cancelled channels. Measure silence using the speech-region threshold; shorten long silent spans before Whisper to reduce hallucinations, then map timestamps back to the original recording. Process pause-aligned chunks of at most 28 seconds with source-language autodetection, no default language-biasing prompt, and condition_on_previous_text disabled. Use MLX on supported Apple Silicon and Faster Whisper on other supported systems.\n\nReject hallucinated words wholly inside measured silence, repeated n-gram loops, and recognized subtitle-credit artifacts. Adjust word edges from silence toward actual speech and report implausibly low word coverage for measured voice duration. These safeguards do not replace listening.\n\nResolve or reject iCloud-only placeholders before FFmpeg so a read cannot hang silently. Cache source-signature transcripts in edit/transcripts/ and measured regions in edit/regions/.\n\nUsage: uv run python helpers/transcribe.py VIDEO --edit-dir EDIT\nUse --language only when the source language is known, --prompt for supplied terminology, and --force to invalidate the cached transcript. Never translate recorded speech unless requested.'

from __future__ import annotations

import argparse
import bisect
import importlib.util
import os
import platform
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

from common import file_signature, load_json, save_json
from icloud import CloudOnly, ensure_local
from speech_regions import (
    SAMPLE_RATE,
    AudioError,
    complement,
    detect_silences,
    estimate_noise,
    load_mono,
    probe_average,
    regions_path,
    window_levels,
)

DEFAULT_MODEL = "large-v3-turbo"
# Empty default prompt preserves the recording language and avoids lexical bias.
# A caller can supply an explicit transcription prompt when appropriate.
DEFAULT_PROMPT = ""
MIN_SILENCE_S = 0.25      # same default as speech_regions.py
LONG_GAP_S = 2.0          # silences at least this long get shortened before Whisper
KEEP_GAP_S = 0.5          # silence kept on each side of a shortened gap
CHUNK_TARGET_S = 28.0     # one Whisper window per chunk (spec cap is 300 s)
CHUNK_MIN_S = 12.0
GUARD_SILENCE_S = 1.0     # words entirely inside a silence this long are dropped
EDGE_EPS = 0.02
SPARSE_SPEECH_WPS = 1.0      # fewer words than this per second of measured voice: Whisper likely returned the prompt
SPARSE_SPEECH_MIN_S = 4.0    # ... only judged with at least this much voice

MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}

# Whisper's classic inventions on silence (subtitle credits, YouTube outros).
KNOWN_HALLUCINATIONS = (
    "legendas pela comunidade amara org",
    "legendas pela comunidade amaraorg",
    "legenda adriana zanotto",
    "legendas adriana zanotto",
    "transcricao e legendas",
    "obrigado por assistir",
    "obrigada por assistir",
    "inscreva se no canal",
    "se inscreva no canal",
    "ate o proximo video",
    "ate a proxima",
    "tchau tchau",
    "e ai",
    "subtitles by the amara org community",
    "thanks for watching",
)

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".mkv", ".webm")


class EngineError(RuntimeError):
    pass


class NameClashError(RuntimeError):
    """Two different videos would share transcripts/<stem>.json (video.mov + video.mp4)."""


def same_video(a: Path, b: Path) -> bool:
    """Same file or identical contents; size alone cannot identify a recording."""
    try:
        from common import content_hash
        return os.path.samefile(a, b) or (os.stat(a).st_size == os.stat(b).st_size and content_hash(a) == content_hash(b))
    except OSError:
        return False


def check_name_clash(out_path: Path, video: Path) -> None:
    """Refuse to overwrite the transcript of ANOTHER video that has the same name without extension."""
    data = load_json(out_path) if out_path.exists() else None
    if not isinstance(data, dict) or not data.get("source"):
        return
    other = Path(data["source"])
    if other == video or not other.exists() or same_video(other, video):
        return
    raise NameClashError(
        f'{video.name} and {other.name} share a filename stem, so one transcript would overwrite another ({out_path.name}). Rename one of them (for example {video.stem}_2{video.suffix}) and run again.')


# ---------------------------------------------------------------- engines

def default_engine() -> str:
    # The pyproject installs mlx-whisper only on Apple Silicon with Darwin 23+; follow what is actually installed.
    apple = sys.platform == "darwin" and platform.machine() == "arm64"
    return "mlx" if apple and importlib.util.find_spec("mlx_whisper") else "faster-whisper"


def engine_label(engine: str, model: str) -> str:
    name = Path(model).name if ("/" in model or os.sep in model) else model
    return f"{('mlx-whisper' if engine == 'mlx' else 'faster-whisper')}/{name}"


def _mlx_model_path(model: str) -> str:
    if Path(model).exists():
        return str(model)
    repo = MLX_REPOS.get(model, model)
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(repo, local_files_only=True)
    except Exception:
        print(f'downloading model {repo} (first use only)...', file=sys.stderr, flush=True)
        return repo


def _run_mlx(audio: np.ndarray, model: str, language: str | None, prompt: str | None) -> tuple[list[dict], str | None]:
    try:
        import mlx_whisper
    except ImportError as exc:
        raise EngineError('mlx-whisper is not installed. Run: uv sync') from exc
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=_mlx_model_path(model),
        language=language,
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=prompt,
        verbose=None,
    )
    segments = []
    for seg in result.get("segments", []):
        words = [{"word": w.get("word", ""), "start": float(w["start"]), "end": float(w["end"]),
                  "probability": float(w.get("probability", 0.0))} for w in seg.get("words", [])]
        segments.append({"start": float(seg["start"]), "end": float(seg["end"]),
                         "text": seg.get("text", ""), "words": words})
    return segments, result.get("language")


_FASTER_MODELS: dict[str, object] = {}


def _run_faster(audio: np.ndarray, model: str, language: str | None, prompt: str | None) -> tuple[list[dict], str | None]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise EngineError('faster-whisper is not installed. Run: uv sync') from exc
    if model not in _FASTER_MODELS:
        _FASTER_MODELS[model] = WhisperModel(model, device="cpu", compute_type="int8",
                                             cpu_threads=os.cpu_count() or 4)
    fw = _FASTER_MODELS[model]
    seg_iter, info = fw.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=prompt,
        beam_size=5,
        vad_filter=False,
    )
    segments = []
    for seg in seg_iter:  # lazy generator: the work happens while iterating
        words = [{"word": w.word, "start": float(w.start), "end": float(w.end),
                  "probability": float(w.probability)} for w in (seg.words or [])]
        segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text, "words": words})
    return segments, getattr(info, "language", None)


def run_whisper(audio: np.ndarray, engine: str, model: str, language: str | None,
                prompt: str | None) -> tuple[list[dict], str | None]:
    if engine == "mlx":
        return _run_mlx(audio, model, language, prompt)
    return _run_faster(audio, model, language, prompt)


# ---------------------------------------------------------------- long-gap compaction

class TimeMap:
    """Kept pieces of the original timeline, laid end to end in the compacted audio."""

    def __init__(self, pieces: list[tuple[float, float]]):
        self.orig = pieces
        self.comp_starts: list[float] = []
        t = 0.0
        for s, e in pieces:
            self.comp_starts.append(t)
            t += e - s
        self.length = t

    def to_orig(self, t: float, side: str = "start") -> float:
        if not self.orig:
            return t
        if side == "start":
            i = bisect.bisect_right(self.comp_starts, t) - 1
        else:
            i = bisect.bisect_left(self.comp_starts, t) - 1
        i = min(max(i, 0), len(self.orig) - 1)
        s, e = self.orig[i]
        return min(max(s + (t - self.comp_starts[i]), s), e)

    def span_to_orig(self, cs: float, ce: float) -> tuple[float, float]:
        """Map a word. A word that straddles a join (Whisper pulled its start back across a
        removed gap) is kept inside the piece that holds most of it, ties going to the later one."""
        if not self.orig:
            return cs, ce
        i = min(max(bisect.bisect_right(self.comp_starts, cs) - 1, 0), len(self.orig) - 1)
        j = min(max(bisect.bisect_left(self.comp_starts, ce) - 1, 0), len(self.orig) - 1)
        if j <= i:
            return self.to_orig(cs, "start"), self.to_orig(ce, "end")
        best, best_len = i, -1.0
        for k in range(i, j + 1):
            k0 = self.comp_starts[k]
            k1 = k0 + (self.orig[k][1] - self.orig[k][0])
            part = min(ce, k1) - max(cs, k0)
            if part >= best_len:
                best, best_len = k, part
        k0 = self.comp_starts[best]
        s, e = self.orig[best]
        a = s + max(cs - k0, 0.0)
        b = s + min(ce - k0, e - s)
        return min(a, e), max(min(b, e), min(a, e))


def plan_pieces(silences: list[list[float]], duration: float) -> list[tuple[float, float]]:
    removed = []
    for s, e in silences:
        if e - s < LONG_GAP_S:
            continue
        rs = 0.0 if s <= EDGE_EPS else s + KEEP_GAP_S
        re_ = duration if e >= duration - EDGE_EPS else e - KEEP_GAP_S
        if re_ - rs > 0.05:
            removed.append([rs, re_])
    return [(s, e) for s, e in complement(removed, 0.0, duration) if e - s > 0.01]


def plan_chunks(audio: np.ndarray, sr: int = SAMPLE_RATE, max_s: float = CHUNK_TARGET_S,
                min_s: float = CHUNK_MIN_S) -> list[tuple[int, int]]:
    """Sample ranges of <= max_s, each cut at a quiet point (a pause) after at least min_s.

    Chunks fit one Whisper window, so the prompt applies to all of them: with
    condition_on_previous_text=False Whisper only uses the prompt on the first window
    of each call, and later windows came out without punctuation.
    """
    total = len(audio)
    max_n = int(max_s * sr)
    if total <= max_n:
        return [(0, total)]
    win = int(0.05 * sr)
    rms_db, _ = window_levels(audio, sr)
    smooth = np.convolve(rms_db, np.ones(5) / 5, mode="same") if len(rms_db) >= 5 else rms_db
    bounds, start = [], 0
    while total - start > max_n:
        lo_w = (start + int(min_s * sr)) // win
        hi_w = min((start + max_n) // win, len(smooth))
        seg = smooth[lo_w:hi_w]
        if len(seg) == 0:
            cut = start + max_n
        else:
            quiet = np.where(seg <= seg.min() + 1.5)[0]
            cut = (lo_w + int(quiet[-1])) * win + win // 2
        cut = min(max(cut, start + win), total)
        bounds.append((start, cut))
        start = cut
    if total > start:
        if bounds and total - start < 3 * sr and total - bounds[-1][0] <= 30 * sr:
            bounds[-1] = (bounds[-1][0], total)
        else:
            bounds.append((start, total))
    return bounds


# ---------------------------------------------------------------- word cleanup

def _normal(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", t)


def _has_letters(text: str) -> bool:
    return any(c.isalnum() for c in text)


def collect_words(segments: list[dict], offset: float, tmap: TimeMap, seg_base: int) -> list[dict]:
    words: list[dict] = []
    for si, seg in enumerate(segments):
        first_in_seg = True
        for w in seg["words"]:
            raw = w.get("word") or ""
            text = raw.strip()
            if not text:
                continue
            s, e = tmap.span_to_orig(offset + w["start"], offset + max(w["end"], w["start"]))
            same_seg = bool(words) and words[-1]["seg"] == seg_base + si
            # Punctuation alone, or a piece with no leading space ("20" + ".8."), belongs to the previous word.
            if same_seg and (not _has_letters(text) or (not first_in_seg and not raw[:1].isspace())):
                prev = words[-1]
                prev["text"] += text
                prev["end"] = max(prev["end"], e)
                prev["prob"] = min(prev["prob"], w["probability"])
                continue
            first_in_seg = False
            if not _has_letters(text):
                continue
            words.append({"text": text, "start": s, "end": max(e, s), "prob": w["probability"], "seg": seg_base + si})
    return words


def _inside(w: dict, s: float, e: float) -> bool:
    return w["start"] >= s - EDGE_EPS and w["end"] <= e + EDGE_EPS


def silence_guard(words: list[dict], silences: list[list[float]]) -> tuple[list[dict], int]:
    long_sil = [x for x in silences if x[1] - x[0] >= GUARD_SILENCE_S]
    if not long_sil:
        return words, 0
    starts = [x[0] for x in long_sil]
    kept = []
    for w in words:
        i = bisect.bisect_right(starts, w["start"] + EDGE_EPS) - 1
        if i >= 0 and _inside(w, *long_sil[i]):
            continue
        kept.append(w)
    return kept, len(words) - len(kept)


def _speech_fraction(s: float, e: float, silences: list[list[float]]) -> float:
    if e - s <= 0:
        mid = s
        return 0.0 if any(a <= mid <= b for a, b in silences) else 1.0
    sil = sum(max(0.0, min(e, b) - max(s, a)) for a, b in silences)
    return max(0.0, 1.0 - sil / (e - s))


def loop_guard(words: list[dict], silences: list[list[float]]) -> tuple[list[dict], int]:
    """Drop repeated n-gram loops (Whisper stuck repeating itself).

    A run needs 5+ copies of one word, 4+ copies of 2-3 words or 3+ copies of longer
    n-grams. Copies after the first are dropped only when they look invented: low
    confidence, squeezed timing, or sitting mostly on silence. A real retake said three
    times keeps all its copies.
    """
    toks = [_normal(w["text"]) for w in words]
    drop = [False] * len(words)
    # The same word twice on top of itself in time (a segment emitted twice) is never real speech.
    for i in range(1, len(words)):
        for j in range(max(0, i - 8), i):
            if not drop[j] and toks[i] and toks[i] == toks[j] and words[i]["start"] < words[j]["end"] - 0.05:
                drop[i] = True
                break
    words = [w for w, d in zip(words, drop) if not d]
    n_overlap = sum(drop)
    toks = [_normal(w["text"]) for w in words]
    drop = [False] * len(words)
    i, n_words = 0, len(words)
    while i < n_words:
        found = None
        for n in range(1, 9):
            if i + 2 * n > n_words:
                break
            pat = toks[i:i + n]
            if not all(pat):
                continue
            k = 1
            while toks[i + k * n:i + (k + 1) * n] == pat:
                k += 1
            need = 5 if n == 1 else 4 if n <= 3 else 3
            if k >= need:
                found = (n, k)
                break
        if not found:
            i += 1
            continue
        n, k = found
        for c in range(1, k):
            copy = words[i + c * n:i + (c + 1) * n]
            mean_prob = sum(w["prob"] for w in copy) / n
            mean_dur = sum(w["end"] - w["start"] for w in copy) / n
            speech = _speech_fraction(copy[0]["start"], copy[-1]["end"], silences)
            if mean_prob < 0.6 or mean_dur < 0.08 or speech < 0.5:
                for j in range(i + c * n, i + (c + 1) * n):
                    drop[j] = True
        i += k * n
    kept = [w for w, d in zip(words, drop) if not d]
    return kept, len(words) - len(kept) + n_overlap


def known_phrase_guard(words: list[dict], silences: list[list[float]]) -> tuple[list[dict], int]:
    """Drop whole segments that are a known subtitle-credit phrase sitting mostly on silence.

    A creator can really say "obrigado por assistir", so the text alone is never enough.
    """
    by_seg: dict[int, list[int]] = {}
    for idx, w in enumerate(words):
        by_seg.setdefault(w["seg"], []).append(idx)
    drop: set[int] = set()
    for idxs in by_seg.values():
        seg_words = [words[i] for i in idxs]
        text = " ".join(_normal(w["text"]) for w in seg_words).strip()
        if text not in KNOWN_HALLUCINATIONS:
            continue
        if _speech_fraction(seg_words[0]["start"], seg_words[-1]["end"], silences) < 0.5:
            drop.update(idxs)
    kept = [w for i, w in enumerate(words) if i not in drop]
    return kept, len(words) - len(kept)


_FINAL_PUNCT = re.compile(r"[.!?…]$")   # sentence end or ellipsis (not commas)
EDGE_TAIL_S = 0.25                         # a speech island this short next to a sentence end is a neighbour's tail


def refine_edges(words: list[dict], silences: list[list[float]]) -> int:
    """Move word edges that fall inside a measured silence onto speech. In place.

    The part of the word outside the pauses is split into speech islands:
    - one island (the word starts or ends inside a pause): the word becomes that island;
    - several islands (a pause inside the word): the longest island wins, after dropping a
      short first island when the previous word ends a sentence (it is that word's tail) and
      a short last island when this word ends a sentence or has "..." (the next word's head);
    - no island (entirely inside a short pause): snap after the pause, or before it when the
      word ends a sentence and the previous one does not.
    """
    if not silences or not words:
        return 0
    starts = [s for s, _ in silences]
    changed = 0
    for idx, w in enumerate(words):
        ws, we = w["start"], w["end"]
        j = bisect.bisect_right(starts, we) - 1
        hits = []
        while j >= 0 and silences[j][1] > ws:
            if silences[j][0] < we:
                hits.append(silences[j])
            j -= 1
        if not hits:
            continue
        hits.sort()
        dur = we - ws
        own_final = bool(_FINAL_PUNCT.search(w["text"]))
        prev_final = idx > 0 and bool(_FINAL_PUNCT.search(words[idx - 1]["text"]))
        islands = [i for i in complement([[max(a, ws), min(b, we)] for a, b in hits], ws, we) if i[1] - i[0] > 1e-3]
        if not islands:
            ss, se = hits[0]
            if own_final and not prev_final:
                ns, ne = max(ss - min(dur, 0.3), 0.0), ss
            else:
                ns, ne = se, se + min(dur, 0.3)
        else:
            cands = islands
            if len(cands) > 1 and prev_final and cands[0][1] - cands[0][0] <= EDGE_TAIL_S:
                cands = cands[1:]
            if len(cands) > 1 and own_final and cands[-1][1] - cands[-1][0] <= EDGE_TAIL_S:
                cands = cands[:-1]
            ns, ne = max(cands, key=lambda i: (round(i[1] - i[0], 3), i[0]))
        if ne - ns < 0.02:
            ne = ns + min(max(dur, 0.02), 0.08)
        if abs(ns - ws) > 1e-6 or abs(ne - we) > 1e-6:
            w["start"], w["end"] = ns, ne
            changed += 1
    words.sort(key=lambda x: x["start"])
    for a, b in zip(words, words[1:]):
        if b["start"] < a["end"]:
            a["end"] = max(a["start"], b["start"])
    return changed


def build_segments(words: list[dict]) -> list[dict]:
    groups: dict[int, list[dict]] = {}
    for w in words:
        groups.setdefault(w["seg"], []).append(w)
    segs = []
    for ws in groups.values():
        text = " ".join(w["text"] for w in ws)
        segs.append({"start": round(ws[0]["start"], 3), "end": round(max(w["end"] for w in ws), 3), "text": text})
    segs.sort(key=lambda s: s["start"])
    return segs


def notice_sparse_speech(words: list[dict], speech_s: float, prompt: str | None) -> str | None:
    """Warning when the transcript has far fewer words than the measured voice asks for.

    Normal speech is 2 to 3 words per second. Under SPARSE_SPEECH_WPS, Whisper most likely returned the prompt
    (its fillers) or skipped a stretch: seen on a clip that started in the middle of a sentence.
    """
    if speech_s < SPARSE_SPEECH_MIN_S:
        return None
    n = len(words)
    wps = n / speech_s
    if wps >= SPARSE_SPEECH_WPS:
        return None
    vocab = {_normal(x) for x in re.findall(r"\w+", f"{DEFAULT_PROMPT} {prompt or ''}")} - {""}
    eco = [w["text"] for w in words if _normal(w["text"]) in vocab]
    msg = (f'only {n} words for {speech_s:.0f} s of measured speech ({wps:.1f} per second; typical speech is two to three). ')
    if n and len(eco) * 2 >= n:
        msg += f'''Most recognized text matches the prompt ("{' '.join(eco[:8])}"): Whisper returned the prompt rather than recorded speech. '''
    msg += ('If this is an excerpt from a longer recording, copy it again from a natural pause before a complete phrase and retranscribe. Otherwise retry with --no-prompt --force. Ignore this warning only if the interval contains no speech')
    return msg


# ---------------------------------------------------------------- main entry

def transcript_path(edit_dir: Path | str, video: Path | str) -> Path:
    return Path(edit_dir) / "transcripts" / f'{Path(video).stem}.json'


def build_prompt(extra: str | None, use_default: bool) -> str | None:
    # An empty prompt is not "no prompt": Whisper encodes it as one space token and decodes with NaN log-probabilities.
    parts = [x.strip() for x in ((DEFAULT_PROMPT if use_default else None), extra) if x and x.strip()]
    return " ".join(parts) or None


def is_cached(out_path: Path, video: Path, engine: str, model: str, language: str | None, prompt: str | None) -> bool:
    data = load_json(out_path)
    if not isinstance(data, dict):
        return False
    from common import content_hash
    if data.get('cache_version') != 2 or data.get('source_sha256') != content_hash(video):
        return False
    if data.get("engine") != engine_label(engine, model):
        return False
    if data.get('requested_language') != (language or 'auto'):
        return False
    return data.get("prompt", DEFAULT_PROMPT) == prompt


def transcribe_one(video: Path | str, edit_dir: Path | str | None = None, language: str | None = None,
                   model: str = DEFAULT_MODEL, force: bool = False, prompt: str | None = DEFAULT_PROMPT,
                   engine: str | None = None, verbose: bool = True) -> Path:
    """Transcribe one video. Returns the transcript path (cached when the source did not change)."""
    prompt = (prompt or "").strip() or None
    video = Path(video).absolute()
    if not video.exists():
        raise FileNotFoundError(f'video not found: {video}')
    edit_dir = Path(edit_dir).absolute() if edit_dir else video.parent / "edit"
    from project_contract import managed_config
    managed = managed_config(edit_dir)
    if managed:
        configured = managed['workflow']['language']['transcription']
        if language not in (None, 'auto', configured):
            raise ValueError('Transcription language conflicts with confirmed settings')
        language = configured
    engine = engine or default_engine()
    language = None if (language or "").lower() in ("", "auto") else language
    out_path = transcript_path(edit_dir, video)
    check_name_clash(out_path, video)

    if not force and is_cached(out_path, video, engine, model, language, prompt):
        if verbose:
            data = load_json(out_path)
            print(f"{video.stem}: cached ({len(data.get('words', []))} words); {out_path}")
            if data.get('warning'):
                print(f"{video.stem}: WARNING: {data['warning']}", flush=True)
        return out_path

    ensure_local([video], notify=(print if verbose else (lambda *_: None)))
    from common import content_hash
    source_hash = content_hash(video)
    t0 = time.time()
    info = probe_average(video)
    duration = float(info.get("duration") or 0.0)
    mono, audio_desc = load_mono(video)
    if duration <= 0:
        duration = len(mono) / SAMPLE_RATE
    if verbose and audio_desc.get("reason"):
        side = {"left": "left", "right": "right"}.get(audio_desc["used"], audio_desc["used"])
        print(f"{video.stem}: using only channel {side} ({audio_desc['reason']})", flush=True)

    est = estimate_noise(mono)
    audio_len = len(mono) / SAMPLE_RATE
    silences = [[min(a, duration), min(b, duration)] for a, b in detect_silences(mono, est["threshold_db"], MIN_SILENCE_S)]
    silences = [x for x in silences if x[1] > x[0]]
    if silences and silences[-1][1] >= min(audio_len, duration) - EDGE_EPS:
        silences[-1][1] = duration
    t_audio = time.time() - t0

    pieces = plan_pieces(silences, audio_len)
    tmap = TimeMap(pieces)
    comp = np.concatenate([mono[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)] for s, e in pieces]) if pieces else np.zeros(0, np.float32)
    comp = np.ascontiguousarray(comp, dtype=np.float32)

    t1 = time.time()
    raw_words: list[dict] = []
    detected_lang = language
    seg_base = 0
    speech_total = sum(e - s for s, e in complement(silences, 0.0, audio_len))
    if len(comp) >= int(0.2 * SAMPLE_RATE) and speech_total >= 0.1:
        for a, b in plan_chunks(comp):
            chunk = comp[a:b]
            if len(chunk) < int(0.2 * SAMPLE_RATE):
                continue
            segments, lang = run_whisper(chunk, engine, model, language, prompt)
            detected_lang = detected_lang or lang
            raw_words.extend(collect_words(segments, a / SAMPLE_RATE, tmap, seg_base))
            seg_base += len(segments)
    t_whisper = time.time() - t1

    words = sorted(raw_words, key=lambda w: w["start"])
    words, n_sil = silence_guard(words, silences)
    words, n_loop = loop_guard(words, silences)
    words, n_known = known_phrase_guard(words, silences)
    n_refined = refine_edges(words, silences)

    for w in words:
        w["start"] = round(min(max(w["start"], 0.0), duration), 3)
        w["end"] = round(min(max(w["end"], w["start"]), duration), 3)
    segments = build_segments(words)
    out_words = [{"text": w["text"], "start": w["start"], "end": w["end"], "prob": round(w["prob"], 3)} for w in words]
    notice = notice_sparse_speech(out_words, speech_total, prompt)

    elapsed = time.time() - t0
    if content_hash(video) != source_hash:
        raise ValueError('Source changed during transcription; inspect it again before continuing')
    doc = {
        "source": str(video),
        "source_sig": file_signature(video),
        "source_sha256": source_hash,
        "cache_version": 2,
        "requested_language": language or 'auto',
        "stem": video.stem,
        "duration": round(duration, 3),
        "language": detected_lang or language or "",
        "engine": engine_label(engine, model),
        "words": out_words,
        "segments": segments,
        "prompt": prompt,
        "audio": {**audio_desc, "noise_db": est["threshold_db"], "shortened_gaps": len(pieces) - 1 if pieces else 0},
        "guard": {"silence": n_sil, "loop": n_loop, "known_phrase": n_known, "refined_edges": n_refined},
        "timing": {"audio_s": round(t_audio, 2), "whisper_s": round(t_whisper, 2), "total_s": round(elapsed, 2)},
        'voice_seconds': round(speech_total, 2),
    }
    if notice:
        doc['warning'] = notice
    save_json(out_path, doc)
    _write_regions_if_stale(edit_dir, video, duration, est, silences, audio_desc)

    if verbose:
        per_min = elapsed / (duration / 60.0) if duration > 0 else 0.0
        removed = n_sil + n_loop + n_known
        extra = f' · {removed} hallucinated words removed' if removed else ""
        print(f'{video.stem}: {len(out_words)} words, {duration:.1f}s of audio, {elapsed:.1f}s ({per_min:.1f}s per minute){extra} · {out_path}', flush=True)
        if notice:
            print(f'{video.stem}: WARNING: {notice}', flush=True)
    return out_path


def _write_regions_if_stale(edit_dir: Path, video: Path, duration: float, est: dict,
                            silences: list[list[float]], audio_desc: dict) -> None:
    path = regions_path(edit_dir, video.stem)
    cached = load_json(path)
    if cached and cached.get("source_sig") == file_signature(video):
        return
    r = lambda x: round(float(x), 3)  # noqa: E731
    save_json(path, {
        "source": str(video),
        "source_sig": file_signature(video),
        "stem": video.stem,
        "duration": r(duration),
        "noise_db": est["threshold_db"],
        "noise_mode": "auto",
        "min_silence": MIN_SILENCE_S,
        "speech": [[r(s), r(e)] for s, e in complement(silences, 0.0, duration)],
        "silences": [[r(s), r(e)] for s, e in silences],
        "audio": audio_desc,
        "noise_estimate": est,
    })


def main() -> None:
    ap = argparse.ArgumentParser(description='Transcribe a video locally with word timing using Whisper.')
    ap.add_argument('video', type=Path, help='video file')
    ap.add_argument("--edit-dir", type=Path, default=None, help='edit folder (default: <video folder>/edit)', dest='edit_dir')
    ap.add_argument("--language", default="auto", help='recorded language; auto detects it by default', dest='language')
    ap.add_argument("--model", default=DEFAULT_MODEL, help='Whisper model (default large-v3-turbo)', dest='model')
    ap.add_argument("--force", action="store_true", help='transcribe again even when cached', dest='force')
    ap.add_argument("--prompt", default=None,
                    help='proper names in the recording to guide spelling, for example "Kiwify, Hubla"', dest='prompt')
    ap.add_argument("--no-prompt", action="store_true", help='disable the transcription prompt', dest='in_prompt')
    ap.add_argument("--engine", choices=("auto", "mlx", "faster-whisper"), default="auto",
                    help='engine (default: mlx on Apple Silicon Macs, faster-whisper elsewhere)', dest='engine')
    args = ap.parse_args()

    video = args.video.absolute()
    if not video.exists():
        sys.exit(f'video not found: {video}')
    if video.is_dir():
        sys.exit(f'This is a directory, not a video: {video}\\nFor a complete folder, use transcribe_batch.py')
    try:
        transcribe_one(
            video,
            edit_dir=args.edit_dir,
            language=args.language,
            model=args.model,
            force=args.force,
            prompt=build_prompt(args.prompt, not args.in_prompt),
            engine=None if args.engine == "auto" else args.engine,
        )
    except (AudioError, EngineError, FileNotFoundError, NameClashError, CloudOnly) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
