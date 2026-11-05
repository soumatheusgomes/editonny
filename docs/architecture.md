# editonny architecture

Source reviewed on 2026-10-19. Changes to entry points, contracts, distribution, dependencies, or resource paths invalidate this map.

## Entry points and ownership

| Entry | Responsibility |
|---|---|
| `editonny.py` | Resolve and launch the canonical CLI from any working directory |
| `skills/editonny/scripts/editonny.py` | Environment checks, catalog search, project creation, scene/media commands, helpers, and the guide's local development server |
| `skills/editonny/SKILL.md` | One portable conversation workflow with standard cleanup and scoped-edit rules |
| `skills/editonny/engine/` | Local transcription, waveform cuts, captions, audio, framing, preview, and render preparation |
| `skills/editonny/engine/project-template/` | English project contract, composition runtime, build commands, and pinned HyperFrames integration |
| `skills/editonny/visuals/` | Visual catalog, scene templates, recipes, resource materialization, and media verification |
| `install.py` | One shared installation, agent links, ownership checks, staging, backups, and rollback |
| `web/` | Public guide (Next.js app on Vercel): localized request builder, catalog, examples, installation, and design-system showcase route |
| `DESIGN.md`, `design-system/` | Visual contract and token source; React component library, preview templates, showcase, adherence lint, and frozen reference HTML |

The engine Python range and dependency resolution live in `engine/pyproject.toml` and `engine/uv.lock`. The template pins HyperFrames 0.8.179. Root developer dependencies and the web app's dependencies (`web/package.json`) are pinned to exact versions; the workspace uses TypeScript 6.0.3 because typescript-eslint and `eslint-config-next` require the TypeScript JavaScript API, which TypeScript 7 does not ship. Renaming the engine package does not authorize dependency upgrades.

## Contracts and processing

The project configuration is `project.json`; fields, enums, generated filenames, commands, and first-party scene state use English. New experimental contracts have no backward compatibility layer. `window.__scenes` supplies per-scene configuration; HyperFrames timeline interfaces retain upstream names.

The skill directs work against actual speech and waveform measurements. Speech, Unicode paths, and supplied text remain user data. Transcription detects source language. Language-specific token sets support speech analysis; they do not expose alternative product schemas.

Processing uses an `edit/` directory for transcripts, EDLs, cut media, timing, review state, and project notes. Composition projects live alongside it, store their package origin, and use their own pinned render commands. A single project render runs at a time. The shared cleanup policy never expands the scope of a targeted revision.

## Guide and data

The guide is the `web/` workspace (`@editonny/web`): a Next.js 16 App Router app with React 19, the React Compiler, Cache Components, typed routes, and Turbopack, deployed on Vercel with Root Directory `web`. Every page is prerendered per locale; interactive parts are client islands.

Localization uses next-intl 4 with `next/root-params`. Locales are `en-US` (default), `pt-BR`, and `es`, with URL prefixes `/en`, `/pt-br`, and `/es` and localized pathnames (`/create` · `/criar` · `/crear`, `/catalog` · `/catalogo`, `/examples` · `/exemplos` · `/ejemplos`, `/install` · `/instalacao` · `/instalacion`). `src/proxy.ts` redirects the first visit by `Accept-Language` and afterwards by the `NEXT_LOCALE` cookie that the language menu sets; it skips API, internal, design-system, Open Graph image, and file requests. Each page publishes a canonical URL, `hreflang` alternates with `x-default`, a localized Open Graph image, robots directives that allow large image previews, and sitemap entries. The home page adds schema.org `WebSite` and `SoftwareApplication` JSON-LD in its locale. `[locale]/opengraph-image.tsx` renders the 1200×630 card after `design-system/assets/brand/og-image.png` from the hero, workbench, and anatomy text of each locale; synchronous asset reads keep it prerendered. `app/apple-icon.png` is the 180×180 touch icon rasterized from `icon.svg`.

`src/features/request/` owns the brief options (stable keys such as `vertical` or `dynamic`), draft parsing, and the request builder. The English phrases reproduce the previous builder byte for byte, including the canonical cleanup policy; `pt-BR` and `es` phrases translate every clause. Scene, example, and custom requests all use this builder, and text stays in the browser until copied.

`src/features/examples/records.ts` lists the 54 examples in workflow order with their categories and presets; `web/content/examples/<locale>.json` holds their text. Scenes are read at build time from `skills/editonny/visuals/catalog.json` (public fields only, server-only module) and translated by `web/content/scenes/<locale>.json`, falling back to English per field. Tests require complete translations and identical ids across locales.

Preferences are browser-only: `editonny:draft` (the brief) and `editonny:theme` in `localStorage`, read through a guarded external store so blocked or corrupted storage falls back to defaults, and synchronized across tabs. An inline script applies the saved theme before the first paint. Catalog and example views keep search, category, and the open item in the URL so views can be shared; ids that are not in the catalog are ignored.

`python3 editonny.py guide` starts the guide's development server on `localhost` (default port 4173) and opens the browser once it reports ready when the full checkout has its npm dependencies; the standalone skill installation and the plugin archive do not include the guide.

## Design system

`DESIGN.md` holds the normative tokens; `design-system/tokens/` implements them with a dark default and a light paper theme selected by `data-theme`. Components are React 19 function components with TypeScript prop types (`design-system/tsconfig.json`), styled by per-category CSS that may use only tokens; files that need hooks or browser APIs are client components and the rest render on the server. `design-system/scripts/icons.mjs` generates the icon registry (`npm run check:icons` verifies it). The web app imports `@editonny/design-system` and its `styles.css`, loads the engine's licensed fonts through `next/font`, and serves the showcase at `/design-system`. The folder is development material: the plugin archive carries only `DESIGN.md` with the canonical documentation.

## Media and visual composition

`visuals/media.json` records 79 original videos by English path, size, and SHA-256. Binary content is preserved unless an intentional local content rebuild is required. `media.py` verifies the bundle and recovers missing resources from a valid backup without network access or overwriting conflicts.

`resources.py` finds literal references in selected scene HTML and materializes only required clips. Existing project files remain untouched, regardless of their modification time. New resources are copied to a verified temporary file and published atomically without overwrite, requiring hard-link support on the destination volume. Unexpected symlinks, unknown media, and escaped paths are rejected before scene/configuration changes.

Scenes are project copies with independent IDs and timeline registrations. Customization never modifies the package template. Required runtime dependencies are copied into the project so a materialized composition uses its own media rather than reading bundled videos during render.

## Preview and rendering

Managed jobs start with CLI `inspect` and the mandatory settings summary/reply gate. `project_contract.py` validates schema 1 and distinct consent/artifact hashes. `intake.py` owns bounded local observations and proposals; `workspace.py` owns exclusive creation, independent revisions, accepted variants, evidence and no-overwrite publication. `project/project.json.workflow` owns decisions; `edit/source_manifest.json` records selected content hashes/facts and `edit/state.json` stores confirmations and execution evidence. The physical package and code fingerprint are reported by `doctor` to distinguish a checkout from an older personal installation.

`media_policy.py` derives image/precision/encoding requirements from observed inputs and accepted decisions. `render_cut.py` retains rational CFR timing; `render_vfr.py` uses selected source PTS and hard joins. The default image route does not grade or convert HDR/SDR. `fidelity.py` compares actual corresponding frames outside time-dependent measured overlay bounds and checks current sources, decode, geometry, rate, precision, color tags, audio and text. Unsupported/unknown cases remain blocked. Sample/treatment records bind media SHA, complete configuration, cut/timeline, engine and renderer identity; changed dependencies invalidate them.

Workspaces live in local Downloads/editonny/date-title with two folders: a shared `exports/` and `project/v001/{edit,project}`. Remote execution requires an explicit accessible root. A revision copies mutable data, retaining its parent. Delivery stages video/subtitles in `exports/` and publishes unique filenames without overwriting existing files; the manifest is published last as the completion marker in the revision's `edit/deliveries/`. Destination hard-link support is required; no cache fallback exists.

The preview application separates shared state/API, playback, timeline, trims, marks, approval, and style controls. The local server has an explicit static allowlist and authenticated same-origin writes. Approval checks file versions to reject stale saves; later changes are also surfaced by the watcher.

The 3D scene runtime keeps its facade over focused timing, camera, material, text, label, media, and stage modules, sharing the same local Three.js instance. Time-driven scenes must support arbitrary seeking and preserve meaningful validation.

## Installation and distribution

Installation fingerprints source and destination, refuses foreign paths, stages a complete copy, preserves package `.env` and `engine/.env`, backs up the previous copy, and rolls back failures. Other personal files must live outside the installed skill. Agent links are symlinks, or directory junctions on Windows when symlinks need extra privileges; ownership and containment checks treat both as links. Unregistering removes only the selected agent links.

`scripts/package.py` builds the plugin ZIP, publishes the completed archive locally, and writes a SHA-256 file. The plugin uses an explicit content scope containing the skill, installer, launcher, manifests, README translations, licenses, and canonical documentation; the guide is deployed separately and is not packaged. Secrets, environments, caches, and unexpected symlinks are rejected or excluded.

The local graph is a derived navigation tool outside distribution. Its coverage and provenance do not replace current source or test evidence. See [setup](setup.md) and [validation](validation.md).
