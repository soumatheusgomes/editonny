# Sound and music

Add music, sound effects, or background ambience only when the user explicitly requests them and the audio settings are confirmed. Editing, visual scenes, transitions, or a dynamic style do not authorize additional audio. Otherwise keep music and effects off, preserve source audio within scope, and skip these helpers.

For requested additions, use licensed local assets or an explicitly authorized provider. Keep originals and credit metadata. Music supports the voice; effects support visible movement. Measure mixed loudness and encoded true peaks, use fades at joins, and listen for pumping or clipped syllables.

See [WORKFLOW.md](../WORKFLOW.md) for scope, originals, source-language fidelity, and sample review.

## Helpers

Run the relevant helper with `--help` before supplying task-specific paths:

```sh
uv run --directory '<ENGINE>' python helpers/music_library.py --help
uv run --directory '<ENGINE>' python helpers/effects.py --help
uv run --directory '<ENGINE>' python helpers/mix_music.py --help
uv run --directory '<ENGINE>' python helpers/sound_plan.py --help
```
