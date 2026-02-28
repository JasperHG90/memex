# Memex — Refined Backlog

> **Generated:** 2026-02-28 by a 6-engineer staff review team.
> Each task has been verified against the actual codebase with exact file paths and line numbers.
> RFCs for large items are in `.temp/rfcs/`.

---

## Priority Tiers

| Tier | Criteria | Target |
|------|----------|--------|
| **P0** | Critical fixes & quick wins (High severity, S effort, score >= 2.0) | This sprint |
| **P1** | Important improvements (High severity, M effort — need RFCs) | Next 1-2 months |
| **P2** | Strategic enhancements (Medium severity, M effort) | 3-6 months |
| **P3** | Long-term vision (L/XL effort, integrations, architecture) | 6+ months |

---

## P0 — Critical Fixes & Quick Wins

### P0-01: Fix Path Traversal Vulnerability in FileStore
| | |
|---|---|
| **ID** | S9 |
| **Severity** | High |
| **Effort** | S |
| **Priority Score** | 3.00 |
| **Category** | Security |

**Problem:** `BaseAsyncFileStore.join_path()` in `packages/core/src/memex_core/storage/filestore.py:37-49` uses f-string concatenation with no path traversal guard. A key like `../../etc/passwd` resolves outside the root directory.

**Attack surface:**
- `GET /api/v1/resources/{path:path}` — passes user-supplied path to `filestore.load(key)`
- `POST /api/v1/ingestions/file` — accepts arbitrary `file_path` parameter

**Fix:** Add `os.path.realpath` validation in `join_path()`. Reject any resolved path not under root.

**Acceptance Criteria:**
- [ ] `join_path()` validates resolved path is under root via `os.path.realpath`
- [ ] `ValueError` raised for traversal attempts (`../../etc/passwd`, `..`, `foo/../../../etc`)
- [ ] Unit tests for normal paths, `..` traversal, URL-encoded traversal, absolute paths
- [ ] Use `root_normalized` property from `LocalFileStoreConfig` (`config.py:131-133`)

---

### P0-02: Fix Silent Exception Swallowing (15 Bare Blocks)
| | |
|---|---|
| **ID** | E8 |
| **Severity** | High |
| **Effort** | S |
| **Priority Score** | 3.00 |
| **Category** | Error Handling |

**Problem:** 15 bare `except Exception:` blocks across the codebase. Worst: `extraction/core.py:1059-1060` has `except Exception: pass`.

**Truly silent (4 blocks):**
1. `extraction/core.py:1059` — `except Exception: pass` (regex match)
2. `extraction/utils.py:36,194` — return None / set False silently
3. `retrieval/engine.py:56,61` — reranker/ner_model silently fall back to None

**Overly broad (11 blocks):** `api.py:267,580,870`, `processing/dates.py:93`, `processing/titles.py:140`, `server/vaults.py:77`, `reflect/reflection.py:101`, `memory/engine.py:326`, `retrieval/document_search.py:208,285`

**Fix:** Add logging to silent blocks. Narrow exception types on broad blocks (e.g., `yaml.YAMLError`, `UnicodeDecodeError`, `dspy.DSPyError`).

**Acceptance Criteria:**
- [ ] Zero `except Exception: pass` patterns remain
- [ ] All `except Exception:` blocks have at least `logger.debug()` call
- [ ] Behavior unchanged — only observability improves
- [ ] Existing tests pass

---

### P0-03: Extract Hardcoded Thresholds to RetrievalConfig
| | |
|---|---|
| **ID** | CQ8 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Code Quality |

**Problem:** Magic numbers duplicated across retrieval code. `0.3` (similarity threshold) appears 8 times in `strategies.py`. `30.0`/`2.0` (temporal decay) appear in 2 places. `60` (RRF constant) in `engine.py`.

**Files:** `packages/common/src/memex_common/config.py:419` (RetrievalConfig), `packages/core/src/memex_core/memory/retrieval/strategies.py`, `packages/core/src/memex_core/memory/retrieval/engine.py:36-37`

**Fix:** Add `similarity_threshold`, `temporal_decay_days`, `temporal_decay_base`, `rrf_k`, `candidate_pool_size` to `RetrievalConfig`. Thread config into strategy constructors.

**Acceptance Criteria:**
- [ ] All 8 occurrences of `0.3` replaced with config value
- [ ] Temporal decay formula uses config values
- [ ] `K_RRF` and `CANDIDATE_POOL_SIZE` sourced from config
- [ ] Default values match current hardcoded values (no behavioral change)
- [ ] New unit test verifies custom config propagation

---

### P0-04: Configure statement_timeout for Vector Searches
| | |
|---|---|
| **ID** | D5 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Database |

**Problem:** No `statement_timeout` in `metastore.py:90-102` engine creation. Slow vector searches can hold connections indefinitely.

**Fix:** Add `statement_timeout: '30000'` to `connect_args['server_settings']`. Add configurable field in `PostgresMetaStoreConfig` (`config.py:173-192`).

**Acceptance Criteria:**
- [ ] Global `statement_timeout` set via `connect_args`
- [ ] Timeout configurable in `PostgresMetaStoreConfig`
- [ ] `QueryCanceledError` handled gracefully in error handlers
- [ ] Existing tests unaffected (30s is generous)

**Gotcha:** Ensure Alembic migrations (if added) use a longer timeout.

---

### P0-05: Add Health Check Endpoints
| | |
|---|---|
| **ID** | E4 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Observability |

**Problem:** No `/health` or `/ready` endpoints. No way for load balancers or orchestrators to check server health.

**Fix:** New `packages/core/src/memex_core/server/health.py` with liveness (`GET /api/v1/health` — returns 200) and readiness (`GET /api/v1/ready` — checks DB via `SELECT 1`).

**Acceptance Criteria:**
- [ ] `GET /api/v1/health` returns `200 {"status": "ok"}`
- [ ] `GET /api/v1/ready` returns `200` when DB reachable, `503` when not
- [ ] Both exempt from future auth middleware
- [ ] Unit + E2E tests

---

### P0-06: Add Rate Limiting Middleware
| | |
|---|---|
| **ID** | S2 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Security |

**Problem:** No rate limiting. Server binds localhost by default (mitigated), but production deployment needs protection.

**Fix:** Add `slowapi` middleware to `server/__init__.py`. Disabled by default. Configurable limits: ingestion 10/min, search 60/min, batch 5/min. Health/metrics exempt.

**Acceptance Criteria:**
- [ ] `slowapi` added as dependency (requires user permission)
- [ ] Rate limiting configurable and disabled by default
- [ ] 429 responses include `Retry-After` header
- [ ] Health/metrics endpoints exempt

---

### P0-07: Fix btoa() Non-ASCII Bug in Quick Note
| | |
|---|---|
| **ID** | U14 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | UX/UI |

**Problem:** `packages/dashboard/src/components/quick-note-modal.tsx:37` uses `btoa(content)` which throws `DOMException` on non-ASCII characters (accented letters, CJK, emoji). User's note is silently lost.

**Fix:** Create `encodeBase64()` utility in `packages/dashboard/src/lib/utils.ts` using `TextEncoder` approach. Compatible with server-side `base64.b64decode(content).decode('utf-8')`.

**Acceptance Criteria:**
- [ ] Non-ASCII notes save successfully
- [ ] Utility function in `lib/utils.ts` is reusable
- [ ] No regressions for ASCII-only notes

---

### P0-08: Add React Error Boundary
| | |
|---|---|
| **ID** | TS7 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** No Error Boundary anywhere in the dashboard. A rendering error in any component white-screens the entire app.

**Fix:** Wrap `<Outlet />` in `app.tsx:25` with a class-based Error Boundary. Reuse existing `error-state.tsx` component. Keep sidebar/nav functional during errors.

**Acceptance Criteria:**
- [ ] Error Boundary wraps main content area
- [ ] Recovery UI with "Reload" button shown on render errors
- [ ] Sidebar remains functional during error state
- [ ] Must be class component (React 19 requirement)

---

### P0-09: Null Guard for NDJSON Stream
| | |
|---|---|
| **ID** | TS2 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/api/ndjson.ts:2` uses `response.body!.getReader()` — non-null assertion. `Response.body` can be null (204, HEAD, opaque responses).

**Fix:** Replace `!` with null check and early return (empty sequence for AsyncGenerator).

**Acceptance Criteria:**
- [ ] `!` non-null assertion removed
- [ ] Graceful empty result when body is null
- [ ] TypeScript strict mode compiles clean

---

### P0-10: Remove Unsafe Double-Cast in API Client
| | |
|---|---|
| **ID** | TS3 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/api/client.ts:41` has `return response as unknown as T` — bypasses type system.

**Fix:** Use function overloads: one overload for `rawResponse: true` returning `Response`, another for normal use returning `T`.

**Acceptance Criteria:**
- [ ] No `as unknown as T` patterns in `client.ts`
- [ ] `api.getRaw()` still returns `Response`
- [ ] TypeScript strict mode compiles clean

---

### P0-11: Fix Mutable Shared Config Race Condition
| | |
|---|---|
| **ID** | TS6 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/openclaw/src/plugin.ts:64-67` mutates shared `cfg` object during concurrent tool calls. Two concurrent `memex_search` calls interfere with each other's `searchLimit`/`tokenBudget`.

**Fix:** Pass override values as parameters to `client.searchMemories()` instead of mutating shared config.

**Acceptance Criteria:**
- [ ] `cfg` object never mutated after construction
- [ ] Concurrent searches with different limits don't interfere
- [ ] Existing openclaw tests pass

---

### P0-12: Fix Vault Store Initialization Side-Effect
| | |
|---|---|
| **ID** | TS10 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/pages/settings.tsx:238-242` calls `store.initialize()` during render, triggering Zustand state update during render cycle. Causes double-invocation in React 19 StrictMode.

**Fix:** Move initialization into `useEffect` with `[defaults, store.isInitialized]` dependency array.

**Acceptance Criteria:**
- [ ] `store.initialize()` not called during render phase
- [ ] No React warnings about state updates during render
- [ ] Initialization idempotent via `isInitialized` guard

---

### P0-13: Fix MCP Dependency Declaration
| | |
|---|---|
| **ID** | A6 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Architecture |

**Problem:** `packages/mcp/pyproject.toml` declares `memex-common` but imports `memex_core` (`models.py:8-12`, `lifespan.py`). Works only because sibling workspace package is installed.

**Fix:** Add `memex-core` to MCP dependencies and `tool.uv.sources`.

**Acceptance Criteria:**
- [ ] `memex-core` in MCP package dependencies + workspace sources
- [ ] `uv.lock` updated
- [ ] MCP tests pass in clean environment

---

### P0-14: Active Vault Indicator in Sidebar
| | |
|---|---|
| **ID** | U17 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | UX/UI |

**Problem:** Active vault only visible on Settings page. Sidebar and header have no vault indicator. `useVaultStore` already has `writerVaultName`.

**Fix:** Add vault name display to `sidebar.tsx` after logo section. Show tooltip when collapsed.

**Acceptance Criteria:**
- [ ] Active vault name visible in sidebar on every page
- [ ] Updates when writer vault changes in Settings
- [ ] Tooltip on collapsed sidebar

**Depends on:** P0-12 (TS10 — vault store init fix)

---

### P0-15: Tighten Pydantic Model Field Requirements
| | |
|---|---|
| **ID** | CQ6 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Code Quality |

**Problem:** `NoteCreateDTO.description` is Optional but API enforces it downstream. `NoteMetadata.name` is Optional but `manifest()` raises ValueError on None.

**Fix:** Make `description` required on `NoteCreateDTO`. Either make `name` required on `NoteMetadata` or add None handling to `manifest()`.

**Acceptance Criteria:**
- [ ] `NoteCreateDTO.description` becomes `str` (not `str | None`)
- [ ] No runtime behavior change for valid inputs
- [ ] MCP, CLI, server tests pass

---

### P0-16: Improve Streaming Endpoint Response Docs
| | |
|---|---|
| **ID** | AP2 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | API Design |

**Problem:** `ndjson_openapi()` in `server/common.py:157-173` produces `type: string` instead of the actual model schema. Affects 11+ streaming endpoints.

**Fix:** Use `model.model_json_schema()` in the NDJSON OpenAPI helper. Add example payloads. Document error line format.

**Acceptance Criteria:**
- [ ] OpenAPI spec shows NDJSON line item structure
- [ ] Dashboard `openapi.json` regenerated
- [ ] Swagger UI renders response model correctly

---

### P0-17: Remove Duplicate Ctrl+K Event Listener
| | |
|---|---|
| **ID** | U18 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | UX/UI |

**Problem:** Ctrl+K registered in both `use-keyboard-shortcuts.ts:24-27` and `command-palette.tsx:42-51`. Double-toggle causes palette to flicker open/closed.

**Fix:** Remove the `useEffect` listener from `command-palette.tsx` (lines 42-51). `useKeyboardShortcuts` is the single source of truth.

---

### P0-18: Fix Inconsistent Logger Naming
| | |
|---|---|
| **ID** | E10 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Observability |

**Problem:** 15+ files use `memex_core.*` (underscore) logger names instead of `memex.*` (dot). Underscore names are NOT children of the `memex` root logger — log level settings don't propagate.

**Fix:** Standardize all to `memex.{package}.{module}` (dot-separated). 15 specific files listed in detailed task descriptions.

**Note:** Subsumable by RFC-005 Phase 2 if structured logging proceeds.

---

### P0-19: Include Session ID in Log Formatter
| | |
|---|---|
| **ID** | E9 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Observability |

**Problem:** Session ID tracked in contextvars (`context.py`) but NOT in log output.

**Fix:** Add `SessionIdFilter` log filter. Update format string at `server/__init__.py:44`.

**Note:** Subsumable by RFC-005 Phase 1 (structlog `merge_contextvars` handles this automatically).

---

### P0-20: Add `__all__` Exports to Top-Level `__init__.py`
| | |
|---|---|
| **ID** | A9 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Architecture |

**Fix:** Add `__all__ = ['MemexAPI', 'NoteInput', 'MemexConfig']` to `memex_core/__init__.py`.

---

### P0-21: Fix Fixture Side Effects Causing Flaky Tests
| | |
|---|---|
| **ID** | T6 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Testing |

**Problem:** `tests/conftest.py` `tmp_env` fixture (line 119) uses `os.environ.clear()` — if test fails mid-execution, environment is corrupted.

**Fix:** Replace with `patch.dict(os.environ, ...)`. Wrap logger teardown in `try/finally`.

---

### P0-22: Snapshot Testing for API Schemas
| | |
|---|---|
| **ID** | T9 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Testing |

**Fix:** Add `syrupy` snapshot test for `/openapi.json`. CI fails on unintentional schema changes.

---

### P0-23: CI Validation for OpenAPI Spec
| | |
|---|---|
| **ID** | AP10 |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | API Design |

**Fix:** Script importing `app.openapi()` to diff against committed spec. GitHub Actions on PRs touching server code.

---

### P0-24: Improve Generic 500 Error Responses with Correlation IDs
| | |
|---|---|
| **ID** | E3 |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Error Handling |

**Problem:** `_handle_error()` in `server/common.py:30-46` returns bare `"Internal server error"` for unhandled exceptions. The session ID middleware already sets `X-Session-ID` via contextvars, but this is not included in error responses. Production debugging requires correlating logs to responses.

**Fix:** Import `get_session_id()` from `context.py`. Include `correlation_id` in 500 error detail. Add custom exception handler to ensure `X-Session-ID` header in error responses.

**Acceptance Criteria:**
- [ ] 500 error responses include a `correlation_id` field
- [ ] `X-Session-ID` header present in error responses
- [ ] Existing exception mapping (404, 400) unchanged
- [ ] Unit tests for error handler with correlation ID

**Note:** CQ3 (Standardize Error Handling) should follow this task.

---

## P1 — Important Improvements (Need RFCs)

### P1-01: Add Authentication/Authorization to FastAPI Server
| | |
|---|---|
| **ID** | S4 |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Security |
| **RFC** | [RFC-004](.temp/rfcs/RFC-004-authentication.md) |

**Summary:** Zero auth on all 10 route modules. API key auth via `X-API-Key` header with middleware. Disabled by default (non-breaking). Warning when binding non-localhost without auth.

**Key decisions (from RFC + reviews):**
- API key auth is the right starting point (not JWT/OAuth2)
- Middleware approach (Option A) protects all endpoints by default
- Exempt health/metrics endpoints
- Support multiple keys for rotation
- Auth framework should be extensible for webhook secrets (per integrations review)

**Blocks:** I7, I4, I1, S10

---

### P1-02: Set Up Alembic Schema Migrations
| | |
|---|---|
| **ID** | D6 |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Database |
| **RFC** | [RFC-003](.temp/rfcs/RFC-003-alembic-migrations.md) |

**Summary:** Replace `create_all` at `metastore.py:133` with Alembic. 3-phase rollout: alongside create_all -> switch default -> remove create_all. Baseline migration + stamp for existing DBs.

**Key decisions (from RFC + reviews):**
- Alembic already a transitive dependency (via optuna)
- CLI integration: `memex db upgrade/downgrade/current/history/stamp`
- Use advisory lock for multi-worker races
- Register `Vector(384)` custom type comparator in `env.py`

**Blocks:** D8, S10

---

### P1-03: Implement Structured Logging
| | |
|---|---|
| **ID** | E1 |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Observability |
| **RFC** | [RFC-005](.temp/rfcs/RFC-005-structured-logging.md) |

**Summary:** Replace plain-text `logging` with `structlog`. JSON for production, console for dev. Auto-includes session ID via `merge_contextvars`. 4-phase migration.

**Subsumes:** E9, E10

---

### P1-04: Standardize Error Handling Patterns
| | |
|---|---|
| **ID** | CQ3 |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Code Quality |

**Summary:** Beyond the 15 bare blocks (P0-02), audit ~90 broad catches. Narrow to specific types. Create error handling guidelines using custom exception hierarchy in `memex_common/exceptions.py`.

**Depends on:** P0-02

---

### P1-05: Decompose MemexAPI God Object
| | |
|---|---|
| **ID** | CQ2 |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |
| **RFC** | [RFC-001](.temp/rfcs/RFC-001-memexapi-decomposition.md) |

**Summary:** `api.py` is 2037 lines, ~60+ methods across 8 domains. Decompose into domain service classes with `MemexAPI` as thin facade. 6 incremental phases starting with Lineage.

**Key decisions (from RFC + reviews):**
- `VaultService` owns resolution + LRU cache; injected via constructor
- `_reflection_lock` lives in `ReflectionService`
- Complete Phases 1-3 before AP6 (webhooks)

---

### P1-06: Decompose Extraction Engine
| | |
|---|---|
| **ID** | CQ7 |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |
| **RFC** | [RFC-002](.temp/rfcs/RFC-002-extraction-engine-decomposition.md) |

**Summary:** `extraction/engine.py` is 1670 lines, functions up to 340 lines. Extract pipeline stages: `diffing.py`, `extraction.py`, `persistence.py`, `tracking.py`. Bottom-up migration in 5 phases.

**Key decisions (from RFC + reviews):**
- All pipeline stages share one DB session
- `_create_links()` / `_create_cross_doc_links()` go to `pipeline/persistence.py` or `pipeline/linking.py`
- Diffing logic ideal for property-based testing (T3)

**Can run in parallel with:** P1-05

---

### P1-07: Dashboard Test Strategy
| | |
|---|---|
| **ID** | TS1/T8 |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | 1.00 |
| **Category** | Testing |
| **RFC** | [RFC-006](.temp/rfcs/RFC-006-dashboard-test-strategy.md) |

**Summary:** Dashboard has zero tests. Vitest + React Testing Library + MSW for unit/component. Playwright for E2E. 3 phases: 40% -> 65% -> 80% coverage.

---

### P1-08: Use Zod Schemas for Runtime Validation
| | |
|---|---|
| **ID** | TS4 |
| **Severity** | Medium |
| **Effort** | M |
| **Priority Score** | 1.00 |
| **Category** | TypeScript |

**Fix:** Add optional `schema` parameter to `apiFetch`. Validate critical hooks first. Expand incrementally.

---

### P1-09: Remove Duplicate Validation Between CLI and Core
| | |
|---|---|
| **ID** | CQ4 |
| **Severity** | Medium |
| **Effort** | M |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |

**Fix:** Remove CLI-specific validation duplicating core logic. Rely on core API exceptions caught by `handle_api_error()`.

---

## P2 — Strategic Enhancements

### P2-01: Retry Counter/DLQ for Reflection Queue
| | |
|---|---|
| **ID** | D8 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Database |

Add `retry_count`, `max_retries`, `last_error` fields to `ReflectionQueue`. `DEAD_LETTER` status. Admin endpoints.

**Depends on:** P1-02 (Alembic)

---

### P2-02: Circuit Breaker for LLM Calls
| | |
|---|---|
| **ID** | A10 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Architecture |

Python `CircuitBreaker` mirroring openclaw's TypeScript implementation. Wrap `run_dspy_operation()`. 5-failure threshold, 60s reset.

---

### P2-03: TEMPR Strategy Debugging Tools
| | |
|---|---|
| **ID** | A4 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Architecture |

Add `debug: bool` to `RetrievalRequest`. Per-result strategy attribution: name, rank, RRF score, timing.

---

### P2-04: Webhook Support for Async Operations
| | |
|---|---|
| **ID** | AP6 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | API Design |

Webhook CRUD endpoints, `WebhookService` with HMAC-SHA256, retry with backoff. Fire on ingestion/reflection completion.

**Depends on:** P1-01 (Auth)

---

### P2-05: Audit Logging
| | |
|---|---|
| **ID** | S10 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Security |

`AuditLog` SQL model, `AuditService`, non-blocking writes, query endpoints.

**Depends on:** P1-01 (Auth), P1-02 (Alembic)

---

### P2-06: Property-Based Testing for Entity Resolution
| | |
|---|---|
| **ID** | T3 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

Add `hypothesis`. Target `_prepare_inputs` (pure function). Properties: idempotency, case-insensitive grouping, conservation.

---

### P2-07: LLM Mocking Strategy for CI
| | |
|---|---|
| **ID** | T4 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

`mock_dspy_lm` fixture with golden outputs. `@pytest.mark.llm_mock` marker. Mock at DSPy layer.

---

### P2-08: Performance Benchmarks for Retrieval Strategies
| | |
|---|---|
| **ID** | T7 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

`pytest-benchmark` with 5+ benchmarks. `just benchmark` command. Baselines for regression detection.

---

### P2-09: Webhook-based Ingestion API
| | |
|---|---|
| **ID** | I7 |
| **Severity** | High |
| **Effort** | Low |
| **Category** | Integration |

`POST /api/v1/ingestions/webhook` accepting plain JSON. Webhook secret validation. Auto-generated `note_key`. High leverage — enables third-party integrations.

**Depends on:** P1-01 (Auth)

---

### P2-10: Alerting Integration
| | |
|---|---|
| **ID** | E7 |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Observability |

Alertmanager rules for Prometheus metrics. Custom app-level metrics.

---

## P3 — Long-Term Vision

### P3-01: GitHub Integration for Auto-Ingesting PRs/Issues
| | |
|---|---|
| **ID** | I2 |
| **Severity** | High |
| **Effort** | L |
| **Category** | Integration |

New `packages/github-integration/` package. GitHub App webhooks -> `NoteInput` with idempotent `note_key`.

**Depends on:** P2-09 (Webhook API)

---

### P3-02: Obsidian Plugin for Bidirectional Sync
| | |
|---|---|
| **ID** | I4 |
| **Severity** | High |
| **Effort** | L |
| **Category** | Integration |

TypeScript Obsidian plugin. `note_key` maps to file paths. `content_hash` for change detection. Reuse openclaw circuit breaker.

**Depends on:** P1-01 (Auth)

---

### P3-03: RAG Pipeline Integration Backend
| | |
|---|---|
| **ID** | I10 |
| **Severity** | High |
| **Effort** | L |
| **Category** | Integration |

`POST /api/v1/rag/retrieve` with formatted context + citations. LangChain + LlamaIndex adapter packages.

---

### P3-04: Event-Driven Architecture
| | |
|---|---|
| **ID** | A5 |
| **Severity** | Medium |
| **Effort** | XL |
| **Category** | Architecture |

Event bus for decoupled pipelines. Requires ADR for technology choice. Current `SKIP LOCKED` is production-grade at current scale.

---

### P3-05: VS Code Extension
| | |
|---|---|
| **ID** | I1 |
| **Severity** | High |
| **Effort** | XL |
| **Category** | Integration |

Memory sidebar, inline lookup, code annotation, quick note creation.

---

### P3-06: OpenTelemetry Distributed Tracing
| | |
|---|---|
| **ID** | E2 |
| **Severity** | Medium |
| **Effort** | L |
| **Category** | Observability |

Full OTel tracing for cross-service visibility. Build on existing session ID correlation.

---

## Dependency Map

```
P0 Quick Wins (parallel)
  │
  ├──> P1-01 Auth (S4) ──> P2-04 Webhooks (AP6)
  │                    ──> P2-05 Audit (S10)
  │                    ──> P2-09 Webhook Ingestion (I7) ──> P3-01 GitHub (I2)
  │                    ──> P3-02 Obsidian (I4)
  │
  ├──> P1-02 Alembic (D6) ──> P2-01 Retry/DLQ (D8)
  │                        ──> P2-05 Audit (S10)
  │
  ├──> P1-03 Structured Logging (E1) [subsumes E9, E10]
  │
  ├──> P1-04 Error Handling (CQ3) [depends on P0-02]
  │
  ├──> P1-05 MemexAPI Decomposition (CQ2) [6 phases]
  │    P1-06 Extraction Decomposition (CQ7) [5 phases, parallel with CQ2]
  │
  └──> P1-07 Dashboard Tests (TS1) ──> enables verification of all TS/U fixes
```

---

## Recommended Implementation Order

Based on cross-RFC review by the principal (rfc-reviewer) engineer:

1. **Start immediately:** RFC-005 (Structured Logging) -- lowest risk, highest standalone value, no dependencies
2. **Start soon:** RFC-003 (Alembic), RFC-006 (Dashboard Tests) -- after minor revisions
3. **Start after revision:** RFC-001 (MemexAPI), RFC-004 (Auth) -- need vault resolution / CORS revisions
4. **Start last:** RFC-002 (Extraction Engine) -- benefits from RFC-001 completing first

### Cross-RFC Dependencies

- **RFC-001 + RFC-005:** Logger naming should be coordinated (defer API logger renaming until after services are extracted)
- **RFC-004 + RFC-006:** Dashboard tests should include auth-aware API mocks once auth is implemented
- **RFC-001 + A6 (MCP dependency fix):** Service extraction makes MCP's dependency issue more visible -- coordinate

---

## RFC Index

| RFC | Title | Status | Readiness | Author | Reviewers |
|-----|-------|--------|-----------|--------|-----------|
| [RFC-001](.temp/rfcs/RFC-001-memexapi-decomposition.md) | Decompose MemexAPI God Object | Approved with minor revisions | Ready after vault resolution revision | Code Quality Engineer | Integrations, Principal |
| [RFC-002](.temp/rfcs/RFC-002-extraction-engine-decomposition.md) | Decompose Extraction Engine | Approved with revisions | Needs session management revision | Code Quality Engineer | Integrations, Principal |
| [RFC-003](.temp/rfcs/RFC-003-alembic-migrations.md) | Alembic Schema Migrations | Approved with minor revisions | Ready after pgvector details | Security & Infra Engineer | Integrations, Principal |
| [RFC-004](.temp/rfcs/RFC-004-authentication.md) | Authentication/Authorization | Approved with revisions | Needs CORS revision | Security & Infra Engineer | Integrations, Principal |
| [RFC-005](.temp/rfcs/RFC-005-structured-logging.md) | Structured Logging | Approved as-is | Ready to proceed immediately | Security & Infra Engineer | Integrations, Principal |
| [RFC-006](.temp/rfcs/RFC-006-dashboard-test-strategy.md) | Dashboard Test Strategy | Approved with minor revisions | Ready after NDJSON mock refinement | Frontend & Testing Engineer | Integrations, Principal |

## Detailed Task Descriptions

Full task descriptions with exact file paths, line numbers, code snippets, and gotchas are in:

- `.temp/rfcs/quick-wins-security-infra.md` — S9, S2, D5, E8, E4, E9, E10
- `.temp/rfcs/TASK-DESCRIPTIONS-code-quality.md` — CQ8, CQ6, CQ3, CQ4, A6, A9, A10, A4, A5
- `.temp/rfcs/TASKS-frontend-testing.md` — TS7, TS2, TS3, TS6, TS10, TS4, U14, U17, U18, T3, T4, T6, T7, T9, AP2
- `.temp/rfcs/TASKS-testing-reliability.md` — T3, T4, T6, T7, T9, D8, E3, CQ3
- `.temp/rfcs/integrations-api-tasks.md` — AP2, AP6, AP10, I2, I4, I10, I1, I7, D8, A10, S10

---

*Generated by backlog-refinement team (6 staff engineers) on 2026-02-28. All findings verified against codebase with exact file paths and line numbers.*
