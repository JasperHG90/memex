# Tier-A Cognitive Memory Branch Review

**Branch:** `release/tier-a-cognitive-memory`
**Date:** 2026-05-03
**Scope:** 8 commits, 241 files, ~34k insertions / ~205 deletions
**Features:** F1a/b/c, F2, F4, F5, F6, F8, F9, F10, F14, F20, F25, F26, F32, F33, F38

---

## Summary

This branch introduces the "cognitive memory" tier: outcome tracking, spaced repetition, deprioritization, lint, consolidation, diagnostics, and write-time classification. It adds 9 migrations, 6 new services, ~12 new MCP tools, 4 new CLI command groups, and ~20 new REST endpoints. The architecture is principled and consistent with the existing Hindsight framework. The core value — closing the feedback loop so facts can be scored as useful or not — is well-realized. The main risks are operational (surface area, naming collisions, test gaps) rather than architectural.

---

## What's Good

### Architecture is principled and consistent

The Hindsight framework's layered pattern (routes → services → engines → storage) is maintained throughout. Each new feature follows the same contract: a service class, SQL models in a migration, audit logging, Prometheus counters, and both CLI + MCP surfaces. The BACKLOG is unusually rigorous — every feature has acceptance criteria, dependency edges, and explicit non-goals.

### Non-destructive by default

- **F4 deprioritization**: `is_deprioritized` is a soft flag. Retrieval excludes deprioritized units by default but surfaces them with a flag. Only `memex_memory_restore` can flip it back.
- **F38 consolidation**: never promotes units to stale — only prunes units already marked stale by other subsystems. Diff-unit selection caps at 500 units/tick.

These are defensible design choices that prevent accidental data loss.

### Retrieval improvements are the most impactful change

The MW (Memory Worth) score composition at the reranker site is clean — Beta-Bernoulli posterior mean with cold-start default of 1.0, composed multiplicatively with recency and temporal proximity. The anisotropy corrector (shared singleton across retrieval/contradiction/dedup) is a smart reuse of a fundamental normalization. The exploration injection (F33, epsilon-greedy) is a pragmatic way to surface cold-start units. These changes directly address the "rich get richer" retrieval problem.

### FSRS-5 revisit (F20) is well-implemented

- 5-gate eligibility predicate (intent, status, deprioritized, confidence, MW)
- Migration 030 fixes a real elapsed-days bug (conflating `revisit_due_at` with last-review timestamp)
- Sticky auto-deprioritize at 5 consecutive "Again" ratings is a sensible decay signal
- Bool-coercion guard on `quality` prevents real Pydantic v2 footguns (`True` → `Quality.AGAIN`)

### Write-time classification (F25) is a strong addition

Classifying facts as permanent/durable/ephemeral at extraction time gives the retrieval layer a principled signal for filtering and decay. Default-on-fail behavior (LLM error → `durable`/`none`, never blocking extraction) is production-grade. The risk class surface (`safety` → refuse, `private` → exclude from default retrieval, `sensitive` → flag for lint) is a reasonable privacy model.

### Consolidation tick (F38) is well-scoped

- Per-entity advisory locks prevent races with manual reconsolidation
- Diff-unit selection (audit log since last tick) avoids full scans
- 500 units/tick cap bounds blast radius
- Single `ConsolidationTick` row as the sole DB write from the orchestrator is clean audit design

---

## Concerns

### 1. API surface sprawl

The branch adds 6 new services, 9 migrations, 12+ new MCP tools, 4 new CLI command groups, and ~20 new REST endpoints. The `MemoryUnitDTO` has grown by 6 fields. The `MemexAPI` facade now has ~40 methods. This is a lot of surface area for a single merge.

**Naming collisions will confuse operators:**
- `memex memory consolidate` (calls `LocksService.consolidate_vault`) vs `memex consolidate tick` (orchestrates the full F38 pipeline: contradiction + reflection + prune). These are different operations sharing the word "consolidate."
- `memex diagnostics lint` (operator dashboard pivot) vs `memex lint status/findings` (human reconciliation surface). Both relate to linting but have different scopes and output formats.

### 2. No concurrent-request tests

Every state-mutating endpoint (outcome recording, review, deprioritize, consolidation tick) modifies shared counters or FSRS-5 state, but there are zero tests for race conditions. The advisory locks in F9 are only documented behavior — they're never stress-tested. Given that the FSRS-5 state transition is a read-modify-write inside a transaction, this is a real risk.

### 3. Auth coverage is thin

Only `test_e2e_f29_outcomes_route.py` has auth tests (reader/writer key enforcement). The F4, F5, F20, and F26 endpoints have no auth tests at all. If any of these are exposed beyond localhost, this is a gap.

### 4. `asyncio.new_event_loop()` pattern in tests

Appears in 4 test files (~15 call sites). Creating a new event loop per seed helper means connection pools and session contexts can silently leak or misbehave. The pattern works for sequential CI runs today but will break under `pytest-xdist` or any parallel runner. Should be migrated to `pytest-asyncio` fixtures.

### 5. Consolidation and procedure CLI tests are thin

`test_consolidate_cli.py` only checks `--help` output (53 lines, 0 execution tests). `test_procedure_cli_help.py` is marginally better (75 lines, one input-validation test). For operator-facing surfaces, this is insufficient coverage — no tests for `--dry-run`, `--budget`, or actual API invocation.

### 6. Diagnostics manifold endpoint is an outlier

`get_diagnostics_manifold` returns a `(status_code, payload)` tuple, breaking the consistency of the `RemoteMemexAPI` interface. The 202-async pattern (kick off UMAP computation, return 202 on cache miss) is sensible, but the tuple return should be wrapped in a typed response object.

### 7. Feature coupling through audit log

The consolidation tick selects diff units by reading `AuditLog` rows with `action='outcome.record'`. This couples F38 to F1a's audit schema in a way that isn't expressed in code (no FK, no shared constant). If the audit action string changes, consolidation silently stops picking up units. A shared constant or a typed enum would be safer.

---

## Feature-by-Feature Assessment

### F25: Write-Time Intent/Risk Classifier

| Aspect | Assessment |
|--------|------------|
| Problem | Facts had no lifecycle or sensitivity labels at ingestion |
| Solution | DSPy `ClassifyMemoryUnit` signature producing `intent_class` + `risk_class` per fact |
| Design | Default-on-fail (`durable`/`none`), Prometheus counters, safety-class blocking |
| Integration | Wired into `extraction/engine.py` via `_classify_and_filter()`, supports intent/risk overrides per call |
| Risk | LLM classifier adds latency per extraction; cold-start quality untested |

### F20: FSRS-5 Revisit

| Aspect | Assessment |
|--------|------------|
| Problem | No spaced repetition; knowledge decays without review |
| Solution | `py-fsrs 4.1.2` adapter with deterministic scheduling, 5-gate eligibility, sticky deprioritize |
| Design | Separate `revisit_due_at` and `revisit_last_reviewed_at` columns (migration 030 fixes elapsed-days bug) |
| Integration | `RevisitationService` with `list_due`, `review`, `populate_initial_schedules` |
| Risk | No concurrency tests on the review state machine; bool-coercion guard is good but only at MCP boundary |

### F4: Deprioritization

| Aspect | Assessment |
|------------|------------|
| Problem | No non-destructive way to downweight unreliable facts |
| Solution | Soft `is_deprioritized` flag, excluded from retrieval by default, restorable via explicit action |
| Design | Audit-logged, idempotent, does not cascade to related units |
| Integration | Retrieval `apply_generic_filters` respects the flag; MW scoring ignores deprioritized units |
| Risk | No auth tests on the deprioritize/restore endpoints |

### F38: Consolidation

| Aspect | Assessment |
|--------|------------|
| Problem | No unified tick coordinating contradiction + reflection + prune |
| Solution | Per-vault orchestrator selecting diff units from audit log, running 3 steps under entity locks |
| Design | Idempotent tick rows, 500-unit cap, per-entity advisory locks with timeout |
| Integration | Audit rows from `OutcomeService.record_outcome` are the signal; no direct service coupling |
| Risk | String-based coupling to `'outcome.record'` audit action; no execution tests for CLI |

### F6/F10/F26: Lint

| Aspect | Assessment |
|--------|------------|
| Problem | No automated data-quality detection |
| Solution | F6: 4 SQL-based rules (orphan mental models, cold units, sensitive unreviewed, dangling refs). F10: surprise-gated LLM checks (contradiction, schema drift). F26: dashboard aggregation |
| Design | `MaintenanceProposal` table with idempotent upsert, status transitions with `resolved_by`, LLM quota per vault |
| Integration | F25 risk_class feeds `sensitive_unreviewed` rule; dashboard is read-only aggregation |
| Risk | LLM lint is surprise-gated but MiniLM-L12 cannot detect polarity inversions (deferred to Tier B) |

### F32: Diagnostics

| Aspect | Assessment |
|--------|------------|
| Problem | No operator visibility into vault health |
| Solution | Summary (unit counts, MW scores, top entities), heatmap (co-occurrence), manifold (UMAP projection), lint pivot |
| Design | Cache-key invalidation for manifold recomputation; MCP surface gets cheap summary only |
| Integration | Three surfaces: MCP tool, CLI, REST endpoints |
| Risk | Manifold computation is expensive; 202-async pattern is correct but the `(status_code, payload)` return is an outlier |

### F9: Per-Entity Locks

| Aspect | Assessment |
|--------|------------|
| Problem | No concurrency control for entity-level operations |
| Solution | PostgreSQL advisory locks per entity, async connection pool, rate limiter |
| Design | Lock acquisition timeout defers entity to next tick rather than blocking |
| Integration | Used by consolidation tick and `memex_memory_reconsolidate` |
| Risk | No stress tests for concurrent lock contention |

### F14: Procedural Memory / Outcomes

| Aspect | Assessment |
|------------|------------|
| Problem | KV-store procedures had no outcome tracking |
| Solution | `ProcedureOutcome` table with Beta-Bernoulli MW scoring, `procedure:` namespace validation |
| Design | FK to `kv_entries.key ON DELETE CASCADE`, atomic upsert via `ON CONFLICT`, versioned envelopes |
| Integration | `OutcomeService.record_outcome` supports `target_type='kv_key'` mode; MCP tool surfaces procedure outcomes |
| Risk | Mixed `target_type` mode (memory_unit vs kv_key) is validated at the service layer but not at the SQL constraint level |

---

## Migration Summary

| Migration | Key Changes |
|-----------|-------------|
| 023 | MW counters (`success_co_count`, `failure_co_count`) on `memory_units`, `unit_entities`, `mental_models`. `is_deprioritized` flag on `memory_units`. Drops dead `access_count` column. |
| 024 | `intent_class` and `risk_class` columns on `memory_units` with CHECK constraints. |
| 025 | `maintenance_proposals` table for lint findings. Partial unique index for idempotent re-runs. |
| 026 | FSRS-5 revisit columns: `revisit_due_at`, `revisit_stability`, `revisit_difficulty`, `revisit_review_count`. |
| 027 | `consolidation_ticks` table. Indexes on `(vault_id, started_at)` and `(vault_id, completed_at DESC NULLS LAST)`. |
| 028 | `procedure_outcomes` table. FK to `kv_entries.key` with CASCADE delete. |
| 029 | `lint_llm_quota` table. Rolling 24-hour cost cap per vault. |
| 030 | `revisit_last_reviewed_at` column. Fixes FSRS-5 elapsed-days computation. |
| 031 | `resolved_by` column on `maintenance_proposals`. Records actor who resolved/dismissed. |

---

## Test Coverage Summary

| File | Lines | Assertions | Real Behavior? | Edge Cases? | Auth? | Flaky Risk |
|------|-------|-----------|-----------------|-------------|-------|------------|
| test_e2e_retrieval_augmentation.py | 569 | 65 | Yes (DB state) | Yes (ranking reversal) | No | Low |
| test_e2e_f14_llm_turn.py | 443 | 18 | Yes (real LLM) | No | No | Medium (LLM non-determinism) |
| test_e2e_f20_revisit_http.py | 327 | 32 | Yes (DB state) | Yes (bool, cross-vault) | No | Low |
| test_f10_real_llm_content.py | 182 | 5 | Yes (real LLM) | Partial | No | Medium |
| test_e2e_f4_deprioritize.py | 326 | 26 | Yes (DB + audit) | Yes (idempotency) | No | Low-Medium (retry loops) |
| test_e2e_f5_summarize_node_http.py | 325 | 35 | Mixed (mostly wiring) | Yes (429, scope) | No | Low |
| test_e2e_f29_outcomes_route.py | 328 | 23 | Yes (DB state) | Yes (mode mixing) | Yes | Low |
| test_e2e_f26_lint_dashboard_route.py | 133 | 14 | Yes (SQL pivot) | No | No | Low |
| test_lint_cli.py | 130 | 18 | Wiring | Minimal | No | Low |
| test_consolidate_cli.py | 53 | 11 | No (help only) | No | No | Low |
| test_diagnose.py | 107 | 22 | Wiring (JSON shape) | No | No | Low |
| test_procedure_cli_help.py | 75 | 16 | Minimal (validation) | No | No | Low |
| test_mcp_outcome.py | 155 | 11 | No (mock wiring) | No | No | Low |
| test_f20_review_bool_rejection.py | 88 | 2 | Yes (regression) | Yes (parametrized) | No | Low |

---

## Recommendations (Merge-Blocking)

1. **Rename colliding CLI commands.** `memex memory consolidate` and `memex consolidate tick` do different things. Merge under one subcommand or pick distinct verbs (e.g., `memex memory compact` for the F38 orchestration).
2. **Add at least one concurrent-request test** for the FSRS-5 review endpoint (the most state-sensitive path).
3. **Add auth tests for F4 and F20 endpoints.**
4. **Wrap the manifold `(status_code, payload)` tuple** in a typed response object (`ManifoldResult` with `status` and `data` fields).
5. **Extract audit action strings** (`'outcome.record'`) into shared constants or a typed enum.

## Recommendations (Post-Merge)

6. Migrate `asyncio.new_event_loop()` test helpers to `pytest-asyncio` fixtures before enabling parallel test execution.
7. Add execution tests for the `consolidate` and `procedure` CLI commands (beyond help-text fences).
8. Consider field-masking on `MemoryUnitDTO` to avoid bloating every search result with deprioritization/revisit/classification fields.
9. Generalize `RateLimitExceeded` from a two-method pattern to a client-level interceptor as more rate-limited operations are added.
10. Document the intentional absence of an MCP tool for F38 consolidation (AC-F38-5) in user-facing docs, not just code comments.

---

## Overall Verdict

**This is a well-architected branch that will meaningfully improve memex.** The core value — closing the feedback loop so facts can be scored, revisited, and pruned — is correctly implemented. The engineering quality is high: default-on-fail semantics, bounded blast radius, idempotent operations, and audit trails throughout. The main risks are operational (surface area, naming collisions, test gaps) rather than architectural. The five merge-blocking items above are polish, not rework.
