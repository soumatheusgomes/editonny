# Development setup and provenance

Reviewed on 2026-10-19. This is the canonical development-tool reference; onboarding belongs in [installation](installation.md), observed quality evidence in [validation](validation.md). A tool's presence does not authorize external access, spending, publication, or global changes.

## Prepare the checkout

```sh
npm ci --ignore-scripts
uv sync --locked --directory skills/editonny/engine
npx playwright install chromium   # once, for npm run test:browser
npm run build
npm run check
python3 editonny.py doctor
```

On Windows, run the Python commands with `python` (or `py -3`); npm scripts start Python through `scripts/python.mjs`, which picks a working interpreter on each platform. `.gitattributes` keeps text files LF so byte-compared generated files match on every platform; a checkout made before it may need `git rm --cached -r . && git reset --hard` once, after saving local changes. Python I/O passes `encoding='utf-8'` explicitly and entry points set UTF-8 stdout/stderr, because Windows defaults to the ANSI code page; keep both for new code.

Developer Node requirement: `^22.13.0 || >=24.0.0`. The root `package.json` is an npm workspace with `design-system` (`@editonny/design-system`) and `web` (`@editonny/web`) sharing one lockfile; `npm ci --ignore-scripts` is enough because Next.js ships its compiler as platform packages. `npm run dev` serves the guide on port 3000, `npm run build` builds it, and `npm run test:browser` builds it and runs Playwright against `next start` on port 3100 (override with `E2E_PORT`). Stop leftover servers by port: Next.js renames its process to `next-server`. The Python engine range and exact selected dependencies are defined by its pyproject and lockfile. Keep HyperFrames pinned at 0.8.179 unless a separate tested upgrade is requested.

ESLint 10.13.0, @eslint/js 10.0.1, @eslint/css 2.0.0, globals 17.13.0, and esbuild 0.28.2 remain pinned. TypeScript is pinned at 6.0.3 rather than 7.x: typescript-eslint 8.71.2 (used by `eslint-config-next` 16.4.2) supports `typescript <6.1.0` and needs the TypeScript JavaScript API, which TypeScript 7 does not ship. Revisit when typescript-eslint supports TypeScript 7. `@eslint/css` parses CSS for the design-system adherence rule in `design-system/lint/`, ported from the Claude Design export's oxlint configuration and applied to design-system and web CSS. `npm run typecheck` (part of `npm run check`) checks `design-system/tsconfig.json` and runs `next typegen && tsc --noEmit` in `web/`.

The web app pins Next.js 16.4.2, React and React DOM 19.3.1, next-intl 4.14.9, babel-plugin-react-compiler 1.0.0, @playwright/test 1.64.1, and @axe-core/playwright 4.14.0; unit tests use Vitest 5.0.5 with Testing Library and jsdom (`vitest.config.mts` at the root runs the `design-system` and `web` projects). eslint-config-next's bundled plugins declare ESLint ≤9 as peers; they run under ESLint 10 through the config's rule-context shim, so `npm install` reports overridden peers. `npm audit --omit=dev` reported no production vulnerabilities on 2026-10-19; development-only advisories (for example `braces` through tooling) do not ship to the site.

### Deploy

Vercel builds the guide with Root Directory `web`, installing the workspace from the repository root; the build reads `skills/editonny/visuals/catalog.json` and the engine's font files from outside `web/`, so keep Vercel's default of including files outside the root directory. Optional environment variable: `NEXT_PUBLIC_SITE_URL` (canonical origin for metadata, sitemap, and Open Graph images; defaults to the Vercel production domain). No secrets are required. Deploying is a publication step and needs the owner's authorization. The root CI runs canonical checks on Node 22.13.0 and 24. Browser and Python engine integration checks are separate and do not call an LLM. Local success does not imply remote CI ran.

The `quality/max-lines` rule and self-check derive from the MIT-licensed [Vibe Coding Toolkit revision de66fe3](https://github.com/soumatheusgomes/vibe-coding-toolkit/tree/de66fe371a49f5a4d3b521759e5c401f390b6424). Keep `eslint-rules/LICENSE` and local adaptations. Review the source diff before updating; do not copy the whole toolkit or add unused logger/database rules.

## Working agreements

Read `AGENTS.md`, the relevant canonical docs, and current source. Use an ignored local plan under `docs/superpowers/` for temporary progress, with source/date/invalidation recorded for durable knowledge. On resumption, check Git, files, running processes, and fresh checks before trusting a handoff.

Work directly in the requested checkout and preserve unrelated local changes. New English contracts do not support previous experimental project formats. First-party code is MIT licensed; third-party terms remain separate. English is primary; the Portuguese and Spanish READMEs are explicit documentation translations.

Use installed Ponytail, Caveman, Superpowers, RTK, Context7, agent-browser (preview checks and ad-hoc inspection), Playwright (guide checks), and web-design-guidelines where relevant. They are development tools rather than package dependencies. Do not duplicate existing hooks or introduce new Git hooks. Native LLM work uses Codex, `gpt-6-astra`, and ChatGPT authentication only, with explicit model selection for delegation. No external LLM proxy, API key, or silent provider/model substitution is permitted.

Use one writer per file and read-only independent reviewers. The coordinator owns Git, lockfiles, integration, and verification. Scope assignments must name inputs, allowed paths, acceptance checks, and boundaries. Agent summaries require verification against actual files and checks.

## Local code and document graph

Graphifyy 0.9.91 is isolated from the engine. Its deterministic AST, cache, graph, community, report, visualization, and export functions are reused; semantic work is completed by native Codex agents.

```sh
npm run graph:check
npm run graph:update
npm run graph:code
npm run graph:exports
npm run graph:test
GRAPHIFY_NO_AUTO_REFRESH=1 graphify query 'createPrompt' --budget 1500
```

`graph:update` returns 2 when native-session work is pending. Read the indicated `.work/graphify/pending.json`, process each complete assigned source set through the Graphify extraction skill, write responses to assigned destinations, then repeat. Community-label tasks use `labels-request.json` and `labels-response.json`. The terminal never starts an LLM. Recorded provenance describes coordinated execution rather than proving authentication cryptographically.

Every task records source hashes and the official extraction prompt fingerprint. Documents are not truncated; batches limit input volume. Missing/changed sources, incomplete responses, wrong model/provenance, invalid graph endpoints, or invalid confidence block promotion. Cache entries are invalidated by prompt changes. Renamed, removed, or newly excluded origins are pruned. Interrupted updates preserve the served index and reusable staging; do not remove pending graph tasks casually.

`graph:code` updates AST without LLM and reuses only still-valid semantic results; modified document coverage stays explicitly pending. `graph:watch` uses deterministic staging and must be stopped after use. `graph:test` runs the graph pipeline tests in `tests/integration_graph/` under the isolated Graphify Python, with fixtures and no LLM or network. Query/MCP auto-refresh is disabled. Unchanged corpora should produce no new tasks. These commands are outside the canonical CI check.

Promotion validates the graph and rechecks the corpus before replacing `graphify-out/`; a failed rename restores the previous index. If a memory is saved between the final comparison and the swap, the new index stays live and the previous one, with that memory, stays in `.work/graphify-previous/`. Recover the memory from there, because the next promotion refuses to run while that backup exists. `run.json` records commit, local changes, hashes, coverage, and provenance. The index is a directed multigraph preserving parallel relations. Unknown native token counters remain unknown; do not infer cost or savings from zeros or unavailable counters.

The ignore policy excludes secrets, private/client configuration, environments, media, vendor code, generated bundles, locks, and outputs. Symlinks are not followed. Source text is data, not instructions to execute. Old derived exports are invalidated when the graph changes. `graph:exports` creates local HTML, tree, callflow, SVG, GraphML, Cypher, wiki, and Obsidian artifacts. These are disposable navigation aids, not a personal notes vault or distributable product content. Above 5,000 nodes, use native HTML aggregation. Do not transmit Cypher to a database without authorization.

Graphify 0.9.91 cannot export MultiDiGraph directly as GraphML. The wrapper uses an explicit temporary projection for that format and reports relation loss. Prefer `graph.json` when every parallel relation matters. Native summarized queries may also show only one relation per pair.

Tool extras observed in the existing isolated setup include MCP 2.3.0, matplotlib 3.11.2, watchdog 6.0.0, and SciPy 1.18.1. They are not engine dependencies. Do not reinstall global tools solely to validate the repository.

## MCP and client configuration

The local `editonny-graph` registration launches the isolated Graphify Python with `scripts/project_graph.py serve` over stdio. Local client configuration is ignored and excluded from distribution. Do not overwrite unrelated servers or permissions. A client restart may be required for discovery; a protocol test is distinct from an observed UI restart.

```sh
python3 scripts/check_graph_mcp.py
```

This checks initialization, tool listing, local queries, statistics, and report access, then closes the process. It does not run semantic extraction or an LLM. Claude clients can query the graph while semantic generation follows the native Codex policy.

## Invalidation and recovery

CLI/schema changes invalidate architecture and command examples. CSS/text/interaction changes invalidate visual/accessibility evidence. Dependency changes invalidate prior audits. Source/path changes invalidate graph coverage and derived exports. Client instruction changes invalidate discovery assumptions.

Inspect exact diffs and restore only affected files when recovering; preserve later and unrelated work. Never use a global reset or clean as a routine repair. Keep user projects and secrets outside the skill, test installers with a temporary home, and inspect rendered samples before removing task-owned `.work/` outputs.
