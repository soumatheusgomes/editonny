# Installation and recovery

Reviewed on 2026-10-19 against the repository installer and CLI. Run commands from the complete checkout unless a different path is shown. `python3` may be named `python` or `py` on your system; the launcher needs Python 3.9+.

`doctor --json` reports the resolved physical package/engine path, code fingerprint, recorded installation fingerprint and actual FFmpeg filters/encoders. `doctor --probe-media` explicitly runs a short SDR encode/probe; basic doctor does not render. Neither is universal fidelity certification. A checkout change does not update an existing personal installation: review the printed origin, then explicitly run the existing installer when you intend to replace that installation. No installation is changed by inspection or workspace commands.

New edits inspect and confirm settings before workspace creation. The local root is `~/Downloads/editonny`; each job holds `exports/` for finished videos and `project/` for its revisions' working files. Remote execution needs an explicit accessible destination and never treats its own Downloads as the person's Mac. Keep personal footage outside the installed package. See the portable skill's `engine/WORKFLOW.md` for confirmation, revisions, sample evidence, fidelity limits and export commands.

## Requirements

| Tool | Purpose |
|---|---|
| Python 3.9+ | Installer and root CLI |
| uv | Locked engine environment and compatible Python selection |
| Python >=3.10,<3.14 inside uv | Engine runtime |
| FFmpeg and ffprobe on PATH | Cuts, audio, media processing, and inspection |
| Node.js 22.13+ or 24+, with npx | HyperFrames rendering and developer checks |
| Local OCR: Apple Vision/Swift compiler or Tesseract with supported language data | Required recognition and layout checks for enabled captions |
| Configured Codex or Claude Code | Conversation and skill execution |

These versions describe the current package. For installation or updates, read its manifests, locks and pinned renderer requirements as described in the portable [complete setup procedure](../skills/editonny/references/setup.md). That procedure is the operational source for agents, including standalone/plugin installations.

The Python installer registers the package and synchronizes its locked engine; it does not install system programs, preload a transcription model, or prepare the rendering browser. Its “Installation ready” message covers that installer step only. Obtain system tools from the official [uv](https://docs.astral.sh/uv/getting-started/installation/), [Python](https://www.python.org/downloads/), [Node.js](https://nodejs.org/en/download), and [FFmpeg](https://ffmpeg.org/download.html) sources.

For a complete agent-led installation, ask the agent to read `skills/editonny/SKILL.md` and follow its setup reference before running the installer. The request should cover compatible local tools, the locked environment, the pinned renderer/browser, the default transcription model, working local OCR, bundled-media verification, and disposable synthetic checks. Verify actual OCR recognition too; basic doctor and the render smoke do not exercise it. Prepare first-use downloads during setup and report any declined or failed step explicitly. Optional paid generators are not prerequisites. Check FFmpeg's actual capabilities rather than assuming a “Full” distribution supports every route; Dolby dynamic metadata remains blocked by the current engine, not repaired by installing another application.

## Register the shared skill

```sh
python3 install.py --target both
```

Use `--target codex` or `--target claude` for one agent. The package lives once at `~/.local/share/editonny`. Codex uses a link at `~/.agents/skills/editonny`; Claude Code uses `~/.claude/skills/editonny`. A real-home installation respects `CLAUDE_CONFIG_DIR`. General agent settings are not rewritten.

Start a new conversation. Invoke `$editonny` in Codex or `/editonny` in Claude Code. These local registrations do not automatically install the skill, its tools, or its media in cloud sessions or on a phone.

```sh
python3 install.py --target both --dry-run
python3 install.py --target both --home "/tmp/editonny-isolated" --no-sync
```

`--dry-run` reports destinations without writing. `--home` isolates registration under a temporary home. `--no-sync` prepares files only; repeat without it to prepare the engine. Do not confuse a files-only installation with a working editor.

## Check the environment

```sh
python3 ~/.local/share/editonny/scripts/editonny.py doctor
python3 ~/.local/share/editonny/scripts/editonny.py doctor --json
python3 ~/.local/share/editonny/scripts/editonny.py media status
```

`doctor` checks system tools, engine imports, and catalog files; it does not transcribe, render, or test external services. `media status` separately validates bundled video hashes. To repair a missing environment:

```sh
uv sync --locked --directory ~/.local/share/editonny/engine
```

For a development checkout, use `skills/editonny/engine` and `python3 editonny.py doctor`. Developer setup belongs in [setup](setup.md); reproducible checks and observed limits in [validation](validation.md).

Every skill invocation, including resumed jobs, runs a read-only local preflight and checks the official GitHub repository for updates. Check capabilities and caches needed by the requested operation; basic doctor alone does not prove model/browser readiness. No render, download, repair, or installation update happens automatically as part of that preflight. A working local installation remains usable if the update check is unavailable or the user declines an update.

## Bundled media

The complete plugin archive includes all 79 original videos inside `visuals/`, together with scenes, images, fonts, and audio. Of 145 catalog scenes, 35 reference bundled clips. No external media-library configuration or separate media download is required. Clips retain their original bytes, dimensions, and transparency unless an explicitly documented local asset rebuild is needed.

`media status --json` checks English paths, sizes, and SHA-256 from `visuals/media.json`. Missing or invalid files return status 1. Reinstall the complete package or recover missing files from a complete valid backup:

```sh
python3 ~/.local/share/editonny/scripts/editonny.py media import "/path/to/backup/skills/editonny/visuals"
```

Recovery validates the sources before copying, preserves originals, refuses destination conflicts, and can resume already valid copies. Normal installation already includes these files.

`scene --project PATH --use SCENE --start SECONDS --end SECONDS` materializes only the referenced clips. Existing project files are preserved, regardless of their modification time. New copies are verified in temporary files and published complete without overwrite, requiring hard-link support on the destination volume. A failure must not leave an accepted partial resource or write the scene configuration prematurely.

## Update and unregister

The agent checks the official repository on every invocation and reports the installed version, available stable release (or clearly identified unreleased revision), source and check status. Installed archives may have no `.git`; version/provenance uncertainty is reported honestly. An unavailable GitHub check does not mean “up to date.” When a newer version exists, the agent explains it and asks whether to update before changing the installation. Dependency versions follow that package's compatibility contracts and locks, rather than independent upgrades to every upstream latest release.

After acceptance, run the installer from the new separate checkout. It compares source and installed content, refuses foreign destinations, preserves `.env` and `engine/.env`, and backs up the previous copy under `~/.local/share/editonny-backups/`. Reinstallation also repairs missing or modified package resources while retaining the previous installation in backup. Managed plugins use their host's supported updater instead of editing plugin caches directly. Repeat the complete setup/readiness checks after updating.

Keep personal projects and footage outside the installed skill. Other personal files inside it remain in the backup rather than merging into the new package. Verify the update before removing any backup. No conversion of previous experimental project formats is provided.

```sh
python3 install.py --target both --uninstall --dry-run
python3 install.py --target both --uninstall
```

Unregistering removes only the selected agent links. Shared files, settings, backups, and projects remain. It does not uninstall system tools. Inspect foreign destination conflicts rather than forcing an overwrite.

## Load as a local plugin

```sh
claude --plugin-dir "/path/to/editonny"
```

In that session the skill is `/editonny:editonny`; the standalone skill is `/editonny`. Avoid duplicate loading. Plugin metadata does not prove marketplace installation or publication. System video tools and the engine environment are still required.

## Open the guide

The guide is the editonny website (published from `web/` on Vercel). To run it from the complete checkout, install the npm workspace once with Node.js 22.13+ or 24+:

```sh
npm ci
python3 editonny.py guide
python3 editonny.py guide --port 4321 --no-open
```

The development server binds only to `localhost` (loopback; Next.js 16.4 loops on its locale rewrites when bound to `127.0.0.1`); the default port is 4173. `npm run dev` starts it on port 3000. The standalone skill installation and the plugin archive omit the guide.

## Platform limits

Validated environments are macOS Apple Silicon and Windows 11 (x64); see [validation](validation.md) for what each run covered. Linux requires its own validation. On Windows, `python3` is usually only a Microsoft Store stub: run the commands with `python` or `py -3`. The installer registers agents with a symlink and, when Windows refuses symlinks without Developer Mode or administrator rights, with a directory junction to the same single copy; it never creates a second engine copy. Model downloads, long recordings, and optional external generation require separate verification on the actual machine.
