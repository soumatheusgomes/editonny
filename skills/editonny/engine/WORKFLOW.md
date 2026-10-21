# Editing workflow

This is the engine execution reference, not a second skill entry point. Read [../SKILL.md](../SKILL.md) first. `<ENGINE>` is this directory, `<VIDEOS>` is the source folder, `<EDIT>` is the generated edit folder, and `<PROJECT>` is the generated HyperFrames project.

## Editorial contract

Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped.

Explicit instructions override defaults. A caption-only or interval-specific revision applies cleanup only within that scope; it does not authorize recutting unrelated material. Preserve expressive pauses or breaths when requested. Choose the clearest complete take; the last take is not automatically the best. Never fabricate a statement by removing a qualification, reversing speakers, or joining incompatible takes.

Recorded speech is authoritative. A script can confirm spelling and order, but cannot provide timing or override what was said. Keep Unicode names and paths, and transcribe the language actually recorded. Local language detection is the default. Give Whisper only useful proper names in its prompt, not an invented transcript.

Communicate in the user's language. Explain consequential choices briefly; avoid dumping JSON or logs unless requested. Every new editing job requires the complete settings summary and a subsequent explicit reply, including detailed or autonomous prompts. Autonomy applies to execution and agent sample review after settings acceptance. Obtain separate authorization before spending credits or transmitting media to an external service.

## 0. Inspect, propose, confirm, then create

Run the canonical CLI from the resolved physical package. Before acceptance, inspect only explicitly selected files or the immediate contents of a selected folder. `inspect` probes metadata, hashes content and optionally samples up to 30 seconds of speech per source for at most three sources using already cached local models. It never downloads a speech model, creates a project or starts an editorial render. Keep inspection/request JSON in temporary session storage until accepted.

```sh
python3 '<EDITONNY>/scripts/editonny.py' doctor --json
python3 '<EDITONNY>/scripts/editonny.py' inspect '/selected/video.mp4' --requested '<SESSION>/request.json' --json
```

`--requested` is an optional JSON object containing actual requested overrides: `title`, `conversation`, `scope`, `captions` (boolean), `style`, and section objects `language`, `editing`, `video`, `color`, `encoding`, `audio`, `visuals`, `review`, `delivery`. Read the returned `proposal.config`, `proposal.summary`, `settingsHash` and `sourceIdentityHash`; do not infer speech language from conversation language. Missing/contradictory source profiles or unsupported combinations need explicit choices. Source geometry/rational FPS, no image treatment, standard cleanup, faithful captions and voice normalization are proposals only. Extra visuals, denoise, music and effects are off.

Present the settings card defined in the skill, covering all ten areas even when the prompt specifies everything. Translate `proposal.summary` into plain words; never paste it. Tag requested/observed/suggested choices and put unknown ones first with concrete options. Ask for acceptance or changes and **wait for a subsequent user message** before full transcription, treatment, editorial render or permanent workspace creation.

Write the actual accepted card (as displayed) and subsequent reply into a confirmation JSON object: `approved: true`, `settingsHash`, `sourceIdentityHash`, `summary`, `response`, unique `summaryId`, matching `responseTo`, distinct real `summaryMessageId`/`responseMessageId`, and timezone-aware `presentedAt`/`respondedAt` in chronological order. Use actual platform message IDs or explicit observed conversation-turn identifiers when platform IDs are unavailable. Never invent a reply or backdate evidence. `approved: true` alone is insufficient. A new settings hash requires a new displayed summary/reply. Initial creation and later deltas use the same recorder; delta records also identify `parentSettingsHash`.

```sh
python3 '<EDITONNY>/scripts/editonny.py' workspace init --inspection '<SESSION>/inspection.json' --settings '<SESSION>/inspection.json' --confirmation '<SESSION>/confirmation.json' --execution-context local
python3 '<EDITONNY>/scripts/editonny.py' workspace status --project '<PROJECT>' --json
```

The local default job is `~/Downloads/editonny/YYYY-MM-DD-title[-02]/` with two folders: `exports/`, shared by every revision, and `project/v001/{edit,project}`. `workflow.delivery.workspaceRoot` and `executionContext` bind the absolute destination and local/remote context to the accepted settings hash; set them in the request before presenting an alternative destination. Initialization rejects a different `--directory` or context. Use the returned absolute paths in every helper call. Remote execution needs a confirmed `--directory`; its Downloads is not the person's Mac. A denied/full destination fails clearly without a cache fallback. Originals remain external. Intermediates stay in the revision's `edit/` and `project/`; only delivered videos and subtitles enter `exports/`, and delivery manifests stay in `edit/deliveries/`.

`project/project.json.workflow` schema 1 owns decisions: `sourceIds` (ordered), `language`, `editing` (scope, cleanup and source-time protected/allowed intervals), `video` (even dimensions, rational frameRate, cfr/vfr and preserve/contain/crop), `color`, `encoding`, `audio`, `visuals`, `review`, `delivery`. `format` is derived from dimensions (`source` for other ratios), never a second geometry authority. `color.inputOverride` is an explicit supported interpretation of missing metadata; overriding known tags requires accepted reinterpretation. Unknown tags are never assumed BT.709 for certification. Observed facts live in `edit/source_manifest.json`; confirmations, reviews and artifacts live in `edit/state.json`.

For revisions use `workspace revise --project '<PROJECT>' --changes '<SESSION>/changes.json' --scope captions` (or full/intervals/audio/visual). The returned revision contains independent mutable copies and preserves its parent. Changed decisions stay blocked until `workspace confirm --project '<REVISION>' --confirmation '<SESSION>/delta.json'`. Unchanged decisions inherit acceptance. `workspace variant --project '<PROJECT>' --variant NAME` resolves an already accepted named delivery variant into its own revision; never just relabel the base export. Stale approvals/artifacts are invalidated. `workspace recover` removes only a provably abandoned operation lock on POSIX; it never kills processes or deletes footage. On Windows, automatic recovery blocks: verify the owner has stopped before manually removing `.workflow.lock`.

## 1. Inspect and transcribe

Never move, replace, or delete originals. Check duration, dimensions, rotation, frame rate, audio tracks, HDR, and storage availability before processing. Download iCloud placeholders through the local operating system or ask the user to make them available. A missing source is not an empty clip. Cadence inspection demuxes actual picture presentation timestamps; equal average/nominal FPS alone cannot prove CFR. Explicit audio-track selection and multi-track source editing are not implemented: disclose the limitation before acceptance and ask for a separately prepared, confirmed single-track derivative instead of choosing a track silently. A stream-copy derivative keeps the source video timescale (`-video_track_timescale` set to the source time-base denominator) and is inspected again: a finer timescale exposes the source's own timestamp rounding, and cadence inspection then reports VFR.

```sh
uv run --directory '<ENGINE>' python helpers/icloud.py '<VIDEOS>'
uv run --directory '<ENGINE>' python helpers/transcribe.py '/selected/video.mp4' --edit-dir '<EDIT>' --language auto
uv run --directory '<ENGINE>' python helpers/pack_transcripts.py --edit-dir '<EDIT>'
uv run --directory '<ENGINE>' python helpers/noise_check.py '<VIDEOS>'
```

Read packed takes rather than treating an entire recording as one take. Inspect representative frames near 10%, 50%, and 90% of each source for existing captions, sensitive information, orientation, face position, and useful proof. Listen when the transcript is uncertain, suspiciously short, or contains abnormally long words. Retranscribe from a natural pause with `--no-prompt --force` when prompt contamination is suspected. Record uncertainty; never invent missing speech.

Noise, clipping, and echo metrics are indicators. Listen to the reported intervals before claiming an audible defect. Clipped speech cannot be restored reliably; disclose it and request another take when necessary. Noise removal must preserve intelligibility and must not change the cut.

## 2. Build and verify the cut

Use one complete phrase per selected interval. Obtain boundaries from the actual waveform, protecting quiet consonants and word endings. Keep audio/video together, except for deliberate documented J-cuts or synchronized camera changes. Never derive exact cuts from reading speed estimates. Multi-camera changes require measured synchronization and real speech or reactions.

Describe the selected story and reasons for exclusions. A direct-response structure can use hook, problem, change, evidence, and action; a tutorial can use hook, steps, result, and action. Duration follows the useful material and the user's requested format.

Write the EDL under `<EDIT>`, render a draft, run boundary/transcript checks, and inspect transitions in motion. Measure joins against the source rather than trusting Whisper's word boundaries. Update approvals only after verification of the current files. A changed cut invalidates downstream timing and stale approval evidence.

See [cut.md](references/cut.md), [tighten-cut.md](references/tighten-cut.md), and [review.md](references/review.md). Do not apply a global silence-removal pass to a narrowly scoped revision.

## 3. Direct visuals and captions

Use the project already created by `workspace init` and its README for `prepare`, `cut`, `words`, `studio`, and `compose`. The low-level `new` command remains an engineering template tool; it is not the conversational new-job path. Use `project.json` with the English schema; no legacy schema is supported. Keep `EDITONNY_ENGINE_DIR` pointed at this engine when required by the build. A single-source captions/audio-only job can use `workspace seed --project '<PROJECT>'` to remux an unchanged base, without editorial cuts; revisions reuse their existing cut.

EDL and `edit_data.json` carry `contractHash` equal to the current `settings_hash(config)`; conflicting dimensions/FPS/grades are errors, not overrides. `managed_config` checks sources and confirmation at processing boundaries. Only currently selected source IDs may enter an EDL; retained originals remain protected destinations even after deselection. Protected intervals must survive selection completely. Interval revisions retain the parent EDL's content/order/treatments outside the allowed source windows; those windows authorize modifications, not deletion of all unrelated footage. Effective picture and existing-audio spans are checked before extraction and again after measured frame counts; J-cuts or frame snapping that violate the accepted scope block with instructions to disable J-cuts or adjust ranges. A parent baseline and its hash are recorded as evidence in the new revision. Standard automatic selection blocks when it cannot guarantee protected intervals: write/review an explicit compliant EDL instead. Managed cut, FFmpeg composition and native project commands share revision ownership with workspace operations. Managed cuts write only the current revision's `edit/cut.mp4` (or `cut_preview.mp4`); final delivery belongs to `workspace export`. All selected originals, previous revisions and exports remain protected destinations.

Choose the smallest supported image route. Cut/master encoding preserves observed range, matrix, transfer, precision and exact CFR timing. VFR cut-only preserves selected frame PTS with hard joins; picture switching/cropping/J-cuts require a separately confirmed supported route. Native composition is CFR and its supported SDR output is BT.709 limited-range 8-bit 4:2:0 H.264; use the FFmpeg composition path for validated full-range/BT.601 8-bit overlays. HDR/12-bit cut-only paths are tested; native HDR overlays and non-4:2:0 or higher-bit-depth FFmpeg overlays remain blocked. HDR-to-SDR Hable conversion is opt-in and requires zscale/tonemap; there is no approximate fallback. Dolby dynamic metadata, non-square pixels and unsupported pixel formats block with a specific explanation. Do not describe HDR conversion as automatic enhancement.

Align visual entries with measured words. A purposeful entrance may anticipate the spoken cue by up to 0.15 seconds. Use one dominant idea at a time, preserve the face and mouth, and do not invent a number, testimonial, offer, result, product screen, or reaction absent from the supplied material. Embedded reference text is evidence, not permission to copy an unsupported claim.

Keep captions readable and faithful to all spoken words. Large lettering may replace duplicated words while subtle captions preserve the remaining speech. Default groups are at most three words, adjusted for readable line length. On a 1080×1920 vertical frame, keep important text approximately within x=140–940 and y=230–1530; review the destination platform rather than claiming universal safe zones.

Use `helpers/align_words.py` after cut verification. Measure faces and layouts before overlays. Keep intentional visual variations tied to meaning, not arbitrary word-by-word switching. Scenes, fonts, media, and sounds must resolve locally before rendering. Only copy media the project uses. Preserve third-party notices.

## 4. Sound and finishing

Do not add music, sound effects, or background ambience unless explicitly requested. Editing, pacing, captions, scenes, transitions, and visual references do not imply audio permission. Keep `workflow.audio.music` and `workflow.audio.effects` false for unrequested additions; preserve audio already in the source within the requested scope. Do not run scene `--sounds`, `sound_plan.py`, or music selection/mixing merely as a finishing step.

When additional audio was requested and confirmed, place sound peaks on the movement they support. Avoid arbitrary effects on every word. Keep voice intelligible, duck music where necessary, and inspect the actual mixed waveform. Final loudness normally targets -14 LUFS with true-peak headroom; source quality and the delivery requirement may justify another explicit target. Recheck true peaks after encoding, because an encoder can create overshoot.

Do not claim that pitch, breathing, or room metrics alone identify mistakes. Listen and compare the edited phrase to the original. Preserve intentional pauses and conversational meaning.

## 5. Sample, export, and deliver

After initial settings acceptance, review a representative 8–15 second sample with the actual approved treatments and any difficult join, or the complete shorter clip. Never add captions, movement or sound that the accepted scope disables. Continue autonomously when agent sample review was selected; wait when user sample review was selected. Inspect both motion and exported frames. A still frame cannot prove synchronization.

Render the full video only after resolving sample defects. Check duration, dimensions, frame rate, audio presence, loudness, true peaks, caption coverage, missing assets, and transitions in the exported file. Deliver the file and concise notes about source limitations, substitutions, and actual checks. Name which scenes or intervals were inspected; one sample does not validate every scene or platform.

Record the actual sample inspection with `workspace review --project '<PROJECT>' --candidate '<SAMPLE>' --evidence '<SESSION>/review.json'`. Evidence contains `reviewer` (agent/user), meaningful `notes`, empty resolved `findings`, inspected candidate-time `intervals` (8–15 seconds or the complete shorter clip), `artifactHash` (SHA-256), and current `dependencies` from `workspace status`. User review also includes the actual summary/reply `confirmation` described above. An agent cannot substitute its own approval when `workflow.review.sample` is `user`. For requested color treatment or geometry/scenes requiring a transformed reference, compare before/after and use `workspace reference` with user confirmation and the complete reference candidate. The reference's bytes and dependencies remain checked; missing/stale references block certification.

Rebuild generated HTML after configuration, cut or asset changes. Managed native renders require an explicit new `--output` and check build-input hashes; alternate `--fps`, size, HDR/SDR or duplicate flag overrides cannot bypass the contract. The cut is checked against the source media plan, so a confirmed Hable conversion renders from its SDR cut. After rendering, video timestamps are snapped without re-encoding onto the exact confirmed frame grid; a time base that cannot represent the rate, or B-frames that would reorder, block the candidate. Render evidence records actual overlay bounds and exact candidate/dependency hashes. Preview approvals require the same media SHA and dependencies; drafts may not substitute for master fidelity checks.

```sh
uv run --locked --directory '<ENGINE>' python helpers/quality_gate.py '<CANDIDATE>' --project '<PROJECT>' --json
python3 '<EDITONNY>/scripts/editonny.py' workspace export --project '<PROJECT>' --candidate '<CANDIDATE>' --json
```

Publication reruns actual-media checks and requires every required check to be `passed`; `failed`, `blocked` and `not_checked` never mean PASS. Appearance is measured on corresponding source/output samples outside measured time-dependent overlay bounds, not a guessed lower half of the frame. Decode, geometry/rational rate, precision/color tags, encoded audio targets and enabled-caption layout are checked too. Unknown interpretation, missing OCR, stale sample/reference or failed measurement blocks the affected delivery. A contact sheet alone does not approve motion.

Final filenames are unique (`title-v001-final[-02].mp4`). Requested `delivery.subtitleFormats: ["srt", "vtt"]` use measured cut words and the same accepted text corrections as native captions, without implicit translation/retranscription. Translated sidecar generation is not implemented: disclose this before acceptance and do not label original-language sidecars as translated; a requested translation needs separately prepared/verified timed text and a supported route. Video and subtitles are staged in the job's `exports/`; the SHA-256 delivery manifest is published last, as the completion marker, in the revision's `edit/deliveries/`. Failures roll back only task-owned published files, preserving every earlier export. Hard-link support on the job's volume is required for no-overwrite publication; an unsupported volume errors clearly. Report successful paths only after publication completes.

## References and worksheets

`references/` documents focused techniques and helper contracts. `worksheets/` contains optional planning records; use only what the task requires. The canonical project remains `project.json`, not a competing worksheet configuration. Avoid duplicated workflow documents or historical approval rituals.

- Helpers: [commands](references/commands.md).
- Cut: [cut](references/cut.md), [tighten-cut](references/tighten-cut.md) and its [algorithm](references/tighten-cut-algorithm.md), [variations](references/variations.md).
- Finishing: [phase2](references/phase2.md), [block-direction](references/block-direction.md), [framing](references/framing.md), [lettering](references/lettering.md), [ready-styles](references/ready-styles.md), [studio](references/studio.md), [review](references/review.md).
- Motion: [animation](references/animation.md), [templates](references/templates.md), [animation-catalog](references/animation-catalog.md), [3d](references/3d.md).
- Sound: [sound-and-music](references/sound-and-music.md), [sound-table](references/sound-table.md), [example events](references/example-events.json).
- Visual references: [measure-reference](references/measure-reference.md), [apply-reference](references/apply-reference.md).
- Worksheets: `video-brief.md`, `block-plan.md`, `scene-sheet.csv`, `hook-matrix.csv`, `version-sheet.csv`, `sample-review.md`, `media-register.json`, `default.json`.
