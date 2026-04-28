# Memex — Hermes Agent Plugin

Long-term memory for [Hermes Agent](https://hermes-agent.nousresearch.com), powered by [Memex](https://github.com/JasperHG90/memex).

See `memex/README.md` for the plugin-local documentation shown by `hermes memory setup`.

> Full user-facing documentation is in this README. If you're looking for the install instructions, skip to [Installation](#installation).

## Why Memex?

Most agent memory backends are opaque: a SaaS vendor decides what gets stored, how it is indexed, and when it is surfaced. Memex takes the opposite approach. You own the Postgres database, the markdown files, the vault structure. You can inspect, export, or migrate at any time.

Inside Hermes, Memex exposes a tool family covering memory-unit recall (facts/observations/events), whole-note retrieval, query-decomposing surveys, ingestion (`memex_retain` for new notes, `memex_append` for delta-only progress), the knowledge graph, note lifecycle, templates, assets, and namespaced operational state via the KV store. A session briefing — including a storage-model primer that explains how Memex's three layers (notes / memory units / KV) interact — is injected into the system prompt at startup; the full transcript is ingested at session end. Per-project vault binding mirrors the pattern used by Memex's Claude Code plugin.

## Prerequisites

1. Install the Memex CLI:

   ```bash
   uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
   ```

2. Initialize Memex and create a vault:

   ```bash
   memex config init
   memex vault create my-vault --description "My notes"
   ```

3. Start the Memex server (the plugin warns if unreachable):

   ```bash
   memex server start -d
   ```

## Installation

The plugin is a standalone package — no Memex CLI required.

```bash
uv tool install 'memex-hermes-plugin @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/hermes-plugin'
memex-hermes install
hermes memory setup    # select "memex"
```

Or install from a GitHub release wheel (attached to every Memex `v*` release):

```bash
uv tool install https://github.com/JasperHG90/memex/releases/download/<tag>/memex_hermes_plugin-<version>-py3-none-any.whl
memex-hermes install
```

Manual install (no Python tooling):

```bash
git clone https://github.com/JasperHG90/memex.git
cp -r memex/packages/hermes-plugin/src/memex_hermes_plugin/memex "$HERMES_HOME/plugins/memex"
```

`memex-hermes install` writes `memory.provider: memex` to `$HERMES_HOME/config.yaml` and symlinks the plugin directory into `$HERMES_HOME/plugins/memex/`. Use `--mode copy` for a real copy instead of a symlink, and `--force` to replace an existing install.

Commands: `memex-hermes install`, `memex-hermes status`, `memex-hermes uninstall`.

## Per-project vault binding

The plugin resolves which vault to use per project via the Memex KV store — the same pattern used by the Claude Code plugin. The project identifier is derived from the git remote origin URL (portable across team members' machines), falling back to the directory path for non-git projects.

On session start, it looks up the KV key `project:<project_id>:vault` — for example, `project:github.com/acme/myapp:vault`.

To bind a project to a vault, ask Hermes:

> Set this project's vault to "my-vault"

Or call Memex directly:

```bash
memex kv put "project:github.com/acme/myapp:vault" my-vault
```

If no per-project vault is set, writes go to the vault in `$HERMES_HOME/memex/config.json`, then to the Memex global vault.

## Configuration

### Config file

The plugin writes non-secret config to `$HERMES_HOME/memex/config.json`. Secrets go to `$HERMES_HOME/.env`.

| Key | Default | Description |
|---|---|---|
| `server_url` | `http://127.0.0.1:8000` | Memex server base URL |
| `vault_id` | _(unset)_ | Fallback vault when per-project lookup misses |
| `memory_mode` | `hybrid` | `hybrid` / `context` / `tools` (see below) |
| `create_vaults_on_init` | `true` | Auto-create a missing vault at session start |
| `briefing_budget` | `2000` | Token budget for the startup briefing |
| `briefing_refresh_cadence` | `0` | Refresh briefing every N turns (0 = never) |
| `recall.facts_limit` | `5` | Prefetch memory-unit count |
| `recall.notes_limit` | `3` | Prefetch note count |
| `recall.strategies` | `[semantic, keyword, temporal, graph, mental_model]` | TEMPR strategies to fuse |
| `recall.token_budget` | `2048` | Token budget per recall |
| `recall.include_stale` | `false` | Include stale memory units in prefetch (the `memex_recall` tool exposes `include_stale` for ad-hoc filtering) |
| `recall.include_superseded` | `false` | Include superseded memory units in prefetch (no tool-level toggle currently — superseded is config-only) |
| `recall.expand_query` | `false` | LLM query expansion for prefetch |
| `retain.session_template` | `hermes-session` | Template name for session notes |

### Environment variables

Env vars override the config file. Useful for CI, containerized setups, or per-shell tweaks:

- `MEMEX_SERVER_URL` — server base URL
- `MEMEX_API_KEY` — API key (secret; goes to `$HERMES_HOME/.env`)
- `MEMEX_VAULT` — alias for `vault_id`
- `MEMEX_HERMES_MODE` — alias for `memory_mode`

### Fallback to Memex's own config

If `$HERMES_HOME/memex/config.json` is absent, the plugin reads `MemexConfig()` (which in turn reads `~/.config/memex/config.yaml` and local `.memex.yaml`). Users who already run Memex locally can skip the Hermes-side config file entirely.

## Tools

The granularity mirrors Memex's MCP server — separate tools so the LLM can usefully dispatch in parallel, consolidated only when operations are genuinely sequential. Tool descriptions describe *what each tool does*; routing (when to pair tools vs. call them solo) and the storage-model primer (notes / memory units / KV — what each is for, what supersession looks like) are injected once per session via `system_prompt_block` rather than repeated in every schema.

Primary tools (the ones the LLM reaches for most often):

| Tool | Purpose |
|---|---|
| `memex_recall` | Search memory units (facts/observations/events). TEMPR fusion. |
| `memex_retrieve_notes` | Search whole notes ranked by relevance. |
| `memex_survey` | Decompose a broad query into sub-questions; server fans out. |
| `memex_retain` | Ingest a NEW note, or fully overwrite an existing one. |
| `memex_append` | Atomically append a delta to an existing note (preferred over re-`retain` for in-progress notes). |
| `memex_list_entities` | Search entities by name/type. |
| `memex_get_entity_mentions` | Source memory units mentioning a specific entity. |
| `memex_get_entity_cooccurrences` | Related entities with co-occurrence counts. |

Additional tools cover note lifecycle (`memex_set_note_status`, `memex_update_user_notes`, `memex_rename_note`), discovery (`memex_find_note`, `memex_list_vaults`, `memex_get_vault_summary`, page-index/node reads), templates (`memex_list_templates` / `memex_get_template` / `memex_register_template`), assets, and the KV store (`memex_kv_write` / `memex_kv_get` / `memex_kv_search` / `memex_kv_list`). See the schema list for the full surface.

### Routing (delivered via the system prompt)

- **Content lookup** — `memex_recall` + `memex_retrieve_notes` in parallel (same assistant message).
- **Broad / panoramic query** — `memex_survey` (single call).
- **Entity graph** — `memex_list_entities` first; then `memex_get_entity_mentions` and/or `memex_get_entity_cooccurrences` (safe to parallelise if both are needed).
- **Title known** — `memex_find_note` with a title fragment.
- **Capturing work** — `memex_retain` for a fresh note; `memex_append` to extend the running session note (or any existing note) without re-sending the full body.

## Memory modes

| Mode | Briefing | Prefetch | Tools |
|---|---|---|---|
| `hybrid` (default) | injected | yes | exposed |
| `context` | injected | yes | hidden |
| `tools` | skipped | skipped | exposed |

Set via `memory_mode` in the config file or `MEMEX_HERMES_MODE=tools`.

## What gets captured automatically

- **Session briefing** on startup — fetched from Memex via the `get_session_briefing` endpoint and injected into the system prompt.
- **Session transcript** on exit — `on_session_end` ingests the full conversation as a single note keyed `hermes:session:<ISO-timestamp>` with template `hermes-session`. Idempotent via the note key.
- **Pre-compression chunks** — `on_pre_compress` appends messages about to be discarded to the session note so nothing is lost.
- **Built-in memory writes** — Hermes' built-in MEMORY.md / USER.md writes are mirrored to the Memex KV store under `hermes:<target>:<hash>`.

## Troubleshooting

**"Memex server is not reachable"** — start it: `memex server start -d`. Check `$MEMEX_SERVER_URL` or `server_url` in the config file.

**"No vault configured"** — the plugin resolves `project:<project_id>:vault` from KV. Bind one: `memex kv put "project:<your_project_id>:vault" my-vault`. Run `memex hermes status` to see the derived project ID.

**Tools missing in chat** — check `memory_mode`. If set to `context`, tools are intentionally hidden. Use `hybrid` or `tools`.

**Permission errors under `$HERMES_HOME/plugins/memex/`** — the install step may have used `--mode copy` when a symlink was expected. Re-run: `memex hermes install --mode symlink`.

## Updating

```bash
uv tool upgrade memex-hermes-plugin
memex-hermes install --mode copy --force    # refresh the copy in $HERMES_HOME
```

## Uninstall

```bash
memex-hermes uninstall
uv tool uninstall memex-hermes-plugin
```

Removes `$HERMES_HOME/plugins/memex/` and (with `--purge-config`) `$HERMES_HOME/memex/config.json`.

## Development

Two test suites:

### Unit tests (fast; no external deps)

```bash
cd packages/hermes-plugin
just test                  # pytest -v, excludes hermes_integration marker
uv run ruff check . && uv run mypy src
```

Stubs Hermes' `MemoryProvider` ABC via `tests/_stubs/memory_provider.py`. Refresh when upstream changes by copying from https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_provider.py — the drift-detection test `test_default_strategies_match_server` will flag other breaking changes at test time.

### Integration tests (live Hermes loader + live Memex via uvicorn + testcontainers Postgres)

```bash
just test-integration       # full end-to-end
```

What it does:
1. `uv sync --all-packages --dev --group hermes-integration` — installs `hermes-agent` from GitHub
2. Spins up `pgvector/pgvector:pg18-trixie` via testcontainers
3. Boots the Memex FastAPI app via `uvicorn` on a free port in a background thread
4. Discovers + loads the plugin through Hermes' real `plugins.memory.load_memory_provider`
5. Exercises every lifecycle hook against real HTTP + real Postgres

Skipped automatically if Docker or hermes-agent is unavailable. Assumes no LLM credentials — tests that rely on LLM output (survey decomposition, fact extraction on recall) are deliberately scoped to return-empty-on-empty-graph cases.

## Upstreaming

This plugin ships from the Memex monorepo. After the plugin has user traction, we'll propose contributing a bundled copy to `NousResearch/hermes-agent/plugins/memory/memex/` so it's discoverable without the `memex hermes install` step.
