"""Optional soundtrack made in the buyer's OWN ElevenLabs account (Eleven Music, paid per use).

Each buyer uses their own key (ELEVENLABS_API_KEY in the environment or in `.env` in the skill root) and
their own plan: nothing generated ships with the product. Without the key the helper exits 2 and tells the
user how to configure their key locally. The helper never prints or logs the key
(see elevenlabs_api.redact).

The track is instrumental by default (a sung voice fights the speaker) and lasts the cut duration from
<edit>/cut_timeline.json rounded up plus 2 s, kept between 3 s and 300 s. Each variation is one request.
Files go to <edit>/assets/music_<n>.mp3 (or --out) and every file gets an entry in
<edit>/assets/credits.json. Prompts that name real artists, songs, soundtracks or brand sounds are refused
before any request. `--dry-run` prints the request and the estimated credits without touching the network.

Moods (helpers/music_moods.json): `--mood sad` fills the prompt with the mood's ready English prompt;
words given as the prompt are appended to it. `--library` (needs --mood) also puts every generated file in
the person's personal soundtrack library (helpers/music_library.py, outside the skill folder), with the source,
prompt, model and usage note recorded. With --library the edit folder is optional: without it the file
waits in <library>/_incoming/ until it is indexed, and the length defaults to 60 s (with an edit folder:
the longer of 60 s and the cut + 2 s), so one track covers most ads without repeating.

Usage:
    uv run python helpers/gen_music.py "<prompt in English>" --edit-dir DIR [--seconds S]
        [--no-instrumental] [--out NAME.mp3] [--variations N] [--model ID] [--dry-run]
    uv run python helpers/gen_music.py [extra words] --mood ID [--library] [--title T] [--edit-dir DIR] ...
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elevenlabs_api as el  # noqa: E402
from common import load_json, save_json  # noqa: E402

MODELS = ("music_v2_5", "music_v2", "music_v1")
DEFAULT_MODEL = MODELS[0]               # the "latest, most advanced" model in the ElevenLabs model list
MIN_SECONDS = 3.0                       # API: music_length_ms from 3000
MAX_SECONDS = 300.0                     # API accepts up to 600000 ms; the product page says 5 minutes
EXTRA_SECONDS = 2
LIBRARY_SECONDS = 60.0                  # a library track should cover most ads (20 to 45 s) without looping
MAX_VARIATIONS = 3
SOURCE = 'ElevenLabs Music (user account)'
LIBRARY_NOTE = (f"{el.COMMERCIAL_NOTE}; generated in the user's account for their own videos only; do not redistribute, sell, or bundle")
EXAMPLE = "upbeat modern pop instrumental, 110-120 bpm, positive, no vocals, leaves room for voice"
TIMEOUT = 300
MUSIC_LINE = '"volume_db": -15, "duck": true, "fade_out": 1.5}'


def cut_duration(edit: Path) -> float | None:
    try:
        timeline = load_json(edit / "cut_timeline.json")
        duration = float((timeline or {}).get("duration") or 0)
    except (ValueError, TypeError, AttributeError, OSError):
        return None
    return duration if duration > 0 and math.isfinite(duration) else None


def pick_seconds(edit: Path | None, asked: float | None, library: bool = False) -> tuple[float, str, list[str]]:
    """-> (seconds, where it came from, warnings)."""
    notes = []
    if asked is None:
        duration = cut_duration(edit) if edit else None
        if library:
            if duration is not None and math.ceil(duration - 1e-6) + EXTRA_SECONDS > LIBRARY_SECONDS:
                seconds = float(math.ceil(duration - 1e-6) + EXTRA_SECONDS)
                origin = f'cut of {duration:.2f} seconds rounded up plus {EXTRA_SECONDS} s'
            else:
                seconds = LIBRARY_SECONDS
                origin = f'library default ({LIBRARY_SECONDS:g} seconds; covers most short ads without looping)'
        elif duration is None:
            raise el.ElevenError('cut duration unavailable in cut_timeline.json. Render the cut first or provide --seconds', 2)
        else:
            seconds = float(math.ceil(duration - 1e-6) + EXTRA_SECONDS)
            origin = f'cut of {duration:.2f} seconds rounded up plus {EXTRA_SECONDS} s'
    else:
        if not math.isfinite(asked) or asked <= 0:
            raise el.ElevenError('--seconds must be positive, for example 32', 2)
        seconds, origin = float(asked), "requested with --seconds"
    if seconds < MIN_SECONDS:
        notes.append(f'ElevenLabs requires at least {MIN_SECONDS:g} seconds; adjusted {seconds:g} seconds to {MIN_SECONDS:g} s')
        seconds = MIN_SECONDS
    elif seconds > MAX_SECONDS:
        notes.append(f'music is limited to {MAX_SECONDS:g} s: adjusted {seconds:g} seconds to {MAX_SECONDS:g} s')
        seconds = MAX_SECONDS
    return round(seconds, 3), origin, notes


def output_names(edit: Path, out: str | None, count: int) -> list[Path]:
    assets = edit / "assets"
    if out:
        stem = el.output_stem(out)
        names = [f'{stem}.mp3'] if count == 1 else [f'{stem}_{i}.mp3' for i in range(1, count + 1)]
    else:
        first = el.next_number(assets, "music_")
        names = [f'music_{first + i}.mp3' for i in range(count)]
    return el.free_paths(assets, names)


def inbox_names(lib: Path, mood_id: str, count: int) -> list[Path]:
    """Where generated files wait (inside the library) until they are indexed."""
    import music_library

    inbox = lib / music_library.INBOX_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    names = [f'{mood_id}_{stamp}.mp3'] if count == 1 else [f'{mood_id}_{stamp}_{i}.mp3' for i in range(1, count + 1)]
    return el.free_paths(inbox, names)


def resolve_mood(text: str, library: bool):
    """(mood dict, library folder) for --mood; the library is only read.

    With --library the library must be usable before anything is paid: a bad location, an unreadable index
    or a missing ffmpeg stop here. Without it, an unreadable index only hides the person's own moods."""
    import music_library

    try:
        lib = music_library.check_location()
        try:
            index = music_library.load_index(lib, required=False)
        except music_library.MusicError:
            if library:
                raise
            index = None
        if library:
            music_library.need_ffmpeg()
        mood = music_library.find_mood(index, text)
    except music_library.MusicError as e:
        raise el.ElevenError(str(e), e.code) from None
    if mood is None:
        raise el.ElevenError(music_library.unknown_mood_message(text, index), 2)
    return mood, lib


def to_library(lib: Path, path: Path, mood: dict, title: str, prompt: str, seconds: float, args,
               from_inbox: bool) -> dict:
    import music_library

    extra = {"model": args.model, "instrumental": bool(args.instrumental), "generated_seconds": seconds}
    try:
        return music_library.add_track(path, mood["id"], title=title, source=SOURCE, prompt=prompt,
                                 license_note=LIBRARY_NOTE, lib=lib, move_source=from_inbox, extra=extra)
    except Exception as e:  # noqa: BLE001  (a paid file: keep it and its note whatever went wrong)
        if from_inbox:
            # music_library.py add reads this note back (source, prompt, title, usage rule, model)
            try:
                save_json(path.with_suffix(".json"), {"source": SOURCE, "prompt": prompt, "mood": mood["id"],
                                                      "title": title, "license": LIBRARY_NOTE, **extra})
            except OSError:
                pass
        why = e if isinstance(e, music_library.MusicError) else f'{e.__class__.__name__}: {e}'
        retry = music_library.skill_cmd("music_library.py", f"add '{path}' --mood {mood['id']}")
        raise el.ElevenError(f'The track was generated and saved at {path}, but library import failed ({why}). To import without generating again, preserving source and usage metadata: {retry}',
                             1) from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate optional paid music through the user's ElevenLabs account. Save to the edit assets folder and optionally to a personal mood library with --library.")
    ap.add_argument('prompt', nargs="?", default=None,
                    help='music description, preferably English; with --mood, supplements the preset prompt')
    ap.add_argument("--edit-dir", default=None, help='session edit folder; required without --library', dest='edit_dir')
    ap.add_argument("--mood", help="use a mood's preset prompt, such as upbeat, inspiring, sad, tense, calm, epic, playful, or luxury", dest='mood_value')
    ap.add_argument("--library", action="store_true", help="save every generated track to the personal library under --mood", dest='library_value')
    ap.add_argument("--title", help='library track name (default: mood, AI, and date)', dest='title')
    ap.add_argument("--seconds", type=float, default=None,
                    help=f'duration in seconds (default: rounded-up cut duration plus {EXTRA_SECONDS} seconds; --library requires at least {LIBRARY_SECONDS:g} s; from {MIN_SECONDS:g} to {MAX_SECONDS:g})', dest='seconds')
    ap.add_argument("--instrumental", action=argparse.BooleanOptionalAction, default=True,
                    help='instrumental only, enabled by default; --no-instrumental disables it', dest='instrumental')
    ap.add_argument("--out", help='filename in assets/ (default: music_<n>.mp3)', dest='out')
    ap.add_argument("--variations", type=int, default=1,
                    help=f'number of variants, one request each (default 1; maximum {MAX_VARIATIONS})', dest='variations')
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS,
                    help=f'music model (default {DEFAULT_MODEL})', dest='model')
    ap.add_argument("--dry-run", action="store_true",
                    help='show the request and estimated cost without generation or network access', dest='dry_run')
    args = ap.parse_args(argv)

    key = None
    saved: list[str] = []

    def say(*parts: str, flush: bool = False) -> None:
        print(el.redact(" ".join(parts), key), flush=flush)

    try:
        extra_words = (args.prompt or "").strip()
        mood = lib = None
        if args.mood_value is not None:
            if not args.mood_value.strip():
                raise el.ElevenError('--mood is empty; provide a mood such as sad', 2)
            mood, lib = resolve_mood(args.mood_value, args.library_value and not args.dry_run)
            if not mood.get("prompt") and not extra_words:
                raise el.ElevenError(f"Mood '{mood['id']}' is a custom mood with no preset prompt. Supply an English description, for example '{EXAMPLE}').", 2)
            prompt = ", ".join(p for p in (mood.get("prompt") or "", extra_words) if p)
        else:
            prompt = extra_words
        if not prompt:
            raise el.ElevenError('the request is empty', 2)
        if args.library_value and mood is None:
            raise el.ElevenError('--library requires --mood; each library track belongs to a mood', 2)
        if not args.edit_dir and not args.library_value:
            raise el.ElevenError('missing --edit-dir. For library-only generation, use --mood and --library', 2)
        if args.out and not args.edit_dir:
            raise el.ElevenError('--out requires --edit-dir; library filenames are derived from the mood', 2)
        el.check_prompt(prompt, EXAMPLE)
        if not 1 <= args.variations <= MAX_VARIATIONS:
            raise el.ElevenError(f'--variations must be between 1 and {MAX_VARIATIONS} (each variant incurs a charge)', 2)
        edit = el.edit_folder(args.edit_dir) if args.edit_dir else None
        seconds, origin, notes = pick_seconds(edit, args.seconds, library=args.library_value)
        key = el.api_key()
        el.check_in_key(prompt, key)
        if args.title is not None:
            el.check_in_key(args.title, key)
        if not key and not args.dry_run:
            raise el.ElevenError(el.missing_key_message(
                "Generate music", 'using licensed user-supplied music, or without music'), 2)
        paths = output_names(edit, args.out, args.variations) if edit else\
            inbox_names(lib, mood["id"], args.variations)
        payload = {"prompt": prompt, "music_length_ms": int(round(seconds * 1000)),
                   "force_instrumental": bool(args.instrumental), "model_id": args.model}
        for note in notes:
            say(f'notice: {note}')
        if el.looks_portuguese(extra_words if mood else prompt):
            say(f"notice: the request appears to be Portuguese. ElevenLabs generally responds better to English, for example '{EXAMPLE}').")
        if not args.instrumental:
            say('notice: sung vocals can compete with speech. Use only when requested')
        base_title = " ".join((args.title or "").split())[:70] or\
            f"{(mood['label'] if mood else 'Music')} AI {datetime.now().strftime('%Y-%m-%d')}"

        each = el.credits_estimate("music", seconds)
        if args.dry_run:
            say(f"music: {seconds:g} s ({origin}), {('instrumental' if args.instrumental else 'may include vocals')}, model {args.model}")
            if mood:
                how = "preset prompt plus your words" if extra_words else "preset prompt"
                say(f"mood: {mood['id']} ({mood['label']}), {how}")
            if edit:
                say("files: " + ", ".join(f'assets/{p.name}' for p in paths))
            if args.library_value:
                say(f"library: {lib} (each variant enters mood {mood['id']} named '{base_title}'; the library is personal and stored outside the skill)")
            say(f'estimated cost: about {each} credits per variant, {each * len(paths)} total (ElevenLabs charges about {el.MUSIC_CREDITS_PER_MINUTE} credits per minute; the provider dashboard gives the actual charge')
            say(el.PAID_PLAN_LINE)
            if not key:
                say(f'key missing. Configure it before generation: {el.KEY_NAME}; see the message produced without --dry-run')
            say('request (not sent):')
            say(el.dry_run_request(el.MUSIC_URL, payload, key))
            return 0

        entries = []
        for i, path in enumerate(paths, 1):
            say(f"Generating track {i}/{len(paths)} ({seconds:g} s) in the user's ElevenLabs account...", flush=True)
            data = el.post_audio(el.MUSIC_URL, payload, key or "", "Music", TIMEOUT)
            title = base_title if len(paths) == 1 else f'{base_title} v{i}'
            if edit:
                rel = f'assets/{path.name}'
                el.store(edit, path, data, el.new_credit(rel, SOURCE, prompt, seconds,
                                                         instrumental=bool(args.instrumental), model=args.model))
                saved.append(rel)
                say(f"ok: {rel}  {seconds:g} s  {('instrumental' if args.instrumental else 'with vocals')}  {len(data) // 1024} KB", flush=True)
            else:
                try:
                    el.save_audio(path, data)
                except OSError as e:
                    raise el.ElevenError(f'ElevenLabs generated audio, but saving failed at {path} ({e.__class__.__name__}). Check free space and library permissions before generating again; generation was already charged') from None
                saved.append(str(path))
            if args.library_value:
                entry = to_library(lib, path, mood, title, prompt, seconds, args, from_inbox=edit is None)
                entries.append(entry)
                if not edit:
                    saved[-1] = f"{entry['id']} in the library"
                say(f'''In library: {entry['id']} "{entry['title']}" (mood {entry['mood']}) · {entry['energy']} · {round(entry['voice_band_share'] * 100)}% of energy in the speech band''',
                    flush=True)
        if edit:
            say(f'credits recorded in assets/credits.json ({el.COMMERCIAL_NOTE})')
        if entries:
            say(f'library: {lib} (source, prompt, and usage terms recorded in the index)')
        if edit:
            say(f'In edit_data.json: "music": {{"enabled": true, "file": "{saved[0]}", {MUSIC_LINE}')
        else:
            say(f'''In edit_data.json: "music": {{"enabled": true, "mood": "{mood['id']}", {MUSIC_LINE}''')
        return 0
    except Exception as e:  # noqa: BLE001  (never a traceback: its text could carry the key)
        if not isinstance(e, el.ElevenError):
            e = el.unexpected(e)
        if saved:
            e = el.ElevenError(f"{e} Already saved: {', '.join(saved)}.", e.code)
        el.print_error(e, key)
        return e.code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    el.utf8_console()
    sys.exit(main())
