# Code Audit Backlog

> Generated from comprehensive codebase audit (2026-03-13). 10 senior developers scanned the entire repository. Findings reviewed by 4 staff engineers and 2 SE reviewers. Duplicates merged, false positives dropped, estimates and priorities calibrated.

## Priority Legend
- **P0 (Critical)**: Fix before next release
- **P1 (High)**: Fix this sprint
- **P2 (Medium)**: Schedule this quarter
- **P3 (Low)**: Nice-to-have / opportunistic

## Summary

- **Total tickets:** 41
- **By priority:** P0: 3 | P1: 8 | P2: 24 | P3: 6
- **By category:** Architecture: 11 | Security: 8 | Code Quality: 12 | Testing: 10

---

## P0 — Critical

### AUDIT-001: MCP server violates layer boundaries — direct core imports
**Category:** Architecture
**Size:** M
**Files:** `packages/mcp/src/memex_mcp/server.py`, `packages/core/src/memex_core/api.py`

**Description:** The MCP server bypasses the API layer in two places: (1) `server.py:1746` imports `get_embedding_model` directly from core internals to generate embeddings for KV writes — embedding generation should be delegated to a new `MemexAPI.embed_text()` method. (2) `server.py:876` calls `NoteService._filter_toc()`, a private method — expose this through `MemexAPI` or the page index endpoint. Both violations couple MCP to core implementation details and pull heavyweight ML dependencies into the MCP process.

**Why:** A core refactor (renaming `_filter_toc`, changing embedding model init) silently breaks MCP at runtime with no compile-time signal. The embedding import forces the MCP process to load ML model weights unnecessarily.

---

### AUDIT-002: N+1 semantic dedup queries in extraction storage
**Category:** Architecture
**Size:** M
**Files:** `packages/core/src/memex_core/memory/extraction/storage.py`

**Description:** `check_duplicates_in_window` (storage.py:366-392) issues one `SELECT` with a cosine distance filter per extracted fact. For a note with 30-50 facts, this means 30-50 sequential database roundtrips, each involving a pgvector index scan. Batch into a single query using a CTE/`VALUES` clause, `unnest()` with a parameterized array, or at minimum `asyncio.gather()` for parallel execution.

**Why:** Ingestion is already the slowest user-facing operation. This N+1 pattern is the single largest contributor to per-note latency. Batching could reduce dedup time by 10-30x for typical notes. Without fixing it, scaling requires proportionally more database capacity.

---

### AUDIT-003: Broken async/sync pattern in E2E tests
**Category:** Testing
**Size:** S
**Files:** `tests/test_background_ingestion.py`, `tests/test_e2e_batch.py`, `tests/test_e2e_batch_progress.py`

**Description:** Three E2E test files use `async def` test functions with `await asyncio.sleep()` inside polling loops but receive a synchronous `TestClient` fixture. `TestClient` runs requests synchronously with its own internal event loop. The `await asyncio.sleep()` calls work by accident. Fix by converting to sync `def` tests with `time.sleep()`, or switching to the existing `async_client` fixture (httpx `AsyncClient`, defined in `conftest.py:309`).

**Why:** These tests pass by coincidence. A pytest-asyncio version upgrade or event loop policy change could produce false passes where background jobs appear to complete instantly. Covers core async workflows (background ingestion, batch processing).

---

## P1 — High

### AUDIT-004: Add CORS middleware to FastAPI server
**Category:** Security
**Size:** S
**Files:** `packages/core/src/memex_core/server/__init__.py`, `packages/common/src/memex_common/config.py`

**Description:** The FastAPI application does not configure any CORS middleware. Add `CORSMiddleware` with configurable allowed origins via `MemexConfig`. Default to restrictive settings (localhost only). The dashboard already communicates with the API and will break in browser contexts without proper CORS headers.

**Why:** Without CORS headers, browsers reject all cross-origin requests. The dashboard and any third-party web integrations cannot function in standard deployment topologies where API and frontend are served from different origins. This is a deployment blocker.

---

### AUDIT-005: Enforce authentication for non-localhost binding
**Category:** Security
**Size:** S
**Files:** `packages/core/src/memex_core/server/__init__.py`

**Description:** Binding to a non-localhost address without authentication (server/__init__.py:56) only emits a `logger.warning`. Change to a startup error (or require an explicit `--allow-insecure` flag) when `host != '127.0.0.1'` and auth is disabled. The current behavior silently exposes an unauthenticated API to the network.

**Why:** A warning log is easily missed. An operator who binds to `0.0.0.0` and forgets to enable auth exposes the entire memory store (read, write, delete) as publicly writable. Fail-closed is the only safe default.

---

### AUDIT-006: Inconsistent 202 response payloads across ingestion endpoints
**Category:** Architecture
**Size:** M
**Files:** `packages/core/src/memex_core/server/ingestion.py`

**Description:** Five ingestion endpoints return three different 202 payloads. `/ingestions?background=true` returns a trackable `BatchJobStatus`; `/ingestions/url` and `/ingestions/upload` return `{'status': 'accepted'}` with no job ID (untrackable fire-and-forget); `/ingestions/webhook` returns a full `IngestResponse` (not actually async). Standardize all 202 responses to return `BatchJobStatus` with a `job_id`. Route background URL and upload ingestion through `JobManager`.

**Why:** Users calling `/ingestions/url?background=true` receive "accepted" with no mechanism to detect failure. This is a silent data loss vector for any integration that relies on background ingestion.

---

### AUDIT-007: Vault ID resolution returns inconsistent types between server and MCP
**Category:** Architecture
**Size:** S
**Files:** `packages/core/src/memex_core/server/common.py`, `packages/mcp/src/memex_mcp/server.py`, `packages/core/src/memex_core/server/entities.py`

**Description:** *Partially addressed by vault config refactor (commits `b73e06c`–`21a3cc4`).* MCP vault params are now optional with config defaults, but two issues remain: (1) Server `resolve_vault_ids()` returns `list[UUID] | None` while MCP `_resolve_vault_ids()` returns `list[UUID | str]` — type mismatch. (2) Entity endpoints `get_entity` and `get_entities_batch` (entities.py:161,172) still hardcode `api.config.server.default_active_vault` instead of accepting a `vault_id` parameter. Fix: tighten MCP return type to `list[UUID]`, add `vault_id` param to entity detail endpoints.

**Why:** Entity detail endpoints are locked to the server's default vault, breaking multi-vault workflows where a user queries a non-default vault. The type mismatch is a latent bug for UUID comparison in SQL queries.

---

### AUDIT-008: Path traversal validation inconsistency and LRU cache sharing bug in filestore
**Category:** Security
**Size:** S
**Files:** `packages/core/src/memex_core/storage/filestore.py`

**Description:** Four separate path traversal validation checks exist with similar but not identical logic (Base uses POSIX normpath; Local uses `os.path.realpath`; S3/GCS use POSIX normpath). Extract a single `validate_path_safe()` function. Additionally, `LocalAsyncFileStore.join_path` (lines 306-307) uses `@cached(cache=LRUCache(maxsize=128))` on an instance method. While `self` is included in the cache key (preventing cross-instance path pollution), the cache is shared across all instances — causing shared eviction pressure and retaining references to dead instances (memory leak for short-lived stores). Move the cache to an instance attribute.

**Why:** If one of the four validation paths is weaker than the others, an attacker could route requests through it. The shared LRU cache causes eviction interference across instances and potential memory leaks.

---

### AUDIT-009: Misleading `.uuid` property naming in NoteInput and test_api_idempotency
**Category:** Code Quality
**Size:** XS
**Files:** `packages/core/src/memex_core/api.py`, `packages/core/tests/unit/test_api_idempotency.py`

**Description:** `NoteInput.uuid` (api.py:143) returns `self.note_key`, which is an MD5 hex digest — not a UUID object or UUID-formatted string. The test at line 51-52 works correctly (both sides produce matching hex strings), but the property name `.uuid` and the test comment "Verify the UUID format" are misleading. Rename the property to `.note_key` or `.idempotency_key` and update the test comment.

**Why:** Developers encountering `.uuid` will expect a UUID-formatted string with dashes. The current naming creates confusion when integrating with systems that expect actual UUIDs (e.g., database UUID columns, which auto-cast but mask the format mismatch).

---

### AUDIT-010: Monitor high-risk third-party dependencies
**Category:** Security
**Size:** S
**Files:** `packages/core/pyproject.toml`, CI configuration

**Description:** Three dependencies warrant active monitoring: `trafilatura` (parses untrusted HTML), `cloudscraper` (executes untrusted code patterns), and `pymupdf4llm` (processes untrusted binary PDFs). Set up automated dependency scanning (`pip-audit` in CI, Dependabot/Renovate) and pin to reviewed versions rather than open-ended `>=` ranges.

**Why:** The ingestion pipeline processes arbitrary URLs and files from users. These libraries are historically frequent sources of CVEs. A vulnerability could allow remote code execution via crafted input. Without active monitoring, known vulnerabilities go unpatched indefinitely.

---

### AUDIT-041: `memex_recent_notes` (MCP) and `memex note recent` (CLI) have inconsistent vault scoping defaults
**Category:** Architecture
**Size:** S
**Files:** `packages/mcp/src/memex_mcp/server.py`, `packages/cli/src/memex_cli/notes.py`, `packages/core/src/memex_core/services/notes.py`

**Description:** The MCP tool and CLI command handle vault scoping differently when no vault is specified, and neither defaults to "all vaults":

1. **MCP (`memex_recent_notes`, server.py:1291):** Accepts a single `vault_id: str | None`. When omitted, passes `vault_id=None` to `api.get_recent_notes()`, which reaches `NoteService.get_recent_notes()` with an empty `ids` list — effectively querying all vaults. However, it only accepts a single vault, not a list. Users cannot scope to 2 of 5 vaults.
2. **CLI (`memex note recent`, notes.py:352):** Falls back to `config.read_vaults` when no `--vault` flag is given. `read_vaults` resolves through `vault.search > [vault.active] > [server.default_reader_vault]` — always scoping to at least one configured vault, never all vaults. Multi-vault via `--vault a --vault b` works but the default is restrictive.
3. **NoteService (notes.py:395-403):** Already supports the correct behavior — when both `vault_id` and `vault_ids` are `None`, no vault filter is applied and all vaults are queried.

Fix: (a) Change MCP tool to accept `vault_ids: list[str] | None` (matching `memex_memory_search` and `memex_note_search` patterns). Default to `None` (all vaults). (b) Change CLI fallback from `config.read_vaults` to `None` when no `--vault` is passed, so the default is all vaults. (c) Both surfaces should support optional multi-vault filtering while defaulting to cross-vault queries.

**Why:** Users with multiple vaults see different results from `memex note recent` vs `memex_recent_notes` for the same data. The CLI silently hides notes from non-default vaults, which is confusing when a user knows a note exists but `recent` does not show it. The MCP tool cannot scope to a subset of vaults (e.g., 2 of 5), forcing all-or-one. Aligning both surfaces to default to all vaults with optional multi-vault filtering matches the existing behavior of `memex_memory_search` and `memex_note_search`.

---

## P2 — Medium

### AUDIT-011: Validate and cap limit/offset query parameters
**Category:** Security
**Size:** S
**Files:** `packages/core/src/memex_core/server/entities.py`, `packages/core/src/memex_core/server/reflection.py`, `packages/core/src/memex_core/server/notes.py`

**Description:** Several server endpoints accept `limit` parameters without upper-bound validation. The audit endpoint correctly uses `Query(ge=1, le=500)` — adopt this pattern everywhere. Add `ge=1, le=<reasonable_max>` constraints to all `limit` and `offset` parameters.

**Why:** Unbounded limits are a DoS vector. A single request with `limit=999999999` can exhaust database and server memory.

---

### AUDIT-012: Add production-mode check for default database password
**Category:** Security
**Size:** XS
**Files:** `packages/common/src/memex_common/config.py`

**Description:** `config.py:761` sets `password=SecretStr('postgres')` as the default `PostgresInstanceConfig` password. The default is acceptable for local development, but production deployments should not inherit it. Add a production-mode check: when a production indicator is set (e.g., `MEMEX_ENV=production` or a config flag), fail at startup if the password is still the default. Emit a warning in dev mode.

**Why:** Default credentials are a leading cause of database breaches. Any deployment guide that omits password configuration silently inherits `postgres`.

---

### AUDIT-013: Restore MCP error logging
**Category:** Security
**Size:** XS
**Files:** `packages/mcp/src/memex_mcp/server.py`

**Description:** FastMCP server is initialized with `log_level='CRITICAL'` (server.py:128), suppressing all error and warning logs. Change to a configurable log level (default `WARNING`) controlled via `MEMEX_MCP_LOG_LEVEL` environment variable.

**Why:** Silent failures in the MCP layer mean LLM tool calls fail with no diagnostic trail. MCP is the primary LLM integration surface where failures are most likely and hardest to reproduce.

---

### AUDIT-014: Audit endpoint access control
**Category:** Security
**Size:** S
**Files:** `packages/core/src/memex_core/server/audit.py`

**Description:** The audit endpoint at `/api/v1/admin/audit` relies entirely on global auth middleware. When auth is disabled (the default), the audit log is freely readable. Require auth for `/admin/*` routes regardless of global setting, or add a separate router with its own auth dependency.

**Why:** Audit logs contain sensitive operational data (actor identifiers, API key prefixes, request paths). Exposing them without authentication aids reconnaissance.

---

### AUDIT-015: Dead webhooks module — register or remove
**Category:** Architecture
**Size:** S
**Files:** `packages/core/src/memex_core/server/webhooks.py`, `packages/core/src/memex_core/server/__init__.py`

**Description:** `server/webhooks.py` contains a complete CRUD router with DTOs and validation, but `server/__init__.py` never registers it and `webhook_service` is never initialized in the lifespan. This is dead code that will silently break as surrounding code evolves. Either register the router behind a config flag (preferred, since the code is already written) or remove the module entirely.

**Why:** Dead code that references uninitialized state misleads developers into thinking webhooks are functional. The module accumulates maintenance cost from refactors it participates in but cannot serve.

---

### AUDIT-016: O(N) DB roundtrips in reindex_blocks and batch job race condition
**Category:** Architecture
**Size:** M
**Files:** `packages/core/src/memex_core/memory/extraction/storage.py`, `packages/core/src/memex_core/processing/batch.py`

**Description:** Two issues: (1) `reindex_blocks` (storage.py:522-524) issues one `UPDATE chunks SET chunk_index = ? WHERE id = ?` per block — 100 sequential roundtrips for a 100-chunk note. Batch into a single `CASE WHEN` UPDATE or `executemany`. (2) Batch job status updates (batch.py:135-142) perform read-modify-write cycles across separate sessions with no optimistic locking. Add a `version` column for optimistic locking or use `SELECT ... FOR UPDATE`.

**Why:** Reindex runs on every incremental re-ingestion and latency is noticeable on large notes. The batch race condition, while rare (single-writer by design), can result in a job stuck in PROCESSING forever after a server restart during reconciliation.

---

### AUDIT-017: Redundant lineage endpoints
**Category:** Architecture
**Size:** S
**Files:** `packages/core/src/memex_core/server/entities.py`, `packages/core/src/memex_core/server/resources.py`

**Description:** Three endpoints all call `api.get_lineage()`: entity-specific (hardcodes `entity_type='mental_model'`), note-specific (hardcodes `entity_type='note'`), and generic (accepts any type). The generic endpoint is a strict superset. Deprecate the specific endpoints with response headers and remove after one release cycle.

**Why:** Each endpoint is an independent maintenance target. The entity endpoint has a bug where it hardcodes `entity_type='mental_model'` instead of inferring from the actual entity. When lineage logic changes, all three must be updated in lockstep.

---

### AUDIT-018: Extract shared MemoryUnitDTO builder in server layer
**Category:** Code Quality
**Size:** S
**Files:** `packages/core/src/memex_core/server/retrieval.py`, `packages/core/src/memex_core/server/memories.py`, `packages/core/src/memex_core/server/entities.py`

**Description:** Three server files independently construct `MemoryUnitDTO` from `MemoryUnit` models with slight variations. Extract a shared `build_memory_unit_dto()` helper into `server/common.py` and call it from all three endpoints.

**Why:** Adding a field to `MemoryUnitDTO` requires finding and updating three separate builder sites. Drift between them causes inconsistent API responses.

---

### AUDIT-019: Deduplicate vault resolution logic across MCP and client
**Category:** Code Quality
**Size:** M
**Files:** `packages/mcp/src/memex_mcp/server.py`, `packages/common/src/memex_common/client.py`, `packages/core/src/memex_core/server/vaults.py`

**Description:** *Scope reduced by vault config refactor (commits `b73e06c`–`21a3cc4`).* MCP now has `_default_write_vault()` and `_default_read_vaults()` helpers and vault params are optional with defaults, reducing some duplication. However, the vault ID list assembly pattern (`ids = list(vault_ids) if vault_ids else []; if vault_id and vault_id not in ids: ids.append(vault_id)`) is still repeated ~7 times in `client.py`. Extract a shared `_resolve_vault_list()` utility in `memex_common` and consolidate remaining client-side duplication.

**Why:** The client-side vault list assembly is still duplicated across 7+ methods. A change to vault resolution semantics requires updating each site individually, risking inconsistency.

---

### AUDIT-020: Consolidate duplicate date/datetime parsing functions
**Category:** Code Quality
**Size:** S
**Files:** `packages/core/src/memex_core/processing/files.py`, `packages/core/src/memex_core/processing/web.py`, `packages/core/src/memex_core/processing/dates.py`

**Description:** Three redundant date parsing implementations exist with subtly different behavior (timezone handling, format precedence). The CLI also has its own DateTime parsing. Consolidate into a single `parse_datetime()` in `processing/dates.py` and update all callers. Note: sequence this before T5 (Dateparser Temporal Extraction) to avoid building on fragmented date parsing.

**Why:** Subtle differences across modules cause silent data inconsistencies. When a bug is found in one parser, the other two are typically forgotten.

---

### AUDIT-021: CLI output layer cleanup — remove debug print and centralize formatting
**Category:** Code Quality
**Size:** M
**Files:** `packages/cli/src/memex_cli/__init__.py`, `packages/cli/src/memex_cli/` (multiple files)

**Description:** Three issues: (1) A debug `print()` statement at `__init__.py:190` leaks to stdout. (2) Mixed `console.print()` and bare `print()` across ~16 files (~293 calls). (3) Output formatting logic duplicated across ~19 locations. Fix: remove the debug print, standardize on Rich `console.print()`, extract shared formatters.

**Why:** The debug print is visible to users. Mixed print/console.print means some output ignores `--quiet`/`--no-color` flags. Duplicated formatting causes visual inconsistencies.

---

### AUDIT-022: Clean up server endpoint inconsistencies — base64 dedup and unreachable reflection code
**Category:** Code Quality
**Size:** M
**Files:** `packages/core/src/memex_core/server/ingestion.py`, `packages/core/src/memex_core/server/reflection.py`

**Description:** Duplicate Base64 decoding logic in `ingestion.py` — extract a shared `_decode_base64_dict()` helper. Note: lineage endpoint duplication is tracked in AUDIT-017; 202 response inconsistency is tracked in AUDIT-006; unreachable reflection code is tracked in AUDIT-040.

**Why:** Duplicate decoding diverges over time. Different error messages and handling between the two sites create inconsistent client-facing behavior.

---

### AUDIT-023: Fix inconsistent MCP supersession metadata and return types
**Category:** Code Quality
**Size:** S
**Files:** `packages/mcp/src/memex_mcp/server.py`

**Description:** Supersession metadata handling differs between `server.py:697` and `server.py:1632` — one path sets it, the other does not. Additionally, `memex_get_page_indices` returns `str | PageIndexDTO` depending on code path. Standardize supersession handling and make `memex_get_page_indices` always return a consistent type.

**Why:** Inconsistent supersession metadata breaks note version history traversal and could cause reflection to process stale data. Inconsistent return types force LLM callers to handle two shapes.

---

### AUDIT-024: Deduplicate entity resolution logic across CLI and core
**Category:** Code Quality
**Size:** S
**Files:** `packages/cli/src/memex_cli/`, `packages/core/src/memex_core/memory/`

**Description:** Entity resolution logic exists in both CLI and core packages. The CLI should delegate entirely to core's entity resolver rather than reimplementing matching/deduplication logic.

**Why:** If the CLI resolves entities differently than the API/MCP server, users get different results depending on how they interact with Memex. Especially confusing for entity merging and search.

---

### AUDIT-025: Remove dead DuckDB module
**Category:** Code Quality
**Size:** XS
**Files:** `packages/core/src/memex_core/duckdb.py`

**Description:** `duckdb.py` is unused dead code. The project uses PostgreSQL+pgvector exclusively. Remove the file and any residual imports.

**Why:** Creates a false impression that DuckDB is a supported storage backend. Accumulates linting debt and confuses contributors evaluating the architecture.

---

### AUDIT-026: Fix fixture leak in test_server_upload.py
**Category:** Testing
**Size:** XS
**Files:** `tests/test_server_upload.py`

**Description:** Module-level `mock_api = MagicMock()` accumulates call state across tests. The `client` fixture sets `app.dependency_overrides[get_api]` but never cleans it up. Add teardown to clear overrides and reset `mock_api` before each test.

**Why:** Module-level mutable mock state is a classic source of order-dependent test flakiness. Will surface as intermittent CI failures as the test suite grows.

---

### ~~AUDIT-027: Implement tests in empty test_memory_cli.py~~ RESOLVED
> Resolved by commit `a27aec0` — file now has 126 lines of real tests covering `add`, `search`, `list`, and error cases.

---

### AUDIT-028: Replace MockAsyncClientContext anti-pattern in test_e2e_cli.py
**Category:** Testing
**Size:** M
**Files:** `tests/test_e2e_cli.py`

**Description:** A 60+ line `MockAsyncClientContext` class manually reconstructs `MemexAPI` initialization, meaning the test validates the mock, not the actual CLI-to-server integration. Replace with the existing `client` or `async_client` fixture, or mock at the HTTP client level (e.g., `respx`).

**Why:** The mock has already diverged from production bootstrap (manually calls `get_embedding_model()` etc.). Every server change risks widening this gap silently. The test gives false confidence.

---

### AUDIT-029: Strengthen test_metadata_flow.py assertions
**Category:** Testing
**Size:** S
**Files:** `tests/test_metadata_flow.py`

**Description:** Uses fragile positional arg unpacking (lines 54-57) that breaks if `retain()` signature changes. Also only verifies `source_uri`, not other payload fields. Switch to keyword-only assertions (`call_args.kwargs['contents']`) and add assertions for all payload fields.

**Why:** The `retain()` method signature has already changed once. Future changes will silently break this test's arg unpacking rather than producing a clear failure.

---

### AUDIT-030: Verify create_task scheduling in test_batch_manager.py
**Category:** Testing
**Size:** XS
**Files:** `tests/test_batch_manager.py`

**Description:** The test patches `_run_job` but never asserts that the background task was actually scheduled. The test's own comments document this gap. Assert that `_run_job` was called with expected arguments or patch `asyncio.create_task` directly. Remove the dead comments.

**Why:** Background task scheduling is the core purpose of `create_job`. If it stops scheduling background work, tests would still pass.

---

### AUDIT-031: Add missing error path and edge case tests
**Category:** Testing
**Size:** L
**Files:** Multiple test files across `packages/core/tests/` and `tests/`

**Description:** Three sub-items:
1. **Formatting + summary error paths:** Empty string input, None fact_type, None date in `format_for_reranking`; empty texts list, None query, invalid LLM summary in `test_summary.py`.
2. **Extraction + retrieval edge cases:** LLM extraction failures, malformed output, budget exhaustion mid-retrieval.
3. **Vault isolation tests:** Verify queries scoped to vault A cannot return results from vault B. This is a data isolation and security-adjacent concern.

**Why:** Production systems fail in error paths, not happy paths. Vault isolation gaps are a data correctness risk. The current suite validates that things work when everything goes right but provides no confidence about degraded behavior.

---

### AUDIT-032: Add test coverage for eval framework
**Category:** Testing
**Size:** XL
**Files:** `packages/eval/src/memex_eval/`

**Description:** The entire `packages/eval/` package has zero test coverage. Needs tests for scenario generation, check logic (exact match, semantic similarity, LLM judge), report generation, and edge cases (malformed scenarios, LLM judge failures).

**Why:** The eval framework is the quality gate for the entire memory system. A bug in check logic could make a regression look like an improvement. Without tests, changes to scoring logic could silently change what "0.958 accuracy" means.

---

### AUDIT-033: Test infrastructure hygiene sweep
**Category:** Testing
**Size:** M
**Files:** `tests/conftest.py`, multiple test files

**Description:** Multiple cross-cutting issues: (1) Inconsistent mock naming (`mock_api` vs `api_mock` vs `MockMemexAPI`). (2) Inconsistent embedding mock types (numpy arrays vs plain lists). (3) Commented-out test code in `test_cli_utils.py:54-55`. (4) Hard-coded threshold magic numbers. (5) Misleading test name (`test_list_vaults` is actually a health check). (6) Missing `@pytest.mark.parametrize` in repetitive tests.

Additionally, a fixture scope mismatch in E2E conftest deserves separate attention: `init_db` is session-scoped and creates the global vault once, but if a test truncates tables without `db_session`, the vault may be missing. This is a latent ordering bug that should be treated as a medium-priority sub-item.

**Why:** Test infrastructure debt compounds. Mocks silently diverge from production, failures are hard to diagnose, and new contributors copy bad patterns.

---

### AUDIT-038: Dependency version skew — dspy, platformdirs, and python-box
**Category:** Architecture
**Size:** S
**Files:** `packages/core/pyproject.toml`, `packages/eval/pyproject.toml`, `packages/cli/pyproject.toml`, `packages/common/pyproject.toml`

**Description:** Three conflicts: (1) `dspy`: core requires `>=3.1.0`, eval requires `>=2.6` — incompatible major versions with breaking API changes. (2) `platformdirs`: CLI requires `>=4.2.0`, common requires `>=4.5.1` — CLI's lower bound is meaningless since it depends on common. (3) `python-box`: CLI requires `>=7.1.1`, common requires `>=7.3.2` — same issue. Align all shared dependencies to consistent ranges. For dspy, upgrade eval to 3.x.

**Why:** The dspy major version mismatch means core and eval cannot coexist in the same environment. A `uv lock --upgrade` could resolve to incompatible versions, breaking eval or CLI.

---

## P3 — Low

### AUDIT-034: Remove empty callback functions and centralize CLI option patterns
**Category:** Code Quality
**Size:** S
**Files:** `packages/cli/src/memex_cli/config.py`, `packages/cli/src/memex_cli/setup_claude_code.py`

**Description:** Empty Typer callback stubs at `config.py:31` and `setup_claude_code.py:32` do nothing. Additionally, ~5+ commands duplicate the same CLI option patterns (vault, output format, limit). Extract shared option decorators or a common options factory.

**Why:** Empty callbacks confuse future developers. Duplicated option patterns mean adding a global option (e.g., `--profile`) requires touching every command.

---

### AUDIT-035: Fix fragile DSN string manipulation in scheduler
**Category:** Code Quality
**Size:** XS
**Files:** `packages/core/src/memex_core/scheduler.py`

**Description:** Lines 71-73 use string manipulation to modify the database DSN. This breaks on DSNs with query parameters, IPv6 hosts, or non-standard formats. Replace with `sqlalchemy.engine.url.make_url()` for proper URL manipulation.

**Why:** Any user with a non-trivial DSN (connection pooler, SSL params, IPv6) will hit a hard-to-debug scheduler failure. The fix is trivial.

---

### AUDIT-036: Deduplicate eval extraction wait logic and clean up eval code
**Category:** Code Quality
**Size:** S
**Files:** `packages/eval/src/memex_eval/runner.py`, `packages/eval/src/memex_eval/locomo_ingest.py`, `packages/eval/src/memex_eval/checks.py`

**Description:** Extraction wait logic duplicated between `runner.py` and `locomo_ingest.py`. Check function dispatch uses if/elif chain instead of dispatch table. Hardcoded excluded question IDs should be configurable. Extract shared wait helper, use dispatch dict, move exclusions to config.

**Why:** The eval framework is actively developed. Duplication slows iteration when adding new eval checks or ingestion methods.

---

### AUDIT-037: Remove TODO comments without issue numbers
**Category:** Code Quality
**Size:** S
**Files:** Codebase-wide

**Description:** Audit for TODO comments lacking tracking issue numbers. Create issues for actionable TODOs and add references, or remove stale TODOs that will never be addressed.

**Why:** TODOs without tracking become invisible debt. Over time developers learn to ignore all TODOs, including important ones.

---

### AUDIT-039: Establish structlog adoption policy — lint rule to prevent stdlib logging spread
**Category:** Architecture
**Size:** XS
**Files:** `packages/core/src/memex_core/logging_config.py`, ruff/linting configuration

**Description:** structlog is configured in `logging_config.py` but only 3-4 files use it; ~91 files use `logging.getLogger()`. The current structlog-wrapping-stdlib config works, so no bulk migration is needed. Add a ruff rule or linting check that flags new `logging.getLogger()` calls. For new code, use `structlog.get_logger()`. Convert existing files opportunistically.

**Why:** Two logging systems means inconsistent output (some structured JSON, most plain text). The XS policy fix prevents the gap from widening without requiring a large migration.

---

### AUDIT-040: Prune unused schema fields and superfluous MCP tools
**Category:** Architecture
**Size:** S
**Files:** `packages/common/src/memex_common/`, `packages/mcp/src/memex_mcp/server.py`, `packages/core/src/memex_core/server/reflection.py`

**Description:** Two cleanup items: (1) `NoteMetadata` has unused fields (`embedding`, `etag`, `project`) that are never populated — remove or deprecate. (2) `memex_active_vault` MCP tool provides the only way to determine the active vault (since `VaultDTO` has no `is_active` field) — add an `is_active` flag to `VaultDTO` in `memex_list_vaults` output, then deprecate `memex_active_vault`. Note: unreachable reflection code is tracked in AUDIT-022.

**Why:** Every unused `NoteMetadata` field consumes tokens when LLMs process API responses. Consolidating vault tools reduces MCP tool count and context window cost (~200 tokens/session).
