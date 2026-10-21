# editonny Visuals

The portable visual library contains 145 scenes, bundled original videos, transparent objects, sound effects, music, voice presets, and recipes for generated footage. It works with the shared editonny engine; there is no separate installation or external media-library dependency.

The authoritative scene descriptions, editable parameter defaults, timing limits, and usage notes are in [catalog.json](catalog.json). Bundled video paths, byte sizes, SHA-256 hashes, and project destinations are in [media.json](media.json). Use the guide for editing-request examples.

## Apply a scene

Create an engine project first, then apply the exact English scene ID:

```sh
python3 editonny.py scene --project /path/to/project --use revenue-counter --start 17.6 --end 21.3
python3 editonny.py scene --project /path/to/project --use transition-glow --cut 25.5
```

From this directory, `python3 apply.py` accepts the same scene options. `--use` copies the selected scene and its required video assets, assigns independent composition and DOM IDs, and adds an entry to `project.json.scenes`. Existing project footage is preserved. Missing, corrupt, uncataloged, or unsafe media paths fail before copying resources. Never replace a user's original footage to repair the bundle.

Scene entries use `file`, `start`, `end`, `name`, `sceneId`, and optional `hidesCaptions`, `aboveFace`, and `overPerson` fields. Times are measured against the actual cut, not an estimated transcript. Fixed-duration scenes can shorten only within their catalog limits; adaptive scenes fit their selected interval. A transition's `--cut` marks its impact point.

To prepare independent copies without adding project entries automatically:

```sh
python3 apply.py --project /path/to/project --transition glow --cuts 3.2,8.5
python3 apply.py --project /path/to/project --pointer down-arrow --count 3
python3 apply.py --project /path/to/project --effect vignette --count 2
python3 apply.py --project /path/to/project --text karaoke --count 3
```

These commands print the entries to insert into `project.json.scenes`. Copies fill unused sequential names without overwriting existing files, including symlinks. Each copy has independent DOM IDs and timeline registration. Customize its `var P` block or, for the original composed scenes, the visible HTML text and `countNumber` calls. Parameter comments and catalog descriptions explain units, limits, automatic sizing, and empty-value behavior.

`python3 apply.py --project /path/to/project` without an operation materializes all scenes and their referenced videos. Prefer `--use` for a small project. A generated project contains only the video clips selected by its scenes; package catalogs and creative libraries stay in this directory.

## Scene families

- **General and specialist scenes:** revenue, numbered steps, comparisons, calls to action, phone and laptop interfaces, property cards, clinical illustrations, nutrition, legal illustrations, and creator metrics.
- **Frame panels:** legal documents and deadlines, engagement signals, heart/artery/chart diagrams, sugar comparisons, and property building/map/floor-plan/price views.
- **Transitions:** glow, flash, film burn, glitch, TV shutdown, curtain, zoom, blinds, iris, and fade. Their strongest visual moment is centered on the requested cut.
- **Pointers:** arrows, crosses, checks, circles, underlines, boxes, highlighting, spotlight, concealment mask, toggle, tap, number, rays, question, and exclamation.
- **Video effects:** vignette, blur, glow, chromatic blur, shake, money rain, confetti, cinema, noir, scan lines, static, VHS, and prism.
- **Captions and titles:** nine timed caption treatments and ten headline/badge treatments. Caption timing comes from the recorded words; title text is separately editable.
- **Proof cards:** receipt, completed checklist, crossed-out quote, gauge, time counter, editing panel, and waveform cuts.
- **Interfaces:** notes, AI conversation, search, camera viewfinder, and command bar.
- **Scene objects:** object fact card, staged house construction, Brazil map, solar system, and video gallery.
- **Comedy:** stone text, cracked screen, stage spotlight, concert strobe, explosion, fire, smoke, and shattered glass.
- **Image entrances:** four directional drops, two zoom styles, side zoom, click-to-open, and a before/after curtain.
- **Demonstration panels:** brushing, muscle recovery, consumer rights, fruit versus juice, room design, year hook, profession cards, and comment call to action.
- **Neon:** quote, challenged advice, glass chart, stuck progress, attempts, and checked list.

All sample claims, prices, counts, legal rules, and clinical examples are illustrative. Replace them with the speaker's verified content. Do not invent sales, views, health figures, or guarantees. A demonstration panel communicates one specific idea: changing its title does not change its animation. For example, the brushing panel cannot demonstrate flossing; use a suitable title, checklist, or pointer instead.

## Layout, captions, and timing

Scenes target 1080×1920 portrait video. Other aspect ratios may crop them and require visual inspection. The original fixed compositions convert unsupported scale tweens to clipping or translation; do not bypass that renderer constraint.

Frame panels use the upper screen, with important content approximately inside x=140–940 and y=230–850. They are designed for a wide speaker frame below. A suitable project setting is:

```json
{"frame": {"box": [36, 912, 1008, 990]}}
```

The dark panels pair naturally with `night` and `amber`; inspect their contrast against `paper`. They wait for `frame.closedAt` before animating and fit the remaining interval. Consecutive panels should share one continuous frame block. A floor-plan panel lasts 7.3 seconds and can shorten to 45% of that duration, approximately 3.3 seconds.

Property panels expose actual floor counts, distances, units, area, rooms, price, development name, and notices. A zero numeric value hides the number while retaining the animation. On the map, `countAt` can select a spoken word or a time in seconds; empty uses the spoken unit. Up to two `others` locations light up when their names are spoken. The building's location pin can synchronize with the spoken place name. If no word matches, the scene retains its default timing.

Image entrances use a 16:9 card above the eyes by default. For portrait screenshots, reduce the card width and place it beside or below the face. To use video, replace the image element with a video preserving its ID and class; set `src`, `data-start`, `data-duration`, `data-track-index`, `muted`, and `playsinline`. Mirrored effects duplicate the video automatically. The before/after curtain needs matching original footage and edited footage at the same spoken moment; never compare unrelated intervals.

Caption scenes respect frame blocks, comparison blocks, green-screen caption settings, explicit hidden intervals, lettering, and overlays. Run `--captions` again after moving a block or overlay. Shorter overlapping caption scenes take priority. With `captions.inFrame`, captions can sit above the lower speaker frame while a panel remains visible above them. A numbered top badge keeps editor captions visible below it.

`--use` marks overlays that require stable footage with `overPerson`. Run the engine's `camera_motion.py` after placing the scenes so camera movement does not move an arrow away from its target. A stationary-camera warning can be expected there: the overlay supplies the motion. Leave uncovered intervals if additional camera movement is wanted. Scenes reaching the final frame hold their exit beyond it so calls to action do not vanish early.

For moving speakers, begin a frame after they stop or inspect tracking carefully. Tracking can enlarge a face substantially. Adjacent frame blocks can isolate a moving interval from a stable one without interrupting the panel above. Inspect the sample contact sheet even when structural checks pass; a valid script alone does not prove a visible, readable composition.

## Library and provenance

| Directory | Contents |
| --- | --- |
| `library/spinning-3d-objects/` | 44 transparent WebM objects; the catalog records one-shot versus looping behavior and special placement notes. |
| `library/3d-objects/` | 115 upstream 3dicons objects, with multiple angles and styles. File patterns use `{angle}` and `{style}`. |
| `library/backgrounds/` | 12 locally generated animated backgrounds. |
| `library/overlays/` | Seven light, dust, and grain overlays for screen blending. |
| `library/effects/` | 102 sound effects with duration and `anchorSeconds` metadata. |
| `library/music/` | 23 tracks with mood, duration, loudness, and `fillAtSeconds`. |
| `assets/visuals/voice/` | Synthesized support files for voice filters. |

Some scene videos are stored in `assets/visuals/video/`; others are materialized from the library. Consult `media.json` rather than assuming a source path. Verify the complete bundle with `python3 editonny.py media status`. Restore missing original files with `media import FOLDER`; every source is verified before publication, and existing originals are not overwritten.

First-party code is MIT, as described by the repository license. Third-party rights remain separate: 3dicons by realvjy uses CC0; Freesound effects use CC0 and retain authors and source links in [CREDITS.md](library/effects/CREDITS.md); HoliznaCC0 and FreePD music uses CC0. Preserve font licenses and upstream asset names. Bundled scene media, backgrounds, overlays, and spinning objects were created for this package; a filename translation does not change provenance.

## Sound and voice

Music, sound effects, and background ambience are off unless the user explicitly requests them. Applying scenes or transitions does not authorize audio. Preserve source audio within the requested scope; skip sound generation and music helpers for silent visual additions.

For explicitly requested and confirmed effects, generate scene sounds with `python3 editonny.py scene --project /path/to/project --sounds`. Managed projects reject this command while `workflow.audio.effects` is false.

Use a few requested sounds chosen for meaning. A title can use `title-swoosh`, a change `swish`, an appearance `mouth-pop` or `pop-ui`, repeated list items the same `bubble-pop`, money `cash-register`, a drawn line `pencil` or `pen`, and a closing moment `ding`. Do not assign an error sound simply because an item is negative. Align each sound's audible impact using `anchorSeconds`.

`--sounds` writes scene-owned events to `events.json` while keeping other events. Loudness is calibrated from the loudest 100 ms RMS window and the peak; equal peaks do not imply equal perceived loudness. Short effects commonly need a higher peak than long whooshes. The automatic calibration keeps transitions quieter and caps peaks to leave headroom for speech. Rebuild the engine sound plan after writing events.

```sh
python3 apply.py --voice list
python3 apply.py --project /path/to/project --voice old-phone --start 3.2 --end 5.0 --strength 0.8
python3 apply.py --project /path/to/project --voice inspect
python3 apply.py --project /path/to/project --voice remove 2
python3 apply.py --project /path/to/project --voice rebuild
python3 apply.py --project /path/to/project --voice clear
```

[voice-presets.json](voice-presets.json) documents all 15 presets, their intended use, filter chains, calibration, delay compensation, and limitations. Voice processing rebuilds from the original cut rather than stacking effects repeatedly. Ramps avoid clicks, duration and audiovisual alignment are preserved, and loudness is matched to the original segment. Low strength mixes original and processed voice; pitch-changing effects may then sound doubled. Run `voice.py --help` for processing a standalone rendered file before adding music.

## Generated-footage recipes

Start with [recipes/RECIPES.md](recipes/RECIPES.md), then read the relevant recipe. The six recipes cover outfit changes, objects turning to stone, greenscreen effects, empty sets, spinning 3D objects, and reconstructed environments. Their local helpers align or composite media and preserve the original voice when generated footage replaces only the image.

External generation, media upload, and paid credits require explicit authorization. A recipe or an installed tool does not grant it. Use the actual recording, retain originals, and inspect the final transition; generation can alter identity, geometry, lighting, or timing.

## Author and contact

Created and maintained by **Matheus Gomes**. [Instagram: @matheusgomes](https://instagram.com/matheusgomes) · [Email: eu@matheusgom.es](mailto:eu@matheusgom.es) · [GitHub](https://github.com/soumatheusgomes/editonny).
