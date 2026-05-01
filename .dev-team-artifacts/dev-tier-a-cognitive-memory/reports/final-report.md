# Tier-A Cognitive Memory — Final Phase 4 Report

**Branch:** `memory_augmentation` @ `826b6b7`
**Base:** `origin/main`
**Delta:** 223 files changed, +32,174 / −168 (`git diff origin/main..HEAD --shortstat`)
**Commit count:** 50 commits across 12 merge points (10 wave-2 feature merges + 2 POC merges; Wave-1 substrate landed in earlier merges still ahead of `origin/main`).

## 1. Executive Summary

Phase 4 closes the Tier-A cognitive-memory delivery against the spec at `cognitive-memory-research-report.md` §4. Eleven Tier-A features (F4, F5, F6, F8, F9, F10, F14, F20, F26, F29, F32, F38) shipped with full agent-surface parity (HTTP + MCP + Hermes plugin + Claude Code plugin where applicable), backing services, alembic migrations, and three-tier test coverage (unit / integration / E2E). The substrate adds: a maintenance-proposal ledger plus rule-based and surprise-gated LLM linters, an FSRS-5 revisitation scheduler, per-entity asyncpg advisory locks, a consolidation orchestrator, a procedural-KV namespace with `procedure_outcomes` audit, and a UMAP-backed diagnostics surface.

Headline metrics: ten new MCP tools (count progression 41 → 46), eleven new alembic migrations (023, 025, 026, 027, 028, 029 and feature-internal stubs), six new top-level service modules (`services/lint.py`, `services/lint_llm.py`, `services/locks.py`, `services/revisitation.py`, `services/consolidation.py`, `services/diagnostics.py`), two new POC validation harnesses (asyncpg locks; FSRS-4.5 bit-exact parity at 76/76), and a worktree-venv-isolation convention captured after three live recurrences.

All eleven Tier-A features are merged into `memory_augmentation`. Nine open follow-ups (issues #27–#36) carry over to post-Phase-2 work; none are blockers for the surface area shipped here.

## 2. What Shipped

| ID  | Title                                       | Service / module                                            | MCP surface                                                |
| --- | ------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------- |
| F4  | Memory deprioritize + audit                 | `services/outcomes.py`, alembic `023`                       | `memex_memory_deprioritize`, `memex_memory_restore`        |
| F5  | Reflection summarize-node + rate limit      | `services/reflection.py` (widening), token-bucket limiter   | `memex_memory_summarize_node`                              |
| F6  | Maintenance-proposal ledger + rule engine   | `services/lint.py`, alembic `025`                           | (write-only, no MCP)                                       |
| F8  | Lint-flag query tool                        | `services/lint.py::get_findings`, opaque cursor             | `memex_get_lint_flags`                                     |
| F9  | Per-entity advisory locks + reconsolidate   | `services/locks.py`                                         | `memex_memory_reconsolidate`, `memex_memory_consolidate`   |
| F10 | Surprise-gated LLM lint                     | `services/lint_llm.py`, `memory/lint_llm/`, alembic `029`   | (background, no direct MCP)                                |
| F14 | Procedural KV namespace                     | `services/kv.py`, `services/outcomes.py`, alembic `028`     | `memex_record_outcome`, briefing surface                   |
| F20 | FSRS-5 revisitation                         | `services/revisitation.py`, `memory/revisit.py`, `026`      | `memex_get_due_for_review`, `memex_memory_review`          |
| F26 | F32 lint dashboard aggregator               | `diagnostics/lint_dashboard.py`                             | (HTTP + CLI; folded into F32 summary)                      |
| F29 | Hermes record-outcome parity                | Hermes provider + `MemexAPIProtocol.record_outcome`         | `memex_record_outcome` (Hermes parity)                     |
| F32 | Diagnostics core                            | `diagnostics/{summary,heatmap,umap}.py`                     | `memex_get_diagnostics_summary`                            |
| F38 | Consolidation orchestrator                  | `services/consolidation.py`, alembic `027`                  | (scheduler-driven; CLI: `memex consolidate`)               |

### F4 — `memex_memory_deprioritize` + audit

Soft-delete pathway for retrieval candidates. Tool registered at `packages/mcp/src/memex_mcp/server.py:3611` (deprioritize) / `:3647` (restore). Audit rows written through `services/outcomes.py`. Tests: `tests/test_e2e_f4_deprioritize.py`, `packages/cli/tests/test_memory_deprioritize_cli.py`, `packages/core/tests/integration/memory/retrieval/test_int_deprioritization.py`.

### F5 — `memex_memory_summarize_node`

Widens `ReflectionService.limit` to `int | None` and adds a per-`(actor, entity)` token-bucket. Synchronous endpoint at `POST /memories/summarize-node`. MCP at `server.py:3684`. Tests: `packages/core/tests/unit/services/test_summarize_node_service.py`, `tests/test_e2e_f5_summarize_node_http.py`.

### F6 — Maintenance-proposal ledger + rule engine

`MaintenanceProposal` model + 4 v1 SQL rules (orphan-fact, low-confidence-stale, contradiction-unresolved, unlinked-mention). Scheduler tick every 6h via leader election. RFC: `rfcs/006-f6-maintenance-ledger-linter.md`. Migration `025_maintenance_proposals.py`. Tests: `packages/core/tests/integration/services/test_int_lint.py`, `test_int_lint_scheduler.py`, `test_int_lint_metrics.py`, `packages/core/tests/unit/test_lint_rule_sql_audits.py`.

### F8 — `memex_get_lint_flags`

Shape-stable DTO (`LintFindingDTO`) with opaque base64 cursor for pagination. MCP at `server.py:3754`. Tests: `packages/core/tests/integration/services/test_int_f8_lint_query.py`, `packages/core/tests/unit/test_lint_query_cursor.py`, `packages/hermes-plugin/tests/test_hermes_get_lint_flags.py`.

### F9 — Per-entity advisory locks + reconsolidate/consolidate

Single-int asyncpg advisory locks keyed off entity UUID via SHA-256 → 62-bit truncate, OR'd with `1 << 62`. Disjoint from `LEADER_LOCK_ID = 5432789123456789` by construction. Source: `packages/core/src/memex_core/services/locks.py:43-73`. POC: `pocs/001-f9-asyncpg-locks/result.md` (5/5). RFCs: `005-F9-advisory-locks.md`, `008-f9-per-entity-lock-and-reconsolidate.md`. MCP at `server.py:3820` / `:3862`. Tests: `packages/core/tests/integration/test_int_f9_locks.py`, `test_int_f9_consolidate.py`, `test_int_f9_reconsolidate.py`, `packages/core/tests/unit/services/test_locks.py`, `test_locks_invariants.py`.

### F10 — Surprise-gated LLM lint

Anisotropy-corrected surprise score (`memory/lint_llm/surprise.py`) feeds `LintLLMService` (`services/lint_llm.py`) which: rate-limits LLM calls via 24h rolling count cap (`LintLLMQuota` table, alembic `029`), defers (does not drop) overflow, and writes findings to the F6 ledger. RFC: `010-f10-surprise-gated-llm-lint.md`. POC: `pocs/002-f10-surprise-threshold/`. Tests: `packages/core/tests/integration/services/test_int_f10_lint_llm.py`, `packages/core/tests/integration/memory/test_int_lint_llm_checks.py`, `packages/core/tests/unit/memory/test_lint_llm_surprise.py`.

### F14 — Procedural KV namespace

Adds `procedure:` envelope namespace in `services/kv.py` with `procedure_outcomes` audit table (alembic `028`). Briefing surface in `packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py`. Tests: `packages/core/tests/integration/test_int_f14_procedure_kv.py`, `test_int_f14_record_outcome.py`, `test_int_f14_top_outcomes.py`, `test_int_alembic_028.py`, `packages/hermes-plugin/tests/test_briefing_f14.py`.

### F20 — FSRS-5 revisitation

`memory/revisit.py` wraps `py-fsrs>=4.0,<5.0`. `services/revisitation.py` implements 5-gate eligibility (vault scope, lifecycle, confidence floor, sticky-deprioritize, lockout window). Bit-exact parity vs. FSRS-4.5 reference verified at 76/76 in `pocs/003-f20-fsrs-parity/`. Daily scheduler tick. MCP at `server.py:3900` / `:3957`. RFCs: `008-F20-fsrs-revisit.md`, `014-f20-fsrs-revisitation.md`. Tests: `tests/test_e2e_f20_revisit_http.py`, `packages/core/tests/unit/test_revisit_*.py`.

### F26 — F32 lint dashboard aggregator

`diagnostics/lint_dashboard.py` pivots maintenance-proposal counts by `(rule_id, severity, vault_id)`; surfaced through F32's summary endpoint and `memex diagnose lint --vault X`. Tests: `packages/core/tests/integration/test_int_f26_lint_dashboard_aggregator.py`, `tests/test_e2e_f26_lint_dashboard_route.py`.

### F29 — Hermes record-outcome parity

Closes the F1a/F4 outcome-recording gap on the Hermes side: `MemexAPIProtocol.record_outcome` + `RemoteMemexAPI.record_outcome` HTTP wrapper + `POST /api/v1/outcomes/record` route + Hermes handler. Drift-guard bumped 40 → 41. Tests: `packages/hermes-plugin/tests/test_record_outcome_f29.py`, `packages/common/tests/test_client_record_outcome.py`.

### F32 — Diagnostics core

Composed from four modules under `packages/core/src/memex_core/diagnostics/`: `umap.py` (cache + projection), `heatmap.py`, `summary.py`, `lint_dashboard.py` (F26). Three-surface parity (HTTP + MCP `memex_get_diagnostics_summary` + CLI). Tests: `packages/core/tests/integration/test_int_f32_diagnostics_summary.py`, `packages/core/tests/unit/test_diagnostics_cache_key.py`.

### F38 — Consolidation orchestrator

`services/consolidation.py` runs contradiction → reflection → prune-stale-only sequence per tick. Records ticks to `consolidation_ticks` (alembic `027`) and logs outcomes through the audit log. Integration seam with F20: F20's `review()` writes audit rows that F38 reads — no direct service coupling. CLI: `memex consolidate`. Tests: `packages/core/tests/unit/test_consolidation_writes.py`, `packages/cli/tests/test_consolidate_cli.py`.

## 3. Architecture Decisions of Note

**F9 lock disjointness invariant.** `LEADER_LOCK_ID = 5432789123456789` (~2^52) and entity locks are forced into [2^62, 2^63−1] by OR'ing with `1 << 62`. Asserted by construction at `services/locks.py:43-45`: `(LEADER_LOCK_ID & (1 << 62)) == 0` and every entity lock has `(lock_id & (1 << 62)) != 0`. Verified at runtime in `tests/unit/services/test_locks_invariants.py`.

**F20 dual-layer bool rejection.** Python's `bool ⊂ int` would silently coerce `True` to `Quality.AGAIN` (= 1) at the FSRS gate. Defended at both surfaces: MCP via `_reject_bool_quality` `BeforeValidator` at `packages/mcp/src/memex_mcp/server.py:3944-3953`, HTTP via `BeforeValidator` at `packages/core/src/memex_core/server/revisit.py:24-37`. Caught in QA loop, not pre-merge static review.

**F10 surprise-gating with count-based 24h cap and defer-not-drop.** The cap is a count, not a cents budget — simpler reasoning, no LLM-pricing coupling. Overflow events are queued for the next tick, never silently dropped. Quota table has a `count >= 0` `CheckConstraint` (commit `7c62fcc`).

**F38 audit-log integration seam.** F38's tick consumes audit-log rows produced by F20's `review()` rather than calling `RevisitationService` directly. Documented in `services/revisitation.py::review()` docstring. Decouples scheduler ticks from synchronous review traffic.

**Set-union tool count drift guard.** Wave-2 sub-merges concurrently bumped the Hermes provider tool-roster guard. Final reconciliation merged ranges via set union — not max() — to avoid losing tools added on parallel branches. Hermes guard final value: 46. Bumped at: F8 → 41, F20 → 42, F29 → 41 (parallel), F9 → 46.

## 4. Open Follow-ups (Post-Phase-2)

| Issue | Title                                                              | Owner area              |
| ----- | ------------------------------------------------------------------ | ----------------------- |
| #27   | hermes-integration CI job                                          | CI                      |
| #28   | Track-add Tier-A artifact corpus + RFC-014 weights-divergence note | Docs / track            |
| #30   | Prefetch race flake under load (in flight)                         | Retrieval               |
| #31   | Bulk-suite flaky test triage                                       | Test infra              |
| #32   | F8 `test_missing_table_raises_initialization_error` CASCADE drop without restore | F8 / fixtures |
| #33   | F9 `consolidate` rate-limit (RFC-008 line 125)                     | F9                      |
| #34   | F9 `MaintenanceProposal.resolved_by` column (Wave 3 schema patch)  | F9 / schema             |
| #35   | F10 real-LLM `@pytest.mark.llm` content-quality test               | F10 / eval              |
| #36   | Diagnostics router `check_vault_access` sweep                      | F32                     |

None block Tier-A surface acceptance; #30 and #31 are flake-class, #32–#34 are cleanup, #28 is documentation, #27 is CI hardening, #35–#36 are coverage extension.

## 5. Process Notes

**Worktree venv isolation hazard hit 3×.** Shared `uv` cache caused imports to resolve to a sibling worktree's source three times during F9, F20, and F32 development — manifesting as test failures that depended on which worktree had been most recently `uv sync`'d. Codified in `conventions/worktree-venv-isolation.md`. Mitigation: dedicated venv per worktree, asserted at agent prompt time.

**Tool count drift guard requires set-union, not max.** First reconciliation attempt during F9/F26/F10 sub-wave-C used `max(branch_a, branch_b)` and dropped F29's roster bump silently. Subsequent merges enforced set-union semantics over the actual tool-name set, not the count.

**QA loop caught a real correctness bug.** The F20 bool-coercion bug (`True → Quality.AGAIN`) was not caught by static schema review or unit tests — it surfaced only in the QA real-LLM turn, where an agent passed `quality=True` and the body completed successfully without raising. Fix landed as `d373acc fix(F20): block bool from quality at the Pydantic gate, not the body` and a paired test in `test_revisit_review_vault_scope.py`.

**Two-hour silent agent zombification.** During sub-wave-C parallel execution, the F9 and F10 dev agents went silent for ~2h mid-implementation. Orchestrator polling detected the stall; recovered F20/F26/F10 via direct merge of completed branches and re-prompted the F9 dev agent without losing in-progress work. Reinforces that orchestrator must poll, not rely on agent self-report.

## 6. MCP Tool Count Progression

| Stage              | Count | Delta                                                       |
| ------------------ | ----- | ----------------------------------------------------------- |
| Pre-Tier-A baseline| 41    | (Wave-1 substrate + `memex_record_outcome`)                 |
| F8 merge           | 42    | +`memex_get_lint_flags`                                     |
| F20 merge          | 44    | +`memex_get_due_for_review`, +`memex_memory_review`         |
| F4 / F5 / F38 / F32| 46    | +`memex_memory_deprioritize`, +`memex_memory_restore`, +`memex_memory_summarize_node`, +`memex_get_diagnostics_summary` (F38 CLI-only; F26 folded into F32 summary) |
| F9 final merge     | 46    | +`memex_memory_reconsolidate`, +`memex_memory_consolidate`; F4/F5/F32 already counted |

Net new MCP surface: 10 tools registered in `packages/mcp/src/memex_mcp/server.py` between lines 3469 and 4028. F10, F14 (KV-only), and F26 (CLI/HTTP only) intentionally did not add MCP tools — F10 is background-scheduled and F26 surfaces through F32.

---

## Phase 3 — Adversarial Review & Rework (2026-05-01)

This addendum records the Phase 3 adversarial-review cycle that ran after the body of this report was first drafted. Phase 3 surfaced four findings that required rework before close-out; all four were adjudicated GO by `qa-adversarial-2` and merged into `memory_augmentation`.

### Adjudicated findings → rework commits

| Finding | Description | Rework commit(s) on `memory_augmentation` |
| --- | --- | --- |
| HIGH-1 | Hermes boot+discovery canary missing F4 / F8 / F29 verbs | `e9e9920` (`test(hermes): extend AC-X-10 canary to F4/F8/F29`) |
| HIGH-3 | F38 ↔ F9 race — `ConsolidationService` did not acquire per-entity lock | `9e596e2` (`fix(F38): acquire per-entity lock around ConsolidationService loop`) + `2f14d43` (leader-lock dedup) |
| HIGH-4 | Tier-A WRITE auth gaps on routers (F4 / F9 / F20 / F8 / F29 / lint) | `00a6826` (CRIT-001 sweep), `cbd46ce` (F32 diagnostics), `992fb35` (F8 MCP), `29d06e5` (UnitsService), `2190b27` (F29 `/record` Gap A), `39cc5e3` (lint dismiss/resolve Gap B + service-layer SQL filter) |
| MEDIUM-1 | `LEADER_LOCK_ID` duplicated in `services/locks.py` and `scheduler.py` | `2f14d43` (`refactor(locks): dedup MEMEX_LEADER_LOCK_ID source of truth`) |

### Backlog rework closed in the same window

- #28 + #40 → PR #77 (RFC-014 FSRS-5 patch + Tier-A artifact corpus, commit `e59bb28`)
- #30 → PR #75 (prefetch race triage)
- #31 → PR #81 (bulk-suite flaky test triage, commit `8458bf3`)
- #32 → PR #73 (F8 `test_missing_table_raises_initialization_error` CASCADE-restore fix, commit `0955721`)
- #33 → PR #76 (F9 per-vault rate limit on `memex_memory_consolidate`, commit `9811301`)
- #34 → PR #80 (F9 `MaintenanceProposal.resolved_by` column, commit `110b55f`)
- #35 → already merged earlier (F10 real-LLM `@pytest.mark.llm` test, commit `0875255`)

### Final state

- **`memory_augmentation` HEAD post-Phase-3-rework**: `39cc5e3` — `fix(server/lint): enforce per-vault auth on /findings/{id}/{dismiss,resolve} + service-layer SQL filter (HIGH-4 Gap B)`.
- **Phase 3 close criteria**: ALL MET. **0 CRITICAL / 0 HIGH open.** AC-X-7 (vault scoping on 11 server router files), AC-X-8 (single `MEMEX_LEADER_LOCK_ID` definition), and AC-X-10 (Hermes canary covers F4/F8/F29 verbs) verified intact against the post-rework HEAD.
- **Carryover**: 6 Tier-B follow-ups (3 MEDIUM, 3 LOW) — symmetric `bool` coercion guard on F29 `record_outcome`, Aioclock leader-task serialization, `ConsolidationTick.vault_id` model/migration mismatch, F10 `LintLLMQuota.count` value-range nit, F38 scheduler-only canary documentation, and F8 lint-query teardown hardening — filed to `BACKLOG.md` § "Tier-A Phase 3 carryover (added 2026-05-01)".

Phase 3 → Phase 4 transition: **APPROVED**. Tier-A surface ships clean.
