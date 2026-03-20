# CLAUDE.md

Instructions for LLM agents working in this repository.

<constraint name="code-search-protocol" priority="critical">
All code discovery MUST use `qmd`. Collections: `memex_test` (all source: py/toml/yaml/tsx/ts/svg/json), `memex_src` (test files: test_*.py), `memex_md` (markdown: *.md).

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
- `packages/eval` — LoCoMo evaluation framework and benchmarks
- `packages/claude-code-plugin` — Claude Code plugin for cross-project memory integration
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
