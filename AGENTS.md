# CLAUDE.md

Instructions for LLM agents working in this repository.

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
| `packages/cli` | `memex_cli` | Typer CLI (`memex` command) — 13 command groups: note, vault, memory, entity, kv, server, mcp, config, system, database, setup, hermes, report-bug |
| `packages/mcp` | `memex_mcp` | FastMCP server — 35 tools for LLM integration (progressive disclosure by default) |
| `packages/common` | `memex_common` | Shared Pydantic models, config (hierarchical YAML), HTTP client, exceptions |
| `packages/eval` | `memex_eval` | Evaluation: internal synthetic benchmarks + external LoCoMo benchmark with LLM-as-judge |
| `packages/firefox-extension` | — | TypeScript/WebExtension for saving pages to Memex |
| `packages/claude-code-plugin` | — | Claude Code plugin: `/remember` and `/recall` skills, session hooks, MCP server config |
| `packages/hermes-plugin` | `memex_hermes_plugin` | Hermes Agent memory provider plugin |

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
```

### Core architecture (packages/core)

**Layered design**: FastAPI routes → Services → Memory engines → Storage

```
memex_core/
├── server/          # FastAPI route handlers
├── services/        # Domain logic layer
├── memory/
│   ├── extraction/  # LLM fact extraction pipeline (DSPy signatures, chunking, dedup)
│   ├── retrieval/   # TEMPR: 5 strategies + RRF + MMR diversity
│   ├── reflect/     # Hindsight reflection loop (Phases 0-6)
│   ├── contradiction/ # Contradiction detection between facts
│   ├── models/      # Embedding, reranking, NER model backends
│   └── sql_models.py # Full DB schema
├── storage/         # MetaStore (Postgres+pgvector), FileStore (local/S3/GCS)
├── processing/      # Content processing (batch jobs, dates, files, web scraping, titles)
├── api.py           # MemexAPI — main facade class
├── llm.py           # DSPy/LiteLLM executor with circuit breaker
├── circuit_breaker.py # LLM call resilience
├── scheduler.py     # Background reflection with Postgres advisory lock leader election
├── metrics.py       # Prometheus metrics
├── tracing.py       # OpenTelemetry instrumentation
└── alembic/         # Database migrations
```

### Key architectural patterns

- **Distributed reflection queue**: PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` for atomic task claiming
- **Note identity is stable; content is mutable in place**. `note_key` upsert creates new versions; append extends in place. Lifecycle states gate operations.
- **fsspec abstraction**: storage is backend-agnostic (local, S3, GCS)
- **Circuit breaker**: LLM call resilience with Prometheus metrics
- **Leader election**: Postgres advisory locks for background reflection scheduling
- **Multi-tenancy**: vault-scoped data isolation with global vault fallback
- **Lineage tracking**: upstream/downstream provenance chains
- **Entity graph**: cooccurrence tracking + hybrid ranking

## Agent-surface architecture

Agent-facing prompt content lives in three tiers. SSOTs are `packages/common/src/memex_common/agent_surface.py` (universal) and `packages/common/src/memex_common/tool_descriptions.py` (per-tool).

<constraint name="agent-surface-tiers" priority="high">
1a (MCP, terse): `memex_mcp/server.py` `instructions=` (~500 tok) + per-tool descriptions (~300 tok each). Tool contracts + 4xx triggers ONLY. No multi-step composition flow.
1b (universal, SSOT): `memex_common.agent_surface` (~2,050 tok / ≤7,200 chars; cap pinned by `test_agent_surface.py`). LLM-optimized; concrete; imperative. Composed via `compose_universal()`.
2 (agent-specific): hermes `briefing.py` + Claude Code SessionStart hook output (~400 tok each).
</constraint>

**Decision rule** — where does new agent-facing prose go?
- Triggers a 4xx at the server? → MCP tool description (`tool_descriptions.py` if cross-package, inline in `server.py` if MCP-only).
- Universal across agents (storage model, routing, paired writes)? → `agent_surface`.
- Agent-specific framing (capture cadence, slash commands, prohibitions)? → `agent_harnesses.py` (cross-package SSOT; consumed by both Hermes briefing and Claude Code SessionStart hook).
- Slash-command behavior (`/remember`, `/recall`)? → Claude Code plugin only.

**Adding a new MCP tool**: if Hermes mirrors the schema, put the description in `tool_descriptions.py` (SSOT) and import by identity on both sides. Otherwise inline in `server.py`. Either way: stay within the per-tool 1,200-char cap (1,800 for the 5 F3 search tools that embed `LAYER_ROUTING_PRIMER_FRAGMENT`).

**Composition**: hermes imports `agent_surface` + `agent_harnesses` in-process; Claude Code receives both via `memex agent-surface claude-code` from the plugin's `SessionStart` hook (positional target arg, required). Drift prevention: every cross-package surface imports the SAME object from `memex_common` (identity check in tests).

**Enforcement** — six load-bearing test files:
- `packages/common/tests/test_agent_surface.py` — universal block budget + content.
- `packages/common/tests/test_tool_descriptions.py` — per-tool budgets + load-bearing content.
- `packages/common/tests/test_agent_harnesses.py` — Tier 2 SSOT identity + budgets.
- `packages/mcp/tests/test_description_budgets.py` — registered-tool char caps + F3 carve-out.
- `packages/mcp/tests/test_no_universal_content_in_descriptions.py` — banned-phrase boundary fence.
- `packages/hermes-plugin/tests/test_briefing_budget.py` + `packages/cli/tests/test_agent_surface_cli.py` — per-surface budgets + CLI profile invariants.

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

- Root tests (`/tests/`) — E2E tests against real Postgres via testcontainers
- Unit tests (`packages/core/tests/unit/`) — mocked dependencies, no database
- Integration tests (`packages/core/tests/integration/`) — real Postgres, no mocks
- Package tests (`packages/{cli,mcp,eval}/tests/`) — package-specific tests

### Markers

- `@pytest.mark.integration` — requires Docker/Postgres
- `@pytest.mark.llm` — requires real LLM API calls (ANTHROPIC_API_KEY)
- `@pytest.mark.llm_mock` — uses `MockDspyLM` with deterministic golden responses
- `@pytest.mark.benchmark` — performance benchmarks

<constraint name="test-practices">
- Use `uuid4()` in content to prevent `idempotency_check` failures.
- Use `patch.dict(os.environ, ...)` for config tests.
- Ensure `ensure_db_env_vars` fixture is active for E2E tests.
</constraint>

<!-- BEGIN aim: header -->
Agent instructions. `aim:` marker regions are auto-generated by [aim](https://github.com/JasperHG90/agent-integrations-manager) — edit outside markers.
<!-- END aim: header -->

<!-- BEGIN aim: guidelines -->
Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
<!-- END aim: guidelines -->
