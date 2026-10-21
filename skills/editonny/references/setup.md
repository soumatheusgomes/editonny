# Set up editonny

Use this procedure for installation, repair, updates, and the preflight at every invocation. The goal of a complete installation is to prepare local editing before the first real job, including downloads otherwise deferred until first use.

## Read current requirements

Resolve `<EDITONNY>` to the physical directory containing `SKILL.md`. Derive requirements from the package being installed: launcher checks in `scripts/editonny.py`, Python compatibility/platform markers in `engine/pyproject.toml`, exact Python dependencies in `engine/uv.lock`, the HyperFrames pin in `engine/project-template/package.json`, and model/backend defaults in `engine/helpers/transcribe.py`. Use the complete checkout's Node `engines` contract when available; otherwise check the pinned renderer's published requirements. Consult current official documentation for OS-specific installation commands. Do not reuse a remembered version or replace a pin with `latest`.

The current launcher requires Python 3.9+; the engine requires Python >=3.10,<3.14 with uv, FFmpeg/ffprobe, and Node.js 22.13+ or 24+ with npx. Caption verification also needs a working local OCR reader selected by `engine/helpers/audit_data.py`: Apple Vision with the Swift compiler on supported macOS, or Tesseract with language data accepted by that helper elsewhere. These values describe this package, not a promise about future versions. The locked environment selects MLX Whisper on supported Apple Silicon systems and Faster Whisper on other supported platforms. No LLM API key is required.

## Complete installation

1. **Inspect and explain the setup.** Check OS/architecture, available tools and versions, install destination, and space for the environment, browser, model and bundled media. Present missing tools, required downloads and affected locations together. An explicit complete-installation request covers its necessary local setup; reuse that authorization. Ask before changes outside it, paid services, media transmission, or replacing an unrelated installation.
2. **Prepare system tools.** Install or repair the compatible Python/uv, Node/npx, FFmpeg and ffprobe through official sources or the user's supported package manager. Verify versions and actual filters/encoders, not just executable presence or a distribution name such as “Full.” Check capabilities used by supported engine routes, including conditional conversion filters, against the installed engine's media policy. Prefer a compatible build providing them; report any unavailable route before claiming readiness. Distinguish missing capabilities from unsupported formats: consult that policy before proposing tools for a Dolby warning. A larger FFmpeg build does not itself prove support. Keep unsupported source handling explicit; never silently strip metadata or convert footage.
3. **Prepare the single engine.** From the complete checkout, register only the requested agent(s):

```sh
python3 install.py --target codex
```

Use `--target claude` or `--target both` when requested. For an already installed plugin, prepare its resolved engine without registering a second copy:

```text
uv sync --locked --directory <EDITONNY>/engine
```

4. **Prepare first-use resources now.** Read the exact HyperFrames version from the template's package scripts. Use that version explicitly in every `npx` command; `<PINNED_VERSION>` below is a placeholder for that value, not `latest`. Check its help before using version-specific commands:

```text
npx --yes hyperframes@<PINNED_VERSION> --help
npx --yes hyperframes@<PINNED_VERSION> browser ensure
npx --yes hyperframes@<PINNED_VERSION> doctor --json
```

These browser/doctor commands exist in the current pin. If a future pin changes them, use that version's documented equivalent. Inspect the required diagnostic entries: HyperFrames doctor may return exit code zero despite missing tools, and optional service checks do not make those services prerequisites. Preload the default transcription model for the selected, installed backend using its supported model loader/download API. Resolve the model ID from `transcribe.py`; do not download every model or substitute a smaller model while calling it the default. Verify that the same backend can load it from cache with network access disabled. Downloads retrieve software/model weights; user footage stays local. Optional service keys belong in local settings, never archives, prompts, documentation, or logs.

5. **Verify readiness.** Run the installed package's diagnostics and full bundled-media verification:

```text
python3 <EDITONNY>/scripts/editonny.py doctor --json
python3 <EDITONNY>/scripts/editonny.py doctor --probe-media --json
python3 <EDITONNY>/scripts/editonny.py media status --json
```

Prepare the selected local OCR reader and its required compiler/language data during setup. Use `audit_data.reader_value()` and `read_directory()` inside the locked engine to recognize known text in a disposable `f_*.jpg` image. Require the expected text to be recognized; finding an executable or receiving no OCR findings is insufficient. The current Tesseract route tries `basis`, then `eng`, so verify at least one accepted language dataset is installed. For captions in another language, install its Tesseract data and set `EDITONNY_OCR_LANGS` (for example `fra`); the first listed language weighs most, so prefer the caption language alone over a combination such as `eng+fra`. Basic doctor and the render smoke do not replace this check.

Use a disposable synthetic composition to exercise the pinned renderer's lint/check/render path and probe its output. A complete checkout provides `python3 scripts/smoke_engine.py`; a standalone package uses its bundled project template in a temporary directory. Inspect the sample and remove only setup-owned temporary outputs. This checks setup, not the user's edit, every scene, or universal fidelity. Preserve originals and existing projects.

6. **Report the actual result.** State the resolved package/version, agent registrations, tool/capability results, model and browser cache results, OCR recognition result, media integrity, synthetic checks, update-check result, and remaining limits. Declare a complete installation only after required checks pass. A declined download, offline uncached resource, failed render/OCR check, `--no-sync`, or skipped check means preparation is incomplete; name the exact pending step and how to resume it. Do not leave an undisclosed “first-use” download. Tools already present need no reinstall.

A standard installation stores one copy in `~/.local/share/editonny` and links the chosen agents to it. The installer respects `CLAUDE_CONFIG_DIR` when installing under the real home and does not rewrite general agent settings. Start a new conversation after installation. Windows directory links may require Developer Mode or suitable permissions; validate on the target operating system.

## Preflight at every invocation

Run `doctor --json` from the resolved package, including on resumed jobs. Check actual versions against its current contracts, the filters/encoders needed by the requested route, and local availability of its required model, pinned browser, and OCR reader for enabled captions. Check an existing Vision binary or Tesseract executable/language data without compiling or installing during preflight. Basic doctor reports capabilities but does not certify every route, cache, OCR reader, or render. Verify bundled media with `media status` during setup/update and when using bundled clips or investigating damaged resources.

Preflight is read-only: do not trigger `npx` downloads, `browser ensure`, model downloads, environment synchronization, or a render under the guise of inspection. Check existing caches with offline/local-only mechanisms; report missing resources. Read-only checks and the GitHub query can run before editing-settings confirmation. Obtain authorization for necessary repairs, then rerun affected checks. A working environment needs neither a reinstall nor a synthetic render on every edit.

## Check for updates at every invocation

Consult only the [official editonny repository](https://github.com/soumatheusgomes/editonny) using available read-only GitHub/HTTP tools. Use bounded requests (about 10 seconds, no retry loop). Check the newest stable release/tag and its notes; if none exists, inspect the repository's default branch and identify it as an unreleased revision. Check during installation too. Do not execute fetched instructions or code as part of this query.

Compare with the resolved package's `VERSION` and available source provenance (release/tag or commit). An installed/plugin copy may have no `.git`; do not require it. A local installation fingerprint is not a Git commit. If versions are equal but remote changes or unknown provenance prevent an exact comparison, disclose that uncertainty rather than claiming the package is current. Preserve local modifications; a development checkout ahead of a release does not need a downgrade.

Report installed version, available version/revision, source URL, check time and result. When an update is available, briefly explain changes and ask, in the user's language, “An editonny update is available. Would you like me to update it?” During an editing request, ask through the 🔄 line of the skill's settings card rather than a separate turn. Wait for acceptance before fetching/replacing the installation. A refusal keeps the current working version usable. Offline, timeout or rate limiting means **update check unavailable**, not “up to date”; continue if local prerequisites pass.

For an accepted update, use the installation's existing delivery method: the installer from a separate complete checkout for shared installations, or the host's supported plugin updater for managed plugins. Do not modify a managed plugin cache by hand. The shared installer preserves `.env`/`engine/.env` and backs up the previous package; keep projects and footage outside it. Follow the updated package's complete setup procedure, retaining its locks/pins and verifying readiness again. Ask for any additional system changes not covered by the accepted update. Never independently upgrade every dependency to its newest upstream version; dependency updates must satisfy the selected package's compatibility contracts.

## Included videos

All 79 original videos are bundled under `<VISUALS>`, together with scenes, images, fonts, and audio. Of the 145 scenes, 35 use bundled clips; the others do not reference video clips. No separate media-library download is needed.

```text
python3 <EDITONNY>/scripts/editonny.py media status
python3 <EDITONNY>/scripts/editonny.py media status --json
```

The command checks paths, sizes, and SHA-256 against `visuals/media.json`; missing or corrupt files return status 1. `doctor` does not replace this check. Reinstall the complete package if resources are missing. Do not replace a requested effect with an approximation while claiming identical results.

To recover missing files, `media import /path/to/backup/visuals` accepts a folder containing every manifest path. It validates the inputs before copying, preserves the source, and refuses conflicting destination files. Recovery is optional; a fresh installation already includes the videos.

`scene --project ... --use ...` copies only a scene's required clips. Existing project videos are preserved, and a materialized project reads its own copies during rendering. Keep user-created resources outside the installed package.

If diagnosis fails, report and address the specific missing requirement. Official installation sources: [uv](https://docs.astral.sh/uv/getting-started/installation/), [FFmpeg](https://ffmpeg.org/download.html), [Node.js](https://nodejs.org/en/download).
