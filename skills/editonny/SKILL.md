---
name: editonny
description: Use when installing, preparing, diagnosing, or updating editonny, or editing local videos, Reels, ads, sales videos, lessons, combining takes, adding captions or visual scenes, adapting a reference, or refining an existing edit.
---

# editonny

One portable entry point for local video editing and visual storytelling. Follow the user's conversation language. Preserve recorded speech and its language unless translation was requested; prompt language does not identify speech language.

## Installation and preflight

For installation or setup requests, follow [the complete setup procedure](references/setup.md) before declaring readiness: prepare system tools, the locked engine, the pinned renderer/browser, the default local transcription model, local OCR for caption verification, and bundled resources. Registration alone is not a complete installation. Setup uses disposable synthetic fixtures, not user footage; it does not require an editing-settings summary.

On **every invocation**, including resumed edits, resolve the physical package, run `doctor --json`, check the required local capabilities/caches, and consult the official GitHub repository for updates as described in setup. Never update automatically. During an editing request, offer an available update inside the settings card below instead of a separate turn; outside an edit, ask as described in setup. If the user declines or the network is unavailable, continue with the working local version and disclose the update-check status. Before installing or repairing anything during an editing request, explain the missing requirement and obtain authorization covering that change. Keep the editing confirmation below intact.

## Every new editing job starts with confirmation

**Inspect → show the settings card → wait for a subsequent explicit user reply → create the workspace and edit.** This applies even to a detailed prompt or “decide everything and continue.” The initial prompt cannot approve its own card. Never fabricate confirmation, timestamps, message IDs or a reply.

Before that reply, allow only read-only local probes, hashes, representative frames and bounded speech-language sampling (up to 30 seconds per source, three sources, cached local models only). Keep observations in temporary session files. No full transcription, editorial render, treatment, upload or permanent workspace yet.

Do not front-load editing questions. Once the footage is identified, inspect it and put every open editing choice in the card. Ask before inspecting only when no footage was named (then only for the files or folder) or when a missing requirement needs the repair authorization described above.

### What the card decides

Build the card from the actual request, `proposal.summary` and the observed facts. It covers ten areas, even when the prompt specifies everything:

1. **Goal**: scope and pacing.
2. **Videos**: selected sources and order; unresolved media issues.
3. **Image**: preserve original brightness, tone, contrast, saturation and color interpretation; **no automatic filter, grade, HDR/SDR conversion or “enhancement.”** HDR is not automatically better. Disclose unknown or conflicting metadata.
4. **Format**: dimensions, aspect ratio, crop/contain, orientation, exact FPS/VFR, codec, container, precision and output variants.
5. **Language**: conversation, detected speech, transcription, caption and translation languages, kept distinct. Show uncertain detection; do not invent a regional locale.
6. **Captions**: style, density, placement/accessibility and SRT/VTT files.
7. **Cleanup**: intentional pauses and protected source intervals.
8. **Sound**: voice level, noise removal, music, effects, tracks and encoded peak ceiling.
9. **Visuals**: scenes, camera movement, transitions, graphics and background treatment.
10. **Review and delivery**: sample reviewer, final checks, destination, filenames and organization.

Defaults are **proposals**, not permission: source appearance/geometry/FPS, standard editorial cleanup, faithful captions on (up to three words), voice normalization at -14 LUFS, processing headroom -1.5 dBTP, encoded ceiling -1 dBTP, denoise/music/effects/scenes/camera motion off, agent sample review. Respect narrower scopes: captions-only does not normalize audio or recut speech. Explain unsupported combinations before acceptance; offer concrete choices without silently changing them.

**Additional audio is opt-in only.** Do not add music, sound effects, background ambience, risers, whooshes, or sounds attached to scenes or transitions unless the user explicitly requests that audio. A request to edit, improve pacing, make a dynamic video, add captions, use scenes, or follow a visual reference does not authorize additional audio. Keep `workflow.audio.music` and `workflow.audio.effects` false unless the corresponding audio was requested; do not enable them merely to complete a style or sample. Preserve audio already present in the recording within the requested editing scope.

### How the card looks

The card is the person's whole decision, so it must read in a few seconds. Send it as Markdown, never inside a code block, following this layout in the user's language:

```markdown
### 🎬 Lesson 03 · settings to approve

I'll join your 2 videos into one vertical lesson with a natural pace.

**⚠️ Your call before I start**

- `take-2.mov` has no color information. **A)** read it like `take-1.mov` (recommended) · **B)** leave it out

**📋 Settings**

- 🎯 **Goal**: full edit, natural pace, about 3 minutes _(you asked)_
- 🎞️ **Videos**: 1. `take-1.mov` (3:12) · 2. `take-2.mov` (1:40) _(from your folder)_
- ✂️ **Cleanup**: remove long pauses, breaths, mistakes and repeats; keep complete words and intentional pauses _(suggested)_
- 💬 **Captions**: on, word for word, up to 3 words at a time, clear of the face · no SRT/VTT file _(suggested)_
- 🗣️ **Language**: Portuguese speech _(detected in a short sample)_ · captions as spoken, no translation _(suggested)_
- 🔊 **Sound**: voice evened out to the social-media standard, original audio track · noise removal, music and effects off _(suggested)_
- ✨ **Visuals**: scenes, camera moves, transitions, graphics and background changes off _(suggested)_
- 🎨 **Image**: original look, no filter or “enhancement” _(suggested)_
- 📐 **Format**: vertical 9:16, same size and frame rate as the original, no crop _(from your video)_ · one MP4, no extra versions _(suggested)_
- 👀 **Review**: I check an 8–15 s sample myself, then run the full quality check _(suggested)_
- 📁 **Delivery**: `~/Downloads/editonny/2026-10-19-lesson-03/exports/lesson-03-v001-final.mp4`; editing files stay in `project/` beside it _(suggested)_

⚙️ 1080×1920 · 29.97 fps constant (30000/1001) · MP4, H.264 · 8-bit 4:2:0 as source · −14 LUFS · peak ≤ −1 dBTP (−1.5 while processing) · chat pt-BR · transcription auto

🔄 **Update available**: 0.4.0 → 0.5.0, <one-line change>. Reply **update** to install it first.

👉 Reply **A** to start, or tell me what to change, e.g. “add music”, “no captions”, “keep the pause at 0:42”.
```

- Write everything in the user's language, including labels and tags; keep emojis, file names and numbers as they are. Keep the row order and emojis fixed so revisions are easy to compare. Rows are list items so each stays on its own line in every client.
- One row per area, one or two lines each, stating every value even when it is “off”, “none” or “as the original”. Use plain words; technical values (pixel sizes, rational FPS, CFR/VFR, codecs, bit depth, chroma, LUFS, dBTP, color tags) appear only on the ⚙️ line, which spells out values the rows already describe and adds no new decision. Never show JSON, hashes, IDs, config keys, engine terms or source paths; show file names, and the folder once when it helps.
- Tag where each value comes from: _(you asked)_, _(from your video)_ or _(suggested)_, translated. Tag a fragment separately when its origin differs from the rest of its row.
- Unknown, conflicting and unsupported items go in ⚠️ at the top, each with concrete lettered options and a recommendation; never settle them silently. The rows describe the recommended options. Omit the block, and its 📋 heading, when nothing is open.
- Omit 🔄 when no update is available; when the check failed, say so in one short line instead.
- With ⚠️ items, the 👉 line asks for their answers (“Reply **A** to start”); without them, it asks for **ok**. Give 2–3 change examples that fit this video.
- A clear yes (“ok”, “go ahead”, 👍) approves only when it answers every ⚠️ item; otherwise ask for the missing answers. Choosing a non-recommended option, or any other change, is a change. **update** is a separate answer: approving the settings never accepts an update, and accepting an update never approves the settings. Record the card exactly as displayed as the confirmation `summary`.
- After changes, show the whole card again with ✏️ before each changed row, and wait for its acceptance. A revision delta (`workspace revise`) shows only the ✏️ rows, plus the ⚙️ line with each changed technical value, then “Everything else stays as approved.” and the 👉 line. After an accepted update, rerun preflight and inspection and show the card again.

## Keep the person oriented

During long steps, post one short line per phase in their language, with an estimate when known, for example 🎙️ transcribing, ✂️ cutting, 👀 checking the sample, 🎞️ rendering, 📦 exporting. No commands, logs or internal paths in these lines. Finish with the delivery card under **Export and verify**.

## Resolve the package

`<EDITONNY>` is the physical directory containing this file, resolving any symlink. `<ENGINE>` is `<EDITONNY>/engine`; `<VISUALS>` is `<EDITONNY>/visuals`. The root CLI is `<EDITONNY>/scripts/editonny.py`. Do not assume another skill or global engine is installed.

Use absolute media paths when a command changes directories. Prefer structured subprocess arguments; never interpolate speech or filenames into shell commands. Your current agent directs the edit using its native session; the package does not require an LLM API key.

```text
python3 <EDITONNY>/scripts/editonny.py doctor
python3 <EDITONNY>/scripts/editonny.py catalog "three steps"
python3 <EDITONNY>/scripts/editonny.py media status
python3 <EDITONNY>/scripts/editonny.py inspect /selected/video.mp4 --json
python3 <EDITONNY>/scripts/editonny.py scene --project /path/project --use three-steps --start 5 --end 9
```

Read [setup](references/setup.md) for installation, preflight, updates, or missing requirements. Loading instructions does not prove the engine is installed or can render. Install the single package rather than creating separate processing or scene installations.

## Standard editing cleanup

Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped. For targeted revisions, apply cleanup only within the requested editing scope; preserve unrelated content and approved timing.

This is the default for every full edit even when the user did not enumerate cleanup tasks. Explicit instructions to preserve a breath, pause, take, or approved interval override the default. Do not apply one silence threshold blindly or infer mistakes from a written script without listening to the recording.

## Start or resume

1. Complete the preflight above; the diagnostic fingerprint distinguishes this checkout from an older personal installation. An update requires the user's acceptance and a fresh preflight before continuing.
2. Read [the engine workflow](engine/WORKFLOW.md), inspect only selected footage, show the settings card above, and wait for explicit confirmation.
3. Before creating anything permanent, verify the execution host and include its actual destination in the card. A remote host's Downloads is not the person's Mac. Explain access and obtain a concrete destination/transfer choice; no upload or cache-download fallback by assumption. A denied/full Downloads folder is an actionable error, not permission to choose another folder.
4. After acceptance, use `workspace init` with the real card/reply record and execution context. Default local job: `~/Downloads/editonny/YYYY-MM-DD-title/` with only two folders: `exports/` holds every revision's finals and requested subtitles, and `project/v001/{edit,project}` holds working files and delivery manifests. Never scatter intermediates in Downloads, deliver a cache path or overwrite a previous final.
5. Resume with `workspace status --project <PROJECT> --json`. Read canonical `project/project.json`, observed `edit/source_manifest.json` and evidence `edit/state.json`. New decisions use `workspace revise` and a delta card and confirmation; accepted decisions remain valid for in-scope work. Revisions own independent files. Do not manually forge state or mutate a parent revision.

## Edit and compose

Read the engine workflow and only the references needed for the current step. Local tools provide transcription, take comparison, waveform cutting, captions, face tracking, reframing, multiple cameras, noise reduction, music, preview, and review.

- Time every cut and visual entrance against measured speech, not estimates from a script.
- Verify names, numbers, negations, and corrections before building scenes. A sample template is not evidence of a result, price, testimonial, or offer.
- Prepare a representative **8–15 second** sample containing the actual approved treatments (or the complete shorter clip). Do not add captions, movement, sound or a visual change merely to make a sample. Review it yourself when agent review was accepted; wait when user sample approval was selected.
- Keep faces and important content unobstructed. Captions need contrast, readable size, safe placement, and enough reading time.
- Keep previous exports and record the format, duration, decisions, and output paths.

## Use visual scenes

Search with `catalog` and choose scenes by `whenToUse`, category, and duration instead of loading the entire catalog into context. Prefer visuals that clarify a list, relationship, value, step, or demonstration.

- Use local code animation when it serves the request. Realistic bundled videos are already in `<VISUALS>`; `media status` verifies their integrity. Use `media import FOLDER` only to recover missing files from a complete valid bundle or backup.
- Apply a scene through `scene --project ... --use ...`. Customize the project copy, never the package template.
- Application materializes only the clips a scene references and preserves existing project videos. When changing an object source, copy the corresponding original into the project too. Keep personal media in the edit or an external personal library.
- Replace illustrative names, brands, ratings, prices, offers, and numbers with supplied facts. Omit unsupported elements instead of inventing them.
- Adjust camera, captions, and sounds after scene placement. Inspect beginning, middle, and end in motion, including backward seeking when applicable.
- Optional SVG/3D and external media workflows are described in [integrations](references/integrations.md). They are not mandatory dependencies. Media transmission and costs need authorization matching the request.

## Export and verify

Prefer the cut-only master when it already satisfies the request; another composition encode is unnecessary. Choose a supported route using the workflow's media policy. Native visual composition is CFR; HDR overlays and unsupported SDR profiles are blocked until conformance evidence exists. VFR cut-only keeps timestamps with hard joins. Offer a confirmed conversion or supported alternative when needed; never downgrade silently.

HDR footage does not require SDR conversion for cutting. If requested captions or scenes need an unsupported HDR overlay route, explain that limitation and offer the HDR cut-only master or a separately accepted SDR version. A general editing or caption request does not authorize conversion. SDR contact sheets and grade-comparison images are display derivatives, not evidence that the output master was converted or that HDR appearance was verified.

For native composition, use `build.py hf lint`, `build.py hf check`, and the pinned renderer with an explicit new candidate output path. Review the representative sample, record its exact evidence with `workspace review`, and record user-approved before/after references with `workspace reference` for requested image treatment. Agent autonomy applies to sample review only after initial settings acceptance; user sample review remains mandatory when selected.

Use `quality_gate.py` and `workspace export` for actual-media verification and publication. Required checks marked failed, blocked or not_checked prevent delivery. Source/candidate bytes, composition dependencies, appearance outside measured overlay bounds, timing, audio and caption layout must match current evidence. Interpret expressive pauses, original darkness and silent footage in context, documenting any intentional exceptions. Do not call unchecked media validated.

Inspect the exported MP4 for playback, voice and music, duration, captions, framing, text, cut edges, and synchronization. A still frame checks layout but cannot prove motion or timing. Record exceptions and unavailable checks honestly. Finish with a delivery card in the same style, as Markdown in the user's language:

```markdown
### ✅ Your video is ready

- 🎬 **Video**: `~/Downloads/editonny/2026-10-19-lesson-03/exports/lesson-03-v001-final.mp4`
- 📁 **Editing files**: `~/Downloads/editonny/2026-10-19-lesson-03/project/` (keep it to make changes later)
- ✂️ **What changed**: 4:52 → 3:31 · 18 pauses and 2 repeated takes removed · captions added
- 🔍 **Checked**: playback, voice level, captions, framing and cut edges
- ⚠️ **Limits**: <workarounds, unavailable checks or source limits; omit the row when there are none>

👉 Want changes? Just tell me, e.g. “bigger captions”, “cut the intro”.
```

## When an engine step fails

An engine error is an internal problem, not a task for the user. Never show a traceback, describe the package's code, or ask the user to fix, debug, patch, or report editonny. Keep the conversation about their video; at most say in one short line, in their language, that you are taking another route.

- Continue through another supported route, or perform that step with FFmpeg inside the revision, for example a stream-copy remux that keeps the source timescale. Note the workaround once, in plain words, in the final change report.
- Never edit the installed package or a project's `build.py`, skip or relax a required check, or call unchecked media validated.
- When no supported route exists, stop only that step. Explain the limitation in plain language and offer concrete choices, such as the cut-only master or the version without that effect. Never drop a confirmed setting silently.

First-party code is MIT licensed. Bundled third-party code, fonts, models, and media keep their own terms and notices.

## Author and contact

Created and maintained by **Matheus Gomes**. [Instagram: @matheusgomes](https://instagram.com/matheusgomes) · [Email: eu@matheusgom.es](mailto:eu@matheusgom.es) · [GitHub](https://github.com/soumatheusgomes/editonny).
