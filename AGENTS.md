# CLAUDE.md

Instructions for LLM agents working in this repository.

<constraint name="code-search-protocol" priority="critical">
All code discovery MUST use `qmd`. Collections: `memex_src` (source), `memex_test` (tests).

Step 1 (MANDATORY): `qmd search --line-numbers -n 10 "<exact_term>" --collection <COLLECTION>`
Step 2 (MANDATORY if Step 1 fails): `qmd vsearch --line-numbers -n 10 "<concept>" --collection <COLLECTION>`
Step 3 (ONLY if Steps 1+2 fail): `qmd query --line-numbers -n 10 "<question>" --collection <COLLECTION>`

PROHIBITED: Skipping to `query`. Skipping Step 2. Using `read_file`, `glob`, `grep`, `ripgrep`, `find`, `search_file_content`, or any shell-based search for discovery before completing Steps 1-3.
OVERRIDE: Use `run_shell_command` with `qmd search` for all code discovery.
</constraint>

<constraint name="code-retrieval-protocol" priority="critical">
Step 1 (MANDATORY): `qmd get <file>:<line> -l <lines>` — start with `-l 50`.
Step 2 (FALLBACK): `qmd get <file>` — FORBIDDEN unless Step 1 output was insufficient.
Exception: Config files (toml/yaml/json) under 100 lines may be read fully.
</constraint>

## Project

Memex: long-term memory system for LLMs. Stores notes as Markdown (FileStore) with PostgreSQL+pgvector for metadata and vector search (MetaStore).

## Commands

```bash
just setup                                  # install deps + pre-commit hooks
just test                                   # pytest on /tests
just prek                                   # linting/formatting (ruff + mypy)
uv run pytest tests/test_file.py::test_name -v  # single test
uv run pytest -m integration                # integration tests (require Docker)
uv run pytest -m llm                        # tests requiring LLM API calls
uv add --dev <package>                      # add dev dep to root
uv add <package> --package memex_core       # add dep to specific package
just dashboard-dev                          # run dashboard dev server
just dashboard-build                        # build dashboard for production
```

Always use `uv`, never `pip`.

## Architecture

Python monorepo managed by `uv`.

- `packages/core` — core library: storage engines, memory system (extraction/retrieval/reflection), MemexAPI, FastAPI server
- `packages/cli` — Typer CLI (`memex` command)
- `packages/mcp` — FastMCP server for LLM integration
- `packages/common` — shared Pydantic models, config, exceptions
- `packages/dashboard` — React + Vite web UI
- `packages/openclaw` — Memex memory plugin for OpenClaw (TypeScript/Node)

### Memory model (Hindsight Framework)

1. Extraction (`memex_core.memory.extraction`) — LLM fact extraction, entity resolution, embeddings
2. Retrieval (`memex_core.memory.retrieval`) — TEMPR: 5 strategies (Temporal, Entity, Mental Model, Keyword, Semantic) + Reciprocal Rank Fusion
3. Reflection (`memex_core.memory.reflect`) — synthesize observations into mental models via LLM

### Entry points

- `memex_core.api.MemexAPI` — main API class
- `memex_core.server` — FastAPI REST server
- `memex_core.memory.engine.MemoryEngine` — memory orchestrator
- `memex_cli.__init__.app` — Typer CLI app
- `memex_mcp.server.mcp` — MCP server instance

### Architectural decisions

- Distributed reflection queue: PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` for atomic task claiming
- Append-only design: notes are immutable, new versions create new entries
- fsspec abstraction: storage is backend-agnostic (local, S3, GCS)

## Git workflow

- Commit after completing each logical unit of work — do not batch unrelated changes
- Use conventional commit messages: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`
- Include scope when relevant: `feat(dashboard):`, `fix(core):`
- Prefer small, frequent commits over large ones

## Code style

- Single quotes
- Line length: 100
- Formatter: ruff (not black)
- Type hints: strict, mypy enforced
- Python >= 3.12
- All I/O: async (asyncio)

## Testing

- Root tests (`/tests/`) — E2E
- Unit tests (`packages/core/tests/unit/`)
- Markers: `@pytest.mark.integration`, `@pytest.mark.llm`
- Test DB: PostgreSQL via `testcontainers`

<constraint name="test-practices">
- Use `uuid4()` in content to prevent `idempotency_check` failures.
- Use `patch.dict(os.environ, ...)` for config tests.
- Ensure `ensure_db_env_vars` fixture is active for E2E tests.
</constraint>
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

<constraint name="proactive-memory-capture" priority="critical">
### Capture — MANDATORY

Call `memex_add_note` (with `background: true`, `author: "claude-code"`) when any of these apply:

1. Completed a multi-step task (save what was done, decisions, outcome)
2. Diagnosed a bug root cause (save symptom, cause, fix)
3. Made/discovered an architectural decision (save decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

**Keep notes concise** (hard maximum: 300 tokens). Capture the key insight, not a detailed report. No per-file changelogs.
</constraint>

### Retrieval

Session start context is automatic via hook. Do NOT redundantly search at session start.

PROHIBITED:
- `memex_list_notes` for discovery.
- Fabricating Note/Node/Unit IDs. Only use IDs from tool output.
- `memex_get_note_metadata` after `memex_note_search` (metadata already inline).
- `memex_read_note` on notes over 500 tokens. Use `memex_get_page_index` + `memex_get_node`.
- Creating diagrams/charts without first checking assets for visual context via `memex_list_assets` → `memex_get_resource`.

**Search** — pick by query type, or run both in parallel:
- `memex_memory_search` — atomic facts, observations, mental models. Broad queries.
- `memex_note_search` — ranked source notes with inline metadata. Targeted lookup.

**Filter** — before reading:
- After `memex_memory_search`: call `memex_get_note_metadata` to check relevance.
- After `memex_note_search`: use inline metadata directly.

**Read** — only confirmed-relevant notes:
1. `memex_get_page_index` → TOC + node IDs
2. `memex_get_node` (parallel) → section content
3. `memex_read_note` → only when total_tokens < 500

**Assets** — required when `has_assets: true`:
- `memex_list_assets` → `memex_get_resource` → render inline.

### Citations

When presenting information from Memex, use numbered citations [1], [2] etc. inline. Add a reference list at the end with the source type prefix:
- `[note]` — title + note ID
- `[memory]` — title + memory ID + source note ID
- `[asset]` — filename + note ID

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
