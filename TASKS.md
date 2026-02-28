# Memex — Refined Backlog

> **Generated:** 2026-02-28 by a 6-engineer staff review team.
> **Last reviewed:** 2026-02-28 against `feat/better-dashboard` (10 commits, 206 files changed).
> Each task has been verified against the actual codebase with exact file paths and line numbers.
> RFCs for large items are in `.temp/rfcs/`.

---

## Changes Since Last Review

- 10 commits on `feat/better-dashboard`, 206 files changed (+7,673 / -7,646)
- Dashboard fully rewritten from Streamlit to React/Vite — all dashboard paths in this file reflect the new stack
- OpenClaw hardened with additional tests (`plugin.test.ts` +412 lines)
- Config refactored for cleaner vault handling
- Entity search bugfix applied; CLI PID generation fix applied
- `api.py` grew from 2,037 to 2,097 lines; `extraction/engine.py` grew from 1,670 to 1,710 lines
- `except Exception` blocks grew from 15 to 107 across 36 files (new server route handlers)
- Logger naming partially migrated: ~27 files now use `memex.*` (dot), ~13 still use `memex_core.*` (underscore)
- `NoteCreateDTO.description` changed from `Optional[str]` to required `str`
- Vault indicator added to sidebar (expanded only — collapsed tooltip still missing)
- Zod v4 installed with 29 auto-generated schemas, but none used for runtime validation yet
- Dashboard has **zero tests** after full rewrite — no vitest, no jest, no test script in `package.json`

### Status Legend

| Symbol | Meaning |
|--------|---------|
| ⬚ | Open — no work started |
| ◧ | Partially Complete — some acceptance criteria met |
| ✅ | Completed — all acceptance criteria met |
| ⚠️ | Stale — scope/description outdated, needs revision |

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
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | S |
| **Priority Score** | 3.00 |
| **Category** | Security |

**Problem:** `BaseAsyncFileStore.join_path()` in `packages/core/src/memex_core/storage/filestore.py:38-49` uses f-string concatenation with no path traversal guard. A key like `../../etc/passwd` resolves outside the root directory.

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

### P0-02: Fix Silent Exception Swallowing
| | |
|---|---|
| **ID** | E8 |
| **Status** | ⚠️ Stale — scope significantly expanded |
| **Severity** | High |
| **Effort** | ~~S~~ **M** (scope grew 7x) |
| **Priority Score** | 3.00 |
| **Category** | Error Handling |

**Problem:** ~~15 bare `except Exception:` blocks across the codebase.~~ **Updated: 107 `except Exception` blocks across 36 files, of which 17 are bare (no `as e`) and silently swallow errors.** The count grew due to new server route handlers using the `_handle_error()` broad-catch pattern.

**Truly silent (17 bare blocks — no `as e`):**
1. `extraction/core.py:1059` — `except Exception: pass` (regex match)
2. `extraction/utils.py:36,194` — return None / set False silently
3. `retrieval/engine.py:56,61` — reranker/ner_model silently fall back to None
4. `extraction/engine.py:1533,1550` — new bare blocks in NER enrichment
5. Plus 11 more across `api.py`, `processing/`, `server/`, `reflect/`, `memory/engine.py`, `retrieval/document_search.py`

**Overly broad (90 blocks):** Spread across all server route modules (`entities.py`, `notes.py`, `resources.py`, `ingestion.py`, `reflection.py`, etc.), plus `api.py:267,580,870`, `processing/dates.py:93`, `processing/titles.py:140`, `server/vaults.py:77`, `reflect/reflection.py:101`, `memory/engine.py:326`, `retrieval/document_search.py:208,285`

**Fix:** Add logging to all silent blocks. Narrow exception types on broad blocks (e.g., `yaml.YAMLError`, `UnicodeDecodeError`, `dspy.DSPyError`). Consider phased approach given increased scope.

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Code Quality |

**Problem:** Magic numbers duplicated across retrieval code. `0.3` (similarity threshold) appears 8 times in `strategies.py` (lines 198, 209, 221, 228, 411, 420, 430, 436). `30.0`/`2.0` (temporal decay) at `strategies.py:255-268,458`. `60` (RRF constant) in `engine.py:36-37`.

**Files:** `packages/common/src/memex_common/config.py:419-431` (RetrievalConfig — currently only has `token_budget` and `retrieval_strategies`), `packages/core/src/memex_core/memory/retrieval/strategies.py`, `packages/core/src/memex_core/memory/retrieval/engine.py:36-37`

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Database |

**Problem:** No `statement_timeout` in `metastore.py:90-102` engine creation. `connect_args` only has `{'server_settings': {'timezone': 'UTC'}}` at line 99-101. Slow vector searches can hold connections indefinitely.

**Fix:** Add `statement_timeout: '30000'` to `connect_args['server_settings']`. Add configurable field in `PostgresMetaStoreConfig` (`config.py:173-192` — currently only has `pool_size` and `max_overflow`).

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Observability |

**Problem:** No `/health` or `/ready` endpoints. No `health.py` file exists in `packages/core/src/memex_core/server/`. No way for load balancers or orchestrators to check server health.

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Security |

**Problem:** No rate limiting. No `slowapi` dependency anywhere in the project. Server binds localhost by default (mitigated), but production deployment needs protection.

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | UX/UI |

**Problem:** `packages/dashboard/src/components/quick-note-modal.tsx:63` uses `btoa(content)` which throws `DOMException` on non-ASCII characters (accented letters, CJK, emoji). User's note is silently lost.

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** No Error Boundary anywhere in the dashboard. A rendering error in any component white-screens the entire app. `<Outlet />` at `app.tsx:26` is unwrapped.

**Fix:** Wrap `<Outlet />` in `app.tsx:26` with a class-based Error Boundary. Reuse existing `packages/dashboard/src/components/shared/error-state.tsx` component. Keep sidebar/nav functional during errors. App is small (33 lines), so the fix is straightforward. React 19 is in use (`"react": "^19.2.0"`).

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/api/ndjson.ts:2` uses `response.body!.getReader()` — non-null assertion. `Response.body` can be null (204, HEAD, opaque responses). File is 25 lines total.

**Fix:** Replace `!` with null check and early return (empty sequence for `AsyncGenerator<T>`).

**Acceptance Criteria:**
- [ ] `!` non-null assertion removed
- [ ] Graceful empty result when body is null
- [ ] TypeScript strict mode compiles clean

---

### P0-10: Remove Unsafe Double-Cast in API Client
| | |
|---|---|
| **ID** | TS3 |
| **Status** | ⚠️ Stale — scope expanded (3 additional sites found) |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/api/client.ts:41` has `return response as unknown as T` — bypasses type system. **Additional instances found:** `timeline.tsx:52` (`data as unknown as MemoryUnitDTO[]`), `lineage-node.tsx:26` (`data as unknown as LineageNodeData`), `memory-search.tsx:82` (`data as unknown as MemoryUnitDTO[]`).

**Fix:** Use function overloads in `client.ts`: one overload for `rawResponse: true` returning `Response`, another for normal use returning `T`. For the page-level casts, fix by having `collectNDJSON` return properly typed data (or use Zod validation per P1-08).

**Acceptance Criteria:**
- [ ] No `as unknown as T` patterns in `client.ts`
- [ ] No `as unknown as T` patterns in page components (`timeline.tsx`, `lineage-node.tsx`, `memory-search.tsx`)
- [ ] `api.getRaw()` still returns `Response`
- [ ] TypeScript strict mode compiles clean

---

### P0-11: Fix Mutable Shared Config Race Condition
| | |
|---|---|
| **ID** | TS6 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/openclaw/src/plugin.ts:69-72` mutates shared `cfg` object during concurrent tool calls (`cfg.searchLimit`, `cfg.tokenBudget`), with try/finally restore at lines 77-78 and 96-97. **Second mutation site found at lines 535-546** (auto-recall search also mutates `cfg.searchLimit`). The save/restore pattern is NOT concurrency-safe.

**Fix:** Pass override values as parameters to `client.searchMemories()` instead of mutating shared config. Both call sites (line 69 and line 535) need fixing.

**Acceptance Criteria:**
- [ ] `cfg` object never mutated after construction
- [ ] Concurrent searches with different limits don't interfere
- [ ] Existing openclaw tests pass

---

### P0-12: Fix Vault Store Initialization Side-Effect
| | |
|---|---|
| **ID** | TS10 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** `packages/dashboard/src/pages/settings.tsx:249-253` calls `store.initialize()` during render in `VaultsTab()`, triggering Zustand state update during render cycle. Causes double-invocation in React 19 StrictMode. The `isInitialized` guard (line 249) prevents infinite loops but doesn't prevent the state update during render. The file has zero `useEffect` imports/usages.

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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Architecture |

**Problem:** `packages/mcp/pyproject.toml` declares `memex-common` (line 11) but `models.py:8-12` imports `memex_core` (`FileStore`, `AsyncPostgresMetaStoreEngine`, `MemexAPI` under TYPE_CHECKING). `tool.uv.sources` only has `memex-common = { workspace = true }`. Works only because sibling workspace package is installed — would fail in isolated install.

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
| **Status** | ◧ Partially Complete |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | UX/UI |

**Problem:** ~~Active vault only visible on Settings page.~~ Vault name now displayed in expanded sidebar at `sidebar.tsx:168` using `useVaultStore` (line 134). **Remaining:** The display is guarded by `!collapsed` (line 161), so it disappears entirely when the sidebar is collapsed. No tooltip implemented.

**Fix:** ~~Add vault name display to `sidebar.tsx` after logo section.~~ Add tooltip when collapsed. Remove the `!collapsed` guard or add a collapsed-state icon+tooltip.

**Acceptance Criteria:**
- [x] Active vault name visible in sidebar on every page
- [x] Updates when writer vault changes in Settings
- [ ] Tooltip on collapsed sidebar

**Depends on:** P0-12 (TS10 — vault store init fix)

---

### P0-15: Tighten Pydantic Model Field Requirements
| | |
|---|---|
| **ID** | CQ6 |
| **Status** | ◧ Partially Complete |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Code Quality |

**Problem:** ~~`NoteCreateDTO.description` is Optional but API enforces it downstream.~~ `NoteCreateDTO.description` is now required `str` at `schemas.py:468-471` (fixed). **Remaining:** `NoteMetadata.name` is still `str | None` at `schemas.py:80-82`, and `manifest()` at `api.py:206-215` still raises `ValueError` on None.

**Fix:** ~~Make `description` required on `NoteCreateDTO`.~~ Either make `name` required on `NoteMetadata` or add None handling to `manifest()`.

**Acceptance Criteria:**
- [x] `NoteCreateDTO.description` becomes `str` (not `str | None`)
- [ ] `NoteMetadata.name` handled (either required or None-safe in `manifest()`)
- [ ] No runtime behavior change for valid inputs
- [ ] MCP, CLI, server tests pass

---

### P0-16: Improve Streaming Endpoint Response Docs
| | |
|---|---|
| **ID** | AP2 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | API Design |

**Problem:** `ndjson_openapi()` in `server/common.py:156-172` produces `type: string` (line 164) instead of the actual model schema. The `model` parameter is passed but only used for `model.__name__` in the description string (line 166). Affects 11+ streaming endpoints across 5 server modules (entities, vaults, reflection, retrieval, notes).

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
| **Status** | ⬚ Open |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | UX/UI |

**Problem:** Ctrl+K registered in both `use-keyboard-shortcuts.ts:24-27` and `command-palette.tsx:95-104`. Both register separate `document.addEventListener('keydown', ...)` handlers. `useKeyboardShortcuts` is invoked in `app.tsx:15` with `onCommandPalette: toggleCommandPalette`. Double-toggle causes palette to flicker open/closed.

**Fix:** Remove the `useEffect` listener from `command-palette.tsx` (lines 95-104). `useKeyboardShortcuts` is the single source of truth.

---

### P0-18: Fix Inconsistent Logger Naming
| | |
|---|---|
| **ID** | E10 |
| **Status** | ⚠️ Stale — partially migrated, count reduced |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Observability |

**Problem:** ~~15+ files use `memex_core.*` (underscore) logger names.~~ **Updated: 13 files still use `memex_core.*` (underscore), ~27 files now use `memex.*` (dot).** A partial migration was done but these 13 were missed. Underscore names are NOT children of the `memex` root logger — `setLevel()` at `server/__init__.py:40-41` does NOT propagate.

**Remaining files with underscore naming:**
`templates.py:13`, `storage/transaction.py:23`, `storage/filestore.py:21`, `memory/extraction/entity_links.py:15`, `memory/utils.py:7`, `memory/extraction/utils.py:19`, `memory/extraction/core.py:56,862`, `memory/entity_resolver.py:109`, `memory/models/base.py:10`, `memory/models/ner.py:15`, `memory/models/reranking.py:12`, `memory/models/embedding.py:11`

**Fix:** Rename remaining 13 files to `memex.{package}.{module}` (dot-separated).

**Note:** Subsumable by RFC-005 Phase 2 if structured logging proceeds.

---

### P0-19: Include Session ID in Log Formatter
| | |
|---|---|
| **ID** | E9 |
| **Status** | ⬚ Open |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Observability |

**Problem:** Session ID tracked in contextvars (`context.py`) and used in middleware at `server/__init__.py:113-120` to set `X-Session-ID` header, but NOT in log output. Log format string is `'%(asctime)s %(name)s %(levelname)s %(message)s'` at `server/__init__.py:44`.

**Fix:** Add `SessionIdFilter` log filter. Update format string at `server/__init__.py:44`.

**Note:** Subsumable by RFC-005 Phase 1 (structlog `merge_contextvars` handles this automatically).

---

### P0-20: Add `__all__` Exports to Top-Level `__init__.py`
| | |
|---|---|
| **ID** | A9 |
| **Status** | ⬚ Open |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Architecture |

**Fix:** Add `__all__ = ['MemexAPI', 'NoteInput', 'MemexConfig']` to `memex_core/__init__.py`. Currently the file only contains `import warnings` and a filter suppression (4 lines).

---

### P0-21: Fix Fixture Side Effects Causing Flaky Tests
| | |
|---|---|
| **ID** | T6 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Testing |

**Problem:** `tests/conftest.py` `tmp_env` fixture (line 119) uses `os.environ.clear()` — if test fails mid-execution between clear and restore, environment is corrupted for subsequent tests. Logger teardown at lines 122-133 is not wrapped in `try/finally`.

**Fix:** Replace with `patch.dict(os.environ, ...)`. Wrap logger teardown in `try/finally`.

---

### P0-22: Snapshot Testing for API Schemas
| | |
|---|---|
| **ID** | T9 |
| **Status** | ⬚ Open |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | Testing |

**Fix:** Add `syrupy` snapshot test for `/openapi.json`. CI fails on unintentional schema changes. No `syrupy` dependency exists in any `pyproject.toml` yet.

---

### P0-23: CI Validation for OpenAPI Spec
| | |
|---|---|
| **ID** | AP10 |
| **Status** | ⬚ Open |
| **Severity** | Low |
| **Effort** | S |
| **Priority Score** | 1.00 |
| **Category** | API Design |

**Fix:** Script importing `app.openapi()` to diff against committed spec. GitHub Actions on PRs touching server code. No CI workflow for this exists yet.

---

### P0-24: Improve Generic 500 Error Responses with Correlation IDs
| | |
|---|---|
| **ID** | E3 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | Error Handling |

**Problem:** `_handle_error()` in `server/common.py:30-46` returns bare `"Internal server error"` (line 46) for unhandled exceptions. The session ID middleware already sets `X-Session-ID` via contextvars, but this is not included in error responses. `get_session_id()` from `context.py` is available but not imported in `common.py`. Production debugging requires correlating logs to responses.

**Fix:** Import `get_session_id()` from `context.py`. Include `correlation_id` in 500 error detail. Add custom exception handler to ensure `X-Session-ID` header in error responses.

**Acceptance Criteria:**
- [ ] 500 error responses include a `correlation_id` field
- [ ] `X-Session-ID` header present in error responses
- [ ] Existing exception mapping (404, 400) unchanged
- [ ] Unit tests for error handler with correlation ID

**Note:** CQ3 (Standardize Error Handling) should follow this task.

---

## P0-NEW — New Issues Surfaced by Dashboard Rewrite

### P0-25: Additional `as unknown as T` Casts in Page Components
| | |
|---|---|
| **ID** | TS11 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | S |
| **Priority Score** | 2.00 |
| **Category** | TypeScript |

**Problem:** 3 unsafe double-cast patterns outside `client.ts` (tracked separately from P0-10 since the fix strategy differs):
- `packages/dashboard/src/pages/timeline.tsx:52` — `data as unknown as MemoryUnitDTO[]`
- `packages/dashboard/src/pages/lineage/lineage-node.tsx:26` — `data as unknown as LineageNodeData`
- `packages/dashboard/src/pages/memory-search.tsx:82` — `data as unknown as MemoryUnitDTO[]`

**Fix:** Have `collectNDJSON` return properly typed data, or apply Zod runtime validation (per P1-08). Can also be folded into P0-10 if addressed together.

**Acceptance Criteria:**
- [ ] No `as unknown as T` patterns in page components
- [ ] TypeScript strict mode compiles clean

**Related:** P0-10, P1-08

---

### P0-26: Dashboard Has Zero Test Infrastructure
| | |
|---|---|
| **ID** | TS12 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | S |
| **Priority Score** | 3.00 |
| **Category** | Testing |

**Problem:** After the Streamlit-to-React rewrite, the dashboard has zero tests and zero test infrastructure. No vitest config, no jest config, no `@testing-library` dependency. `package.json` has no `test` script. Dev dependencies include only eslint, TypeScript, and Vite. This blocks verification of all TS/UI fixes (P0-07 through P0-14, P0-17).

**Fix:** Add vitest + `@testing-library/react` as dev dependencies. Create `vitest.config.ts`. Add `test` script to `package.json`. This is a prerequisite for P1-07 (full test strategy) but can be done as a quick win to unblock other work.

**Acceptance Criteria:**
- [ ] `vitest` and `@testing-library/react` in devDependencies
- [ ] `vitest.config.ts` exists with basic setup
- [ ] `npm run test` (or `pnpm test`) works (even with 0 test files)
- [ ] At least 1 smoke test proving the setup works

**Blocks:** P1-07 (Dashboard Test Strategy)

---

## P1 — Important Improvements (Need RFCs)

### P1-01: Add Authentication/Authorization to FastAPI Server
| | |
|---|---|
| **ID** | S4 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Security |
| **RFC** | [RFC-004](.temp/rfcs/RFC-004-authentication.md) |

**Summary:** Zero auth on all 10 route modules. No authentication middleware in `server/`. API key auth via `X-API-Key` header with middleware. Disabled by default (non-breaking). Warning when binding non-localhost without auth.

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
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Database |
| **RFC** | [RFC-003](.temp/rfcs/RFC-003-alembic-migrations.md) |

**Summary:** Replace `create_all` at `metastore.py:133` (`await conn.run_sync(SQLModel.metadata.create_all)`) with Alembic. No `alembic.ini`, no `alembic/` directory, no migrations directory exists. RFC-003 is still Draft status. 3-phase rollout: alongside create_all -> switch default -> remove create_all. Baseline migration + stamp for existing DBs.

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
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Observability |
| **RFC** | [RFC-005](.temp/rfcs/RFC-005-structured-logging.md) |

**Summary:** Replace plain-text `logging` with `structlog`. No `structlog` dependency exists yet. JSON for production, console for dev. Auto-includes session ID via `merge_contextvars`. 4-phase migration.

**Subsumes:** E9 (P0-19), E10 (P0-18)

---

### P1-04: Standardize Error Handling Patterns
| | |
|---|---|
| **ID** | CQ3 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | M |
| **Priority Score** | 1.50 |
| **Category** | Code Quality |

**Summary:** Beyond the ~~15~~ **17** bare blocks (P0-02), audit ~~~90~~ **107** broad catches across 36 files. Narrow to specific types. Create error handling guidelines using custom exception hierarchy in `memex_common/exceptions.py`. Scope has grown significantly since original estimate.

**Depends on:** P0-02

---

### P1-05: Decompose MemexAPI God Object
| | |
|---|---|
| **ID** | CQ2 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |
| **RFC** | [RFC-001](.temp/rfcs/RFC-001-memexapi-decomposition.md) |

**Summary:** `api.py` is ~~2037~~ **2,097 lines** (growing), ~60+ methods across 8 domains. Problem is worsening. Decompose into domain service classes with `MemexAPI` as thin facade. 6 incremental phases starting with Lineage.

**Key decisions (from RFC + reviews):**
- `VaultService` owns resolution + LRU cache; injected via constructor
- `_reflection_lock` lives in `ReflectionService`
- Complete Phases 1-3 before AP6 (webhooks)

---

### P1-06: Decompose Extraction Engine
| | |
|---|---|
| **ID** | CQ7 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |
| **RFC** | [RFC-002](.temp/rfcs/RFC-002-extraction-engine-decomposition.md) |

**Summary:** `extraction/engine.py` is ~~1670~~ **1,710 lines** (growing), functions up to 340 lines. New NER enrichment method `_build_ner_type_map()` at line 1527. `extraction/core.py` is 1,372 lines (not previously sized). Extract pipeline stages: `diffing.py`, `extraction.py`, `persistence.py`, `tracking.py`. Bottom-up migration in 5 phases.

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
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | L |
| **Priority Score** | ~~1.00~~ **2.00** (urgency increased) |
| **Category** | Testing |
| **RFC** | [RFC-006](.temp/rfcs/RFC-006-dashboard-test-strategy.md) |

**Summary:** Dashboard has **zero tests** after full Streamlit-to-React rewrite. No vitest config, no jest config, no `@testing-library` dependency, no `test` script in `package.json`. This is the single biggest quality risk — every TS/UI fix (P0-07 through P0-14, P0-17) cannot be verified without test infrastructure. Vitest + React Testing Library + MSW for unit/component. Playwright for E2E. 3 phases: 40% -> 65% -> 80% coverage.

**Urgency note:** RFC-006 is approved with minor revisions and the tech stack (Vitest + Vite) is now even more natural since the dashboard runs on Vite. Fast-track recommended — consider P0-26 as a prerequisite quick win.

**Depends on:** P0-26 (test infrastructure setup)

---

### P1-08: Use Zod Schemas for Runtime Validation
| | |
|---|---|
| **ID** | TS4 |
| **Status** | ◧ Partially Complete (infrastructure only) |
| **Severity** | Medium |
| **Effort** | M |
| **Priority Score** | 1.00 |
| **Category** | TypeScript |

**Summary:** Zod v4 is installed (`"zod": "^4.3.6"` in `package.json`) and 29 schemas are auto-generated via `openapi-zod-client` (see `generate-api` script) in `packages/dashboard/src/api/generated.ts`. **However, none are used for runtime validation.** All API responses are still cast with `as T` or `as unknown as T`. The `apiFetch` function in `client.ts` returns `response.json()` without schema validation.

**Fix:** Add optional `schema` parameter to `apiFetch`. Validate critical hooks first. Expand incrementally. Infrastructure is already in place — this is now primarily a wiring task.

---

### P1-09: Remove Duplicate Validation Between CLI and Core
| | |
|---|---|
| **ID** | CQ4 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Priority Score** | 1.00 |
| **Category** | Code Quality |

**Fix:** Remove CLI-specific validation duplicating core logic. `handle_api_error()` at `packages/cli/src/memex_cli/utils.py:117` is used extensively (33+ call sites across 6 CLI modules). Rely on core API exceptions caught by `handle_api_error()`.

---

## P2 — Strategic Enhancements

### P2-01: Retry Counter/DLQ for Reflection Queue
| | |
|---|---|
| **ID** | D8 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Database |

Add `retry_count`, `max_retries`, `last_error` fields to `ReflectionQueue` (`sql_models.py:1067-1129`). Current model has: `id`, `entity_id`, `vault_id`, `priority_score`, `accumulated_evidence`, `status`, `last_queued_at`. Status enum is `('pending', 'processing', 'failed')` — needs `DEAD_LETTER` status. Admin endpoints.

**Depends on:** P1-02 (Alembic)

---

### P2-02: Circuit Breaker for LLM Calls
| | |
|---|---|
| **ID** | A10 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Architecture |

Python `CircuitBreaker` mirroring openclaw's TypeScript implementation. Wrap `run_dspy_operation()`. 5-failure threshold, 60s reset.

---

### P2-03: TEMPR Strategy Debugging Tools
| | |
|---|---|
| **ID** | A4 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Architecture |

Add `debug: bool` to `RetrievalRequest`. Per-result strategy attribution: name, rank, RRF score, timing.

---

### P2-04: Webhook Support for Async Operations
| | |
|---|---|
| **ID** | AP6 |
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

Add `hypothesis`. Target `_prepare_inputs` (pure function). Properties: idempotency, case-insensitive grouping, conservation.

---

### P2-07: LLM Mocking Strategy for CI
| | |
|---|---|
| **ID** | T4 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

`mock_dspy_lm` fixture with golden outputs. `@pytest.mark.llm_mock` marker. Mock at DSPy layer.

---

### P2-08: Performance Benchmarks for Retrieval Strategies
| | |
|---|---|
| **ID** | T7 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | M |
| **Category** | Testing |

`pytest-benchmark` with 5+ benchmarks. `just benchmark` command. Baselines for regression detection.

---

### P2-09: Webhook-based Ingestion API
| | |
|---|---|
| **ID** | I7 |
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
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
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | L |
| **Category** | Integration |

`POST /api/v1/rag/retrieve` with formatted context + citations. LangChain + LlamaIndex adapter packages.

---

### P3-04: Event-Driven Architecture
| | |
|---|---|
| **ID** | A5 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | XL |
| **Category** | Architecture |

Event bus for decoupled pipelines. Requires ADR for technology choice. Current `SKIP LOCKED` is production-grade at current scale.

---

### P3-05: VS Code Extension
| | |
|---|---|
| **ID** | I1 |
| **Status** | ⬚ Open |
| **Severity** | High |
| **Effort** | XL |
| **Category** | Integration |

Memory sidebar, inline lookup, code annotation, quick note creation.

---

### P3-06: OpenTelemetry Distributed Tracing
| | |
|---|---|
| **ID** | E2 |
| **Status** | ⬚ Open |
| **Severity** | Medium |
| **Effort** | L |
| **Category** | Observability |

Full OTel tracing for cross-service visibility. Build on existing session ID correlation.

---

## Dependency Map

```
P0 Quick Wins (parallel — most OPEN, 2 partial)
  │
  ├── P0-01 Path Traversal (S9)           ⬚ OPEN
  ├── P0-02 Silent Exceptions (E8)        ⚠️ STALE — scope 15→107, effort S→M
  ├── P0-03 Hardcoded Thresholds (CQ8)    ⬚ OPEN
  ├── P0-04 statement_timeout (D5)        ⬚ OPEN — trivial 1-line fix
  ├── P0-05 Health Endpoints (E4)         ⬚ OPEN
  ├── P0-06 Rate Limiting (S2)            ⬚ OPEN
  ├── P0-07 btoa() Bug (U14)              ⬚ OPEN
  ├── P0-08 Error Boundary (TS7)          ⬚ OPEN
  ├── P0-09 NDJSON Null Guard (TS2)       ⬚ OPEN
  ├── P0-10 Double Cast (TS3)             ⚠️ STALE — 3 additional sites found
  ├── P0-11 Mutable Config (TS6)          ⬚ OPEN — 2nd mutation site found (line 535)
  ├── P0-12 Vault Store Init (TS10)       ⬚ OPEN
  │    └──> P0-14 Vault Indicator (U17)   ◧ PARTIAL — expanded works, no collapsed tooltip
  ├── P0-13 MCP Dependency (A6)           ⬚ OPEN
  ├── P0-15 Pydantic Fields (CQ6)         ◧ PARTIAL — description fixed, name still Optional
  ├── P0-16 Streaming Docs (AP2)          ⬚ OPEN
  ├── P0-17 Ctrl+K Duplicate (U18)        ⬚ OPEN
  ├── P0-18 Logger Naming (E10)           ⚠️ STALE — 15→13 files remaining (partial migration)
  ├── P0-19 Session ID Logs (E9)          ⬚ OPEN
  ├── P0-20 __all__ Exports (A9)          ⬚ OPEN — trivial
  ├── P0-21 Fixture Side Effects (T6)     ⬚ OPEN
  ├── P0-22 Snapshot Tests (T9)           ⬚ OPEN
  ├── P0-23 CI OpenAPI (AP10)             ⬚ OPEN
  ├── P0-24 Error Correlation IDs (E3)    ⬚ OPEN
  ├── P0-25 Page Component Casts (TS11)   ⬚ NEW
  └── P0-26 Dashboard Test Infra (TS12)   ⬚ NEW — HIGH PRIORITY quick win
  │
  ├──> P0-26 Test Infra ──> P1-07 Dashboard Tests (TS1)  ⬚ OPEN (urgency INCREASED)
  │                          └──> enables verification of all TS/U fixes
  │
  ├──> P1-01 Auth (S4) ──> P2-04 Webhooks (AP6)
  │                    ──> P2-05 Audit (S10)
  │                    ──> P2-09 Webhook Ingestion (I7) ──> P3-01 GitHub (I2)
  │                    ──> P3-02 Obsidian (I4)
  │
  ├──> P1-02 Alembic (D6) ──> P2-01 Retry/DLQ (D8)
  │                        ──> P2-05 Audit (S10)
  │
  ├──> P1-03 Structured Logging (E1) [subsumes P0-18, P0-19]
  │
  ├──> P0-02 ──> P1-04 Error Handling (CQ3) [scope increased: 107 blocks]
  │
  ├──> P1-05 MemexAPI Decomposition (CQ2) [6 phases, api.py now 2097 lines]
  │    P1-06 Extraction Decomposition (CQ7) [5 phases, parallel with CQ2, engine.py now 1710 lines]
  │
  └──> P1-08 Zod Validation (TS4) [infra exists, needs wiring]
```

---

## Recommended Implementation Order (Updated)

### 1. Immediate Quick Wins (This Sprint)

These have no dependencies and minimal risk:

1. **P0-04: statement_timeout** — literal 1-line change, immediate safety improvement
2. **P0-01: Path Traversal** — critical security vulnerability, small effort
3. **P0-13: MCP Dependency** — 2-line fix in `pyproject.toml`
4. **P0-20: `__all__` Exports** — trivial addition (4-line file)
5. **P0-21: Fixture Side Effects** — replace `os.environ.clear()` with `patch.dict`
6. **P0-15: Pydantic Fields** — finish `NoteMetadata.name` (description already fixed)
7. **P0-26: Dashboard Test Infra** — add vitest + RTL, unblocks P1-07

### 2. Fast-Track (Elevated Priority)

8. **P1-07/RFC-006: Dashboard Tests** — zero test coverage on rewritten dashboard is the single biggest quality risk. Set up Vitest + RTL + MSW before more UI fixes.
9. **RFC-005/P1-03: Structured Logging** — lowest risk RFC, highest standalone value, no dependencies. Subsumes P0-18 and P0-19.

### 3. After Test Infrastructure

10. **P0-07: btoa() Bug** — fix with test
11. **P0-08: Error Boundary** — fix with test
12. **P0-09: NDJSON Null Guard** — fix with test
13. **P0-10 + P0-25: Double Casts** — fix all 4 sites with tests
14. **P0-12: Vault Store Init** — fix with test, then complete P0-14
15. **P0-17: Ctrl+K Duplicate** — fix with test
16. **P1-08: Zod Validation** — wire existing schemas into `apiFetch`

### 4. Parallel Backend Track

17. **P0-02: Silent Exceptions** — scope increased to 107 occurrences, may need phased approach
18. **P0-05: Health Endpoints** — new file, no conflicts
19. **P0-06: Rate Limiting** — after health endpoints
20. **P0-24: Error Correlation IDs** — after P0-02

### 5. Medium Term

21. **RFC-003/P1-02: Alembic** — enables P2-01 (DLQ) and P2-05 (Audit)
22. **RFC-004/P1-01: Auth** — enables webhooks and integrations
23. **RFC-001/P1-05: MemexAPI Decomposition** — after vault resolution revision (api.py at 2097 lines and growing)
24. **RFC-002/P1-06: Extraction Decomposition** — after RFC-001 (engine.py at 1710 lines and growing)

### Cross-RFC Dependencies

- **RFC-001 + RFC-005:** Logger naming should be coordinated (defer API logger renaming until after services are extracted)
- **RFC-004 + RFC-006:** Dashboard tests should include auth-aware API mocks once auth is implemented
- **RFC-001 + A6 (MCP dependency fix):** Service extraction makes MCP's dependency issue more visible — coordinate

---

## RFC Index

| RFC | Title | Status | Readiness | Author | Reviewers |
|-----|-------|--------|-----------|--------|-----------|
| [RFC-001](.temp/rfcs/RFC-001-memexapi-decomposition.md) | Decompose MemexAPI God Object | Approved with minor revisions | Ready after vault resolution revision | Code Quality Engineer | Integrations, Principal |
| [RFC-002](.temp/rfcs/RFC-002-extraction-engine-decomposition.md) | Decompose Extraction Engine | Approved with revisions | Needs session management revision | Code Quality Engineer | Integrations, Principal |
| [RFC-003](.temp/rfcs/RFC-003-alembic-migrations.md) | Alembic Schema Migrations | Draft | Ready after pgvector details | Security & Infra Engineer | Integrations, Principal |
| [RFC-004](.temp/rfcs/RFC-004-authentication.md) | Authentication/Authorization | Approved with revisions | Needs CORS revision | Security & Infra Engineer | Integrations, Principal |
| [RFC-005](.temp/rfcs/RFC-005-structured-logging.md) | Structured Logging | Approved as-is | Ready to proceed immediately | Security & Infra Engineer | Integrations, Principal |
| [RFC-006](.temp/rfcs/RFC-006-dashboard-test-strategy.md) | Dashboard Test Strategy | Approved with minor revisions | **More urgent** — dashboard rewritten with 0 tests | Frontend & Testing Engineer | Integrations, Principal |

## New Dashboard Components (Not Yet in Task Scope)

The React/Vite dashboard rewrite added ~30 new components with zero test coverage. These are tracked by P0-26 (infra) and P1-07 (strategy) but listed here for reference:

**New pages:** `knowledge-flow.tsx`, `timeline.tsx`, `reflection.tsx`, `entity-graph.tsx` (with subcomponents: `entity-search.tsx`, `entity-side-panel.tsx`, `entity-node.tsx`, `entity-types.ts`, `filter-panel.tsx`, `graph-canvas.tsx`), `lineage/` (with `entity-search.tsx`, `lineage-graph.tsx`, `lineage-node.tsx`)

**New shared components:** `advanced-search-panel.tsx`, `connection-banner.tsx`, `detail-modal.tsx`, `format-label.ts`, `loading-button.tsx`, `memory-detail-dialog.tsx`, `metric-card-skeleton.tsx`, `page-index-tree.tsx`, `page-skeleton.tsx`, `page-transition.tsx`, `result-card-skeleton.tsx`, `staggered-list.tsx`, `strategy-filter.tsx`, `summary-card.tsx`, `type-badge.tsx`, `vault-badge.tsx`, `welcome-modal.tsx`, `page-header.tsx`

**New hooks:** `use-animated-number.ts`, `use-connection-status.ts`, `use-debounce.ts`, `use-media-query.ts`

**New stores:** `preferences-store.ts`, `ui-store.ts`, `vault-store.ts`

**New API hooks:** `use-entities.ts`, `use-lineage.ts`, `use-memories.ts`, `use-notes.ts`, `use-reflections.ts`, `use-stats.ts`, `use-summary.ts`, `use-vaults.ts`

## Detailed Task Descriptions

Full task descriptions with exact file paths, line numbers, code snippets, and gotchas are in:

- `.temp/rfcs/quick-wins-security-infra.md` — S9, S2, D5, E8, E4, E9, E10
- `.temp/rfcs/TASK-DESCRIPTIONS-code-quality.md` — CQ8, CQ6, CQ3, CQ4, A6, A9, A10, A4, A5
- `.temp/rfcs/TASKS-frontend-testing.md` — TS7, TS2, TS3, TS6, TS10, TS4, U14, U17, U18, T3, T4, T6, T7, T9, AP2
- `.temp/rfcs/TASKS-testing-reliability.md` — T3, T4, T6, T7, T9, D8, E3, CQ3
- `.temp/rfcs/integrations-api-tasks.md` — AP2, AP6, AP10, I2, I4, I10, I1, I7, D8, A10, S10

---

*Generated by backlog-refinement team (6 staff engineers) on 2026-02-28. Last reviewed 2026-02-28 against `feat/better-dashboard` by 3-agent update team. All findings verified against codebase with exact file paths and line numbers.*
