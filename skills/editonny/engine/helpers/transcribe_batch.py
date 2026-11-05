# Adapted from browser-use/video-use (MIT, (c) 2026 Browser Use). See THIRD_PARTY_NOTICES.md.
"""Transcribe every video in a folder, one after the other.

Not recursive; the edit/ folder is skipped. Each file is cached by its signature
(see transcribe.py), and a failure on one file does not stop the others.

Before opening anything, every video that is only in iCloud (macOS placeholder) is downloaded or the batch stops
with a message: ffprobe and ffmpeg hang on such a file without any error (helpers/icloud.py). At the end, the
videos with a warning (too few words for the voice measured) are listed again.

Usage:
    uv run python helpers/transcribe_batch.py <videos_dir> [--language pt] [--edit-dir DIR]
        [--model large-v3-turbo] [--force] [--prompt "Kiwify, Hubla"] [--no-prompt]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from common import load_json
from icloud import CloudOnly, ensure_local
from speech_regions import AudioError
from transcribe import (
    DEFAULT_MODEL,
    VIDEO_EXTS,
    EngineError,
    build_prompt,
    default_engine,
    is_cached,
    transcribe_one,
    transcript_path,
)


def find_videos(videos_dir: Path) -> list[Path]:
    out = []
    for p in sorted(videos_dir.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith(".") or p.name == "edit":
            continue
        if p.suffix.lower() in VIDEO_EXTS and p.is_file():
            out.append(p)
    return out


def duplicate_names(videos: list[Path]) -> list[list[Path]]:
    """Videos that share a name without extension (case-insensitive): transcripts/<stem>.json,
    regions/<stem>.json and the EDL source key would collide."""
    groups: dict[str, list[Path]] = {}
    for v in videos:
        groups.setdefault(v.stem.lower(), []).append(v)
    return [g for g in groups.values() if len(g) > 1]


def main() -> None:
    ap = argparse.ArgumentParser(description='Transcribe every video in a folder sequentially, with caching.')
    ap.add_argument('videos_dir', type=Path, help='folder containing original videos')
    ap.add_argument("--edit-dir", type=Path, default=None, help='edit folder (default: <folder>/edit)', dest='edit_dir')
    ap.add_argument("--language", default="auto", help='recorded language; auto detects it by default', dest='language')
    ap.add_argument("--model", default=DEFAULT_MODEL, help='Whisper model (default large-v3-turbo)', dest='model')
    ap.add_argument("--force", action="store_true", help='transcribe again even when cached', dest='force')
    ap.add_argument("--prompt", default=None, help='proper names occurring in the recordings, for example "Kiwify, Hubla"', dest='prompt')
    ap.add_argument("--no-prompt", action="store_true", help='disable the transcription prompt', dest='in_prompt')
    args = ap.parse_args()

    videos_dir = args.videos_dir.absolute()
    if not videos_dir.is_dir():
        sys.exit(f'folder not found: {videos_dir}')
    edit_dir = (args.edit_dir.absolute() if args.edit_dir else videos_dir / "edit")
    videos = find_videos(videos_dir)
    if not videos:
        sys.exit(f"no video ({', '.join(VIDEO_EXTS)}) in {videos_dir}")
    clashes = duplicate_names(videos)
    if clashes:
        pairs = "; ".join(" and ".join(v.name for v in g) for g in clashes)
        sys.exit(f"videos have matching stems and different extensions: {pairs}. Transcribing would overwrite another source's transcript. Rename one file in each pair, for example {clashes[0][-1].stem}_2{clashes[0][-1].suffix}, and run again. No files were transcribed.")

    try:
        ensure_local(videos)
    except CloudOnly as exc:
        sys.exit(f'{exc}\nNo files were transcribed.')

    prompt = build_prompt(args.prompt, not args.in_prompt)
    engine = default_engine()
    language = None if args.language.lower() == "auto" else args.language
    cached = [v for v in videos
              if not args.force and is_cached(transcript_path(edit_dir, v), v.absolute(), engine, args.model, language, prompt)]
    print(f'{len(videos)} videos in {videos_dir.name} ({len(cached)} cached, {len(videos) - len(cached)} remaining to transcribe)',
          flush=True)

    t0 = time.time()
    failures: list[tuple[Path, str]] = []
    for i, video in enumerate(videos, 1):
        print(f'[{i}/{len(videos)}] {video.name}', flush=True)
        try:
            transcribe_one(video, edit_dir=edit_dir, language=args.language, model=args.model,
                           force=args.force, prompt=prompt)
        except (AudioError, EngineError, FileNotFoundError, RuntimeError, OSError) as exc:
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            failures.append((video, msg))
            print(f'  failed: {msg}', flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            failures.append((video, f'{exc.__class__.__name__}: {exc}'))
            print(f'  failed: {exc.__class__.__name__}: {exc}', flush=True)

    dt = time.time() - t0
    ok = len(videos) - len(failures)
    print(f"ready in {dt:.1f}s: {ok} of {len(videos)} transcribed in {edit_dir / 'transcripts'}")
    warnings_value = [(v, (load_json(transcript_path(edit_dir, v)) or {}).get('warning')) for v in videos]
    warnings_value = [(v, a) for v, a in warnings_value if a]
    if warnings_value:
        print(f'{len(warnings_value)} with warnings; inspect before selecting phrases:')
        for video, msg in warnings_value:
            print(f'  {video.name}: {msg}')
    if failures:
        print(f'{len(failures)} with errors:')
        for video, msg in failures:
            print(f'  {video.name}: {msg}')
        sys.exit(1)
    skill_root = Path(__file__).resolve().parent.parent
    print(f"next step: uv run --directory '{skill_root}' python helpers/pack_transcripts.py --edit-dir '{edit_dir}'")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    main()
