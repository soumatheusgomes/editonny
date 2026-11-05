"""Optional custom sound effect made in the buyer's OWN ElevenLabs account (Sound Effects, paid per use).

For a sound the built-in CC0 kit does not have (its 24 names are listed in references/phase2.md). Same rules as
gen_music.py: the buyer's own key (ELEVENLABS_API_KEY in the environment or in `.env` in the skill root),
never printed or saved; without it, exit 2 with the message that tells the agent to ask for it. The file goes
to <edit>/assets/sfx_<name>.mp3 with an entry in <edit>/assets/credits.json, and is used as a file path in
`sfx.events` (`{"at": 5.07, "name": "assets/sfx_<name>.mp3"}`) or in `inserts[].sfx`. The file starts on the
event frame; use `anchor_s` when its strong point is later in the file.

Usage:
    uv run python helpers/gen_sfx.py "<sound in English>" --edit-dir DIR [--seconds 1.5]
        [--out NAME] [--prompt-influence 0.5] [--loop] [--variations N] [--dry-run]
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elevenlabs_api as el  # noqa: E402

MODEL = "eleven_text_to_sound_v2"
MIN_SECONDS = 0.5                        # API: duration_seconds from 0.5 (up to 30); effects here stay short
MAX_SECONDS = 5.0
DEFAULT_SECONDS = 1.5
DEFAULT_INFLUENCE = 0.5                  # API default 0.3; a short effect should follow the text closely
MAX_VARIATIONS = 3
PREFIX = "sfx_"
SOURCE = 'ElevenLabs Sound Effects (user account)'
EXAMPLE = "soft paper page flip, close and clean, no music"
TIMEOUT = 120


def pick_seconds(value: float) -> tuple[float, list[str]]:
    if not math.isfinite(value) or value <= 0:
        raise el.ElevenError('--seconds must be positive, for example 1.5', 2)
    notes = []
    if value < MIN_SECONDS:
        notes.append(f'sound effect minimum duration: {MIN_SECONDS:g} s: ajustei {value:g} seconds to {MIN_SECONDS:g} s')
        value = MIN_SECONDS
    elif value > MAX_SECONDS:
        notes.append(f'sound effect maximum duration: {MAX_SECONDS:g} s: ajustei {value:g} seconds to {MAX_SECONDS:g} seconds; use gen_music.py for longer audio')
        value = MAX_SECONDS
    return round(value, 2), notes


def output_names(edit: Path, text: str, out: str | None, count: int) -> list[Path]:
    stem = el.output_stem(out) if out else el.slugify(text)
    if not stem.lower().startswith(PREFIX):
        stem = PREFIX + stem
    names = [f'{stem}.mp3'] if count == 1 else [f'{stem}_{i}.mp3' for i in range(1, count + 1)]
    return el.free_paths(edit / "assets", names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate an optional paid sound effect through the user's ElevenLabs account when the bundled kit does not cover the request.")
    ap.add_argument('text', help='sound description, preferably in English')
    ap.add_argument("--edit-dir", required=True, help='session edit folder', dest='edit_dir')
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help=f'duration in seconds, from {MIN_SECONDS:g} to {MAX_SECONDS:g} (default {DEFAULT_SECONDS:g})', dest='seconds')
    ap.add_argument("--out", help=f'filename in assets/ (default: {PREFIX}<text>.mp3; the {PREFIX} prefix is added automatically)', dest='out')
    ap.add_argument("--prompt-influence", type=float, default=DEFAULT_INFLUENCE,
                    help=f'0 to 1: adherence to the description (default {DEFAULT_INFLUENCE:g})', dest='prompt_influence')
    ap.add_argument("--loop", action="store_true", help='sound loops seamlessly', dest='loop')
    ap.add_argument("--variations", type=int, default=1,
                    help=f'number of variants, one request each (default 1; maximum {MAX_VARIATIONS})', dest='variations')
    ap.add_argument("--dry-run", action="store_true",
                    help='show the request and estimated cost without generation or network access', dest='dry_run')
    args = ap.parse_args(argv)

    key = None
    saved: list[str] = []

    def say(*parts: str, flush: bool = False) -> None:
        print(el.redact(" ".join(parts), key), flush=flush)

    try:
        text = (args.text or "").strip()
        if not text:
            raise el.ElevenError('sound description is empty', 2)
        el.check_prompt(text, EXAMPLE)
        if not 1 <= args.variations <= MAX_VARIATIONS:
            raise el.ElevenError(f'--variations ranges from 1 to {MAX_VARIATIONS} (each variant incurs a charge)', 2)
        influence = args.prompt_influence
        if not math.isfinite(influence) or not 0 <= influence <= 1:
            raise el.ElevenError("--prompt-influence ranges from 0 to 1 (for example 0.5)", 2)
        edit = el.edit_folder(args.edit_dir)
        seconds, notes = pick_seconds(args.seconds)
        key = el.api_key()
        el.check_in_key(text, key)
        if not key and not args.dry_run:
            raise el.ElevenError(el.missing_key_message(
                "Generate a sound effect", 'the 24 bundled sound effects are included with the skill; inspect the local library before generating another'), 2)
        paths = output_names(edit, text, args.out, args.variations)
        payload = {"text": text, "duration_seconds": seconds, "prompt_influence": round(influence, 3),
                   "loop": bool(args.loop), "model_id": MODEL}
        for note in notes:
            say(f'notice: {note}')
        if el.looks_portuguese(text):
            say(f"notice: the description appears to be Portuguese. ElevenLabs generally responds better to English descriptions, for example '{EXAMPLE}').")

        each = el.credits_estimate("sfx", seconds)
        if args.dry_run:
            say(f"effect: {seconds:g} s{(', looped' if args.loop else '')}, text influence {influence:g}")
            say("files: " + ", ".join(f'assets/{p.name}' for p in paths))
            say(f'estimated cost: about {each} credits per variant, {each * len(paths)} total (ElevenLabs charges about {el.SFX_CREDITS_PER_SECOND} credits per sound-effect second; the provider dashboard gives the actual charge)')
            say(el.PAID_PLAN_LINE)
            if not key:
                say(f'key missing. Configure it before generation: {el.KEY_NAME}; see the message produced without --dry-run')
            say('request (not sent):')
            say(el.dry_run_request(el.SFX_URL, payload, key))
            return 0

        for i, path in enumerate(paths, 1):
            say(f'generating effect {i}/{len(paths)} ({seconds:g} s) in the user\'s ElevenLabs account...', flush=True)
            data = el.post_audio(el.SFX_URL, payload, key or "", "Sound Effects", TIMEOUT)
            rel = f'assets/{path.name}'
            el.store(edit, path, data, el.new_credit(rel, SOURCE, text, seconds, loop=bool(args.loop),
                                                     prompt_influence=round(influence, 3), model=MODEL))
            saved.append(rel)
            say(f'ok: {rel}  {seconds:g} s  {len(data) // 1024} KB', flush=True)
        say(f'credits recorded in assets/credits.json ({el.COMMERCIAL_NOTE})')
        say(f'in sfx.events: {{"at": 5.07, "name": "{saved[0]}"}}; the file begins at "at"; use "anchor_s" if its peak comes later')
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
