# CLAUDE.md

Instructions for LLM agents working in this repository.

<constraint name="code-search-protocol" priority="critical">
All code discovery MUST use `qmd`. Collections: `memex_src` (all source: py/toml/yaml/tsx/ts/svg/json), `memex_test` (test files: test_*.py), `memex_md` (markdown: *.md).

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

Memex is a long-term memory system for LLMs. It stores notes as Markdown files (FileStore) with PostgreSQL+pgvector for metadata, full-text search (tsvector), and vector search (MetaStore). The core idea is the **Hindsight Framework**: ingest content, extract structured facts/observations/events, retrieve them using multiple strategies, and synthesize mental models through reflection.

## Commands

```bash
just setup                                  # install deps + pre-commit hooks
just test                                   # pytest on /tests
just prek                                   # linting/formatting (ruff + mypy)
just audit                                  # check deps for vulnerabilities
just docs-serve                             # local docs with live reload
just docs-build                             # build documentation site
just db-upgrade                             # run alembic migrations
just db-revision "message"                  # create new migration
just benchmark                              # run pytest benchmarks
uv run pytest tests/test_file.py::test_name -v  # single test
uv run pytest -m integration                # integration tests (require Docker)
uv run pytest -m llm                        # tests requiring LLM API calls
uv run pytest -m benchmark                  # performance benchmarks
uv add --dev <package>                      # add dev dep to root
uv add <package> --package memex_core       # add dep to specific package
```

Always use `uv`, never `pip`.

## Architecture

Python monorepo managed by `uv` with 8 packages.

### Packages

| Package | Import | Purpose |
|---------|--------|---------|
| `packages/core` | `memex_core` | Core library: storage, memory engine (extraction/retrieval/reflection), services layer, MemexAPI facade, FastAPI server |
| `packages/cli` | `memex_cli` | Typer CLI (`memex` command) — 12 command groups: note, vault, memory, entity, kv, server, mcp, config, system, database, setup, report-bug |
| `packages/mcp` | `memex_mcp` | FastMCP server — 31 tools for LLM integration |
| `packages/common` | `memex_common` | Shared Pydantic models, config (hierarchical YAML), HTTP client, exceptions |
| `packages/eval` | `memex_eval` | Evaluation: internal synthetic benchmarks + external LoCoMo benchmark with LLM-as-judge |
| `packages/obsidian-sync` | `memex_obsidian_sync` | Watchdog-based Obsidian vault synchronization |
| `packages/firefox-extension` | — | TypeScript/WebExtension for saving pages to Memex |
| `packages/claude-code-plugin` | — | Claude Code plugin: `/remember` and `/recall` skills, session hooks, MCP server config |

### Dependency graph

```
memex-cli
├── memex-common
├── memex-core [optional: server extra]
├── memex-mcp [optional: mcp extra]
└── watchdog, sqlmodel, structlog [optional: sync extra]

memex-mcp → memex-common
memex-core → memex-common
memex-eval → memex-common
memex-obsidian-sync → memex-common
```

### Core architecture (packages/core)

**Layered design**: FastAPI routes → Services → Memory engines → Storage

```
memex_core/
├── server/          # FastAPI route handlers (ingestion, retrieval, notes, entities, etc.)
├── services/        # Domain logic layer (ingestion, search, notes, reflection, lineage, etc.)
├── memory/
│   ├── extraction/  # LLM fact extraction pipeline (DSPy signatures, chunking, dedup)
│   ├── retrieval/   # TEMPR: 5 strategies + RRF + MMR diversity
│   ├── reflect/     # Hindsight reflection loop (Phases 0-6)
│   ├── contradiction/ # Contradiction detection between facts
│   ├── models/      # Embedding, reranking, NER model backends
│   └── sql_models.py # Full DB schema (Entity, MemoryUnit, MentalModel, Note, Chunk, etc.)
├── storage/         # MetaStore (Postgres+pgvector), FileStore (local/S3/GCS)
├── processing/      # Content processing (batch jobs, dates, files, web scraping, titles)
├── api.py           # MemexAPI — main facade class
├── llm.py           # DSPy/LiteLLM executor with circuit breaker
├── circuit_breaker.py # LLM call resilience (CLOSED→OPEN→HALF_OPEN)
├── scheduler.py     # Background reflection with Postgres advisory lock leader election
├── metrics.py       # Prometheus metrics
├── tracing.py       # OpenTelemetry instrumentation
└── alembic/         # Database migrations
```

### Memory model (Hindsight Framework)

1. **Extraction** (`memex_core.memory.extraction`) — Multi-phase pipeline: chunk text → extract facts/observations/events via DSPy → entity resolution → deduplication → embedding generation → persistence
2. **Retrieval** (`memex_core.memory.retrieval`) — TEMPR: 5 strategies (Temporal, Entity/Graph, Mental Model, Keyword/BM25, Semantic/vector) fused with Reciprocal Rank Fusion + MMR diversity filtering. Supports query expansion via LLM.
3. **Reflection** (`memex_core.memory.reflect`) — 7-phase Hindsight loop: liveness → validation → comparison → validation → update → finalization → enrichment. Synthesizes observations into mental models with trend tracking (new/stable/strengthening/weakening/stale).
4. **Contradiction** (`memex_core.memory.contradiction`) — Detects conflicting facts, classifies relationships, adjusts confidence.

### Entry points

- `memex_core.api.MemexAPI` — main API facade (ingest, recall, reflect, entity/note CRUD)
- `memex_core.server` — FastAPI REST server (NDJSON streaming, rate limiting, auth, webhooks)
- `memex_core.memory.engine.MemoryEngine` — memory engine factory and orchestration
- `memex_core.services.*` — domain service layer (ingestion, search, notes, reflection, lineage, entities, vaults, kv, stats, audit)
- `memex_cli.__init__.app` — Typer CLI app
- `memex_mcp.server.mcp` — MCP server (31 tools)

### Inference model backends

Embedding and reranking models are configurable via `server.embedding_model` and `server.memory.retrieval.reranker`. Both default to built-in ONNX models; set `type: litellm` to use any litellm-supported provider (OpenAI, Gemini, Cohere, Ollama, etc.).

- Protocols: `EmbeddingsModel`, `RerankerModel` in `memex_core.memory.models.protocols`
- ONNX backends: `FastEmbedder`, `FastReranker` in `memex_core.memory.models`
- LiteLLM backends: `memex_core.memory.models.backends.litellm_embedder`, `litellm_reranker`
- Factory functions: `get_embedding_model(config)`, `get_reranking_model(config)` dispatch on config type
- Reranker logit transform: litellm providers return [0,1] scores; the adapter applies inverse sigmoid so the retrieval engine's sigmoid normalisation (`retrieval/engine.py:987`) recovers the original scores

### Key architectural patterns

- **Distributed reflection queue**: PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` for atomic task claiming
- **Append-only design**: notes are immutable, new versions create new entries (lifecycle: active → superseded/appended/archived)
- **fsspec abstraction**: storage is backend-agnostic (local, S3, GCS)
- **Circuit breaker**: LLM call resilience with Prometheus metrics
- **Leader election**: Postgres advisory locks for background reflection scheduling
- **Multi-tenancy**: vault-scoped data isolation with global vault fallback
- **Lineage tracking**: upstream/downstream provenance chains (Document ↔ Memory Unit ↔ Mental Model)
- **Entity graph**: cooccurrence tracking + hybrid ranking (mention count + retrieval frequency + centrality)
- **Stable chunking**: sentence/code block/list-aware text splitting

## Git workflow

- Commit after completing each logical unit of work — do not batch unrelated changes
- Use conventional commit messages: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`
- Include scope when relevant: `feat(cli):`, `fix(core):`
- Prefer small, frequent commits over large ones

## Code style

- Single quotes
- Line length: 100
- Formatter: ruff (not black)
- Type hints: strict, mypy enforced
- Python >= 3.12
- All I/O: async (asyncio)

## Testing

### Structure

- Root tests (`/tests/`) — E2E tests against real Postgres via testcontainers (`pgvector/pgvector:pg18-trixie`)
- Unit tests (`packages/core/tests/unit/`) — mocked dependencies, no database
- Integration tests (`packages/core/tests/integration/`) — real Postgres, no mocks
- Package tests (`packages/{cli,mcp,eval}/tests/`) — package-specific tests

### Markers

- `@pytest.mark.integration` — requires Docker/Postgres
- `@pytest.mark.llm` — requires real LLM API calls (ANTHROPIC_API_KEY)
- `@pytest.mark.llm_mock` — uses `MockDspyLM` with deterministic golden responses
- `@pytest.mark.benchmark` — performance benchmarks

### Key fixtures

- `postgres_container` (session) — testcontainers Postgres lifecycle
- `client` / `async_client` (function) — FastAPI TestClient with real DB
- `tmp_env` (function, autouse) — isolated config/data/logs dirs per test
- `_truncate_db` (function) — clean tables + re-seed global vault between tests
- `mock_dspy_lm` — deterministic LLM mock with response queue (unit tests)

Async mode: `asyncio_mode = "auto"` — all async tests run automatically.

<constraint name="test-practices">
- Use `uuid4()` in content to prevent `idempotency_check` failures.
- Use `patch.dict(os.environ, ...)` for config tests.
- Ensure `ensure_db_env_vars` fixture is active for E2E tests.
</constraint>
