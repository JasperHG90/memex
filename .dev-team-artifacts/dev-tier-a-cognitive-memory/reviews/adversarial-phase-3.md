# Phase 3 Adversarial Review — Tier-A Cognitive Memory

**Reviewer**: QA Engineer (dev-tier-a-cognitive-memory)
**Branch**: `memory_augmentation`
**HEAD**: `acbf6a5` (snapshot taken 2026-05-01)
**Mandate**: Adversarial code-read review of the full Tier-A surface (F4, F5, F6, F8, F9, F10, F14, F20, F26, F29, F32, F38). Hunt Wave 0 invariant breaches (AC-X-7 vault scoping, AC-X-8 single-leader lock, AC-X-10 boot+discovery canary), three-surface parity drift, missing per-vault auth, race windows, and migration ordering.
**Certainty bar**: ≥95% on every finding. Each cites file:line proof.

---

## Severity legend
- **CRITICAL** — production-impact bug or invariant breach merging would harm users; blocks Phase 3 close.
- **HIGH** — invariant violation or correctness gap; blocks Phase 3 close.
- **MEDIUM** — correctness or hygiene gap that should be fixed but does not block close (can be tracked as follow-up task).
- **LOW** — nit, docs, or test-coverage improvement.

---

## Verified clean (no findings)

| Surface | Verification |
|---|---|
| **AC-X-8 single-leader lock** | Only 2 source files reference `5432789123456789`: `packages/core/src/memex_core/scheduler.py:18` (`MEMEX_LEADER_LOCK_ID`) and `packages/core/src/memex_core/services/locks.py:43` (`LEADER_LOCK_ID`). Only 3 runtime files use advisory-lock SQL: `scheduler.py`, `services/locks.py`, `alembic/env.py`. The migration uses blocking `pg_advisory_lock`, not the runtime `pg_try_advisory_lock`. F9 entity-lock formula at `services/locks.py:69-77` is `ENTITY_LOCK_HIGH_BIT \| raw` with bit 62 set, disjoint from leader (~2^52). |
| **F20 dual-layer bool-rejection** | MCP gate at `packages/mcp/src/memex_mcp/server.py:3944-3968` and HTTP gate at `packages/core/src/memex_core/server/revisit.py:36-46` + `:51-62`. Both layers present at HEAD. |
| **F38 step ordering** | `packages/core/src/memex_core/services/consolidation.py:137-166` runs contradiction → reflection → prune-stale-only (B1 adjudication). Order verified. |
| **F38 ↔ F20 audit-log integration** | `packages/core/src/memex_core/services/outcomes.py` writes `AuditLog(action='outcome.record', resource_type='memory_unit')`; F38 `select_diff_units` filters on this. |
| **F32 ↔ F26 plumbing** | `pending_by_type` helper bridges F26 lint dashboard into F32 diagnostics summary. |
| **F20 service-layer cross-vault** | `revisit.py:163-164` maps service-layer `PermissionError` → 403; `api.review_memory_unit` enforces unit-vault check. |
| **AC-X-7 vault scoping (Tier-A new tables)** | `MaintenanceProposal` (`sql_models.py:1763`), `LintLLMQuota` (:1861), `ConsolidationTick` (:1653), `ProcedureOutcome` (:1577) all carry `vault_id` with FK→vaults; `MaintenanceProposal.vault_id` is documented nullable per AC-F6-1 (NULL = global findings, reserved for Tier B). |
| **Migration ordering** | Linear chain 024 → 025 → 026 → 027 → 028 → 029, all with `down_revision` set. No branch labels. |
| **Three-surface tool count** | Hermes schema set in `packages/hermes-plugin/tests/test_tools.py:121-203` enumerates 46 tools (8 stream-1 + 10 stream-2 + 4 stream-3 + 6 stream-4 + 8 stream-5 + 4 quick-wins + 1 diagnostics + 1 linter + 2 revisit + 2 locks). |

---

## HIGH findings (block Phase 3 close)

### HIGH-1 — AC-X-10 boot+discovery canary missing F4, F8, F29
**File**: `packages/hermes-plugin/tests/integration/test_live_hermes.py:49-78`
**Severity**: HIGH (Wave 0 invariant breach: AC-X-10 three-surface parity)

The test currently asserts only these post-seed verbs are reachable through Hermes' `_tool_to_provider` dispatch: `memex_get_diagnostics_summary` (F32), `memex_memory_summarize_node` (F5), `memex_get_due_for_review` + `memex_memory_review` (F20), `memex_memory_reconsolidate` + `memex_memory_consolidate` (F9). The standing AC-X-10 canary docstring (line 50-58) requires *every* new MCP verb to appear here.

Missing canary entries:
- `memex_memory_deprioritize` (F4) — registered at `packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py:3255`
- `memex_memory_restore` (F4 partner) — registered at `tools.py:3282`
- `memex_record_outcome` (F29) — registered at `tools.py:3459` (this verb just got Hermes parity in task #29; the canary was not extended at the same time — same regression class as v0.1.13)
- `memex_get_lint_flags` (F8) — registered at `tools.py:3592`

**Reproduction**:
```bash
grep -n "memex_record_outcome\|memex_get_lint_flags\|memex_memory_deprioritize\|memex_memory_restore" \
  /home/vscode/workspace/packages/hermes-plugin/tests/integration/test_live_hermes.py
# expected: 4 lines (one per verb), got: 0
```

**Proposed fix**: extend the asserted set in `test_get_tool_schemas_exposes_stream_1_and_post_seed_tools` with the four verbs, paired with one-line reasons (F4 / F29 / F8). Single-file change; no source-code diff needed.

---

### HIGH-3 — F38 consolidation tick races against F9 entity-lock
**Files**:
- `packages/core/src/memex_core/services/consolidation.py:137-153` — F38 callsite (no lock)
- `packages/core/src/memex_core/services/locks.py:209-232` — F9 reconsolidate_entity (acquires lock)
- `packages/core/src/memex_core/server/memories.py:140-159` — F9 HTTP route runs on any pod (no leader gate)
**Severity**: HIGH (concurrency invariant breach)

F38 `consolidate_vault_tick` runs from the leader-only scheduler at `scheduler.py:184-...`. For each `entity_id` in the vault, it calls `contradiction.detect_contradictions(unit_ids=...)` (line 138) and `reflection.reflect_batch(requests)` (line 153). **Neither is wrapped in `acquire_entity_lock(entity_id)`.**

F9 `LocksService.reconsolidate_entity` *does* acquire `acquire_entity_lock(entity_id)` for the *same* operation surface (contradiction over the entity's units, then reflect). F9's HTTP route `/memory/reconsolidate` (memories.py:129) runs on **any pod, not just the leader**.

Race window: leader pod runs F38 tick on entity X concurrently with a follower pod servicing `POST /api/v1/memory/reconsolidate` for entity X. Both will:
- Call `ContradictionEngine.detect_contradictions` over overlapping `unit_ids`. Contradiction-link emission is an `INSERT … ON CONFLICT DO UPDATE` against `memory_links`, but **link de-dup uses an MD5 of (src,dst,kind)** — concurrent emission can produce a spurious second row with stale confidence.
- Call `ReflectionService.reflect_batch` for entity X. The MentalModel UPSERT path is not txn-isolated against the F9 path; last-writer-wins stomps trend deltas.

The whole point of F9's per-entity advisory lock is to serialize curation per-entity. F38 silently bypasses it.

**Reproduction**:
```bash
# 1. Confirm F38 path: no lock acquisition
grep -n "acquire_entity_lock\|acquire\b" \
  /home/vscode/workspace/packages/core/src/memex_core/services/consolidation.py
# expected: zero matches in body

# 2. Confirm F9 path acquires the lock
grep -n "acquire_entity_lock" /home/vscode/workspace/packages/core/src/memex_core/services/locks.py
# expected: hit at line 232 inside reconsolidate_entity
```

**Proposed fix**: in `consolidation.py:134` block, wrap the per-entity work in `async with acquire_entity_lock(entity_id, timeout=...)`. Iterate entities one at a time inside the lock (or batch under a higher-level invariant). Since F38 runs from the leader-only scheduler, it can use a longer timeout (e.g. 60s) — there is no MCP-request urgency. If lock acquisition times out, log + skip that entity and continue (prune-stale step is unaffected because it operates on already-stale ids only).

---

### HIGH-4 — `check_vault_access` not enforced on Tier-A WRITE routes
**Files**:
- `packages/core/src/memex_core/server/memories.py` (no import of `check_vault_access`)
  - `:129-159` `reconsolidate_entity` — accepts `vault_id` in body, no auth-vault scoping
  - `:162-179` `consolidate_vault` — same
- `packages/core/src/memex_core/server/outcomes.py` (no import of `check_vault_access`)
  - `:74` `record_outcome` — accepts `vault_id`, no auth-vault scoping
- `packages/core/src/memex_core/server/lint.py` (no import of `check_vault_access`)
  - `:141-150` `lint_flags` — accepts `vault_id` query param, no auth-vault scoping
  - `:111` `lint_dismiss`, `:126` `lint_resolve` — operate on a `finding_id` whose vault is implicit; service-layer never re-checks auth
- `packages/core/src/memex_core/server/diagnostics.py` (no import of `check_vault_access`)
  - `:113-127` `get_lint_dashboard` — accepts `vault_id` path param, no auth-vault scoping (already tracked as task #36)

**Severity**: HIGH (multi-tenant authz breach)

Pre-existing routes (notes, retrieval, entities, ingestion, survey) all call `check_vault_access(auth, vault_id, api, permission=...)` to verify the API key's `auth.vault_ids` includes the requested vault. Without this gate, an API key restricted to vault A can submit a request specifying vault B, and the service layer faithfully executes against vault B. Confirmed by reading `auth.py:223-268` — the gate is the only place principal-vault binding is enforced; pure `require_write` is a permission gate (READ/WRITE/DELETE), not a vault-scope gate.

**Reproduction**:
```bash
# Compare: routers that DO call check_vault_access vs routers that DON'T
grep -rln "check_vault_access" /home/vscode/workspace/packages/core/src/memex_core/server | sort
# yields: auth.py, survey.py, ingestion.py, notes.py, retrieval.py, entities.py
# missing: memories.py, outcomes.py, lint.py, diagnostics.py, revisit.py*

# *F20 revisit.py has its own service-layer PermissionError → 403 path (revisit.py:163-164),
# so it is the only one actually fenced. F8/F9/F26/F29 are unfenced.
```

**Proposed fix**: each affected route adds `auth: Annotated[AuthContext | None, Depends(get_auth_context)]` to its signature and calls `await check_vault_access(auth, [request.vault_id], api, permission=Permission.WRITE)` before any service call. Pattern is already in use in `notes.py:160`, `notes.py:413`. ~5-line addition per route, no service-layer change. Diagnostics router already tracked as task #36; the others should be folded into the same sweep.

---

## MEDIUM findings (track as follow-ups; do not block close)

### MEDIUM-1 — Leader lock literal duplicated
**Files**: `packages/core/src/memex_core/scheduler.py:18` and `packages/core/src/memex_core/services/locks.py:43`

Both define `5432789123456789` as a `Final[int]` constant under different names (`MEMEX_LEADER_LOCK_ID` vs `LEADER_LOCK_ID`). Drift hazard: if one file's value is changed (typo, refactor) the lock-disjointness invariant silently breaks.

**Proposed fix**: pick one canonical source — recommend `services/locks.py:43` because the lock module owns lock-id semantics (`ENTITY_LOCK_HIGH_BIT`, `ENTITY_LOCK_MASK`, `entity_lock_id()`) — and have `scheduler.py:18` re-export `from memex_core.services.locks import LEADER_LOCK_ID as MEMEX_LEADER_LOCK_ID`. Existing test `test_seed_invariants.py:26` already greps for `MEMEX_LEADER_LOCK_ID` so the alias name must remain visible.

### MEDIUM-2 — F29 `success: bool` field accepts lax Pydantic v2 coercion
**File**: `packages/core/src/memex_core/server/outcomes.py:32`

`success: bool = Field(...)` will accept `"true"`, `"yes"`, `1`, `0`, and similar. F20's `quality` field has explicit `BeforeValidator(_reject_bool_quality)` defending against `bool ⊂ int`. F29's `success` is the symmetric pair (and the audit-log emission downstream is asymmetric — recording `success=False` when the agent passed `success="0"` is a silent data-quality bug).

**Proposed fix**: add `BeforeValidator(_reject_non_bool_success)` that raises `ValueError` unless `isinstance(v, bool)`. Mirror the F20 pattern at the same MCP + HTTP boundary.

### MEDIUM-3 — Aioclock leader-only tasks not serialized
**Files**: `packages/core/src/memex_core/scheduler.py:282-345` (F6, F10, F20, F32, F38 task registrations)

All five tasks run inside the same leader pod under `MEMEX_LEADER_LOCK_ID`. They are scheduled by aioclock at independent intervals. Per-table isolation is reasonable (F6 writes `maintenance_proposals`, F10 writes `lint_llm_quota`, F20 writes `revisit_schedule`, F32 writes UMAP cache, F38 writes `consolidation_ticks`), but cross-task interactions are not pinned: e.g. F38 reflection-batch on entity X and F6 lint over MentalModel(X) can race the same row.

**Proposed fix**: track as a Wave 3 invariant test. Either (a) document non-overlap explicitly in `RFC-008` and add a unit test that asserts the table-write-set per task is disjoint, or (b) serialize all leader-pod tasks behind a single `asyncio.Lock` if disjointness is not actually achievable. Defer to staff-engineer call.

---

## LOW findings (nice-to-have; do not block)

### LOW-1 — F10 `LintLLMQuota.count` constraint nit
**File**: `packages/core/src/memex_core/memory/sql_models.py:1900`

`CheckConstraint('count >= 0', name='ck_lint_llm_quota_count_non_negative')` is correct, but RFC-006 cite specifies `count int` only — the non-negative is a defensive-engineering add. No bug; just call out that the contract source is broader than the spec. Already noted as LOW-F10-1 from prior substrate review.

### LOW-2 — F38 has no Hermes-exposed verb (intentional, but no canary line documents this)
**File**: `packages/hermes-plugin/tests/integration/test_live_hermes.py`

F38 is scheduler-driven only (no MCP/Hermes verb). The canary list does not say so explicitly — a future agent reading the test could assume F38 was forgotten. Add a one-line comment: `# F38: scheduler-only, no MCP verb (intentional per RFC-008)`.

---

## Phase 3 close criteria

Phase 3 closes when **HIGH-1, HIGH-3, HIGH-4** are resolved. MEDIUM-1/2/3 and LOW-1/2 may be tracked as follow-up tasks (#33/#34/#36-pattern) and do not block close.

**Recommended dispatch** (orchestrator decides):
- HIGH-1: spawn a single dev to extend `test_live_hermes.py:49-78` — test-only, ~10-line diff. Trivial.
- HIGH-3: spawn dev-ws-quick-wins (owner of F38) to wrap `consolidation.py:134-166` per-entity work in `acquire_entity_lock`. Touches one file; needs a new integration test that exercises F38-tick + F9-reconsolidate concurrency on the same entity_id and asserts only one of the two paths emits a contradiction-link row.
- HIGH-4: extend task #36 from "diagnostics router only" to "Tier-A WRITE-route check_vault_access sweep" covering memories.py, outcomes.py, lint.py, diagnostics.py. ~5 lines per route + a parametrised integration test exercising vault-A key vs vault-B request.

---

## Audit trail

| Verification surface | Method | Result |
|---|---|---|
| AC-X-7 vault scoping (new tables) | Read sql_models.py for class definitions; confirm vault_id field + FK | PASS |
| AC-X-8 single-leader (literal `5432789123456789`) | grep all packages/ | PASS — only 2 source files |
| AC-X-8 single-leader (advisory-lock SQL) | grep `pg_try_advisory_lock\|pg_advisory_lock` in runtime src | PASS — only 3 files (scheduler.py, services/locks.py, alembic/env.py) |
| AC-X-10 boot+discovery canary | Read test_live_hermes.py:49-78 against tools.py registry | FAIL — HIGH-1 |
| F9 entity-lock disjointness | Read services/locks.py:43-77, confirm bit-62 partition | PASS |
| F38 step ordering | Read services/consolidation.py:120-200 | PASS — contradiction → reflection → prune-stale-only |
| F38 ↔ F9 race | Read consolidation.py vs locks.py vs memories.py routes | FAIL — HIGH-3 |
| F20 dual-layer bool gate | Read server/revisit.py:36-62 + mcp/server.py:3944-3968 | PASS |
| F20 cross-vault scoping | Read revisit.py:163-164 PermissionError → 403 path | PASS (service-layer) |
| F8/F9/F26/F29 cross-vault scoping | Read each router for `check_vault_access` import | FAIL — HIGH-4 |
| Migration linear chain (024-029) | grep `down_revision` in 025-029 | PASS |
| Tool count three-surface | Read test_tools.py:121-203 vs tools.py grep | PASS — 46 |
| F38 has Hermes verb | grep `consolidation_tick` in hermes/tools.py | N/A — scheduler-only (intentional) |

**Phase 3 status**: 3 HIGH open. Until HIGH-1, HIGH-3, HIGH-4 resolve, Phase 3 cannot close.

---

## Phase 3 closeout — VERIFIED CLEAN

**Reviewer**: `qa-adversarial-2` (replacement for original `qa-adversarial`, per dev-team rework-loop protocol)
**Date**: 2026-05-01
**Branch**: `memory_augmentation`
**HEAD**: `41c01b5` (Merge PR #82)
**Certainty**: 99%

### Rework PR verification trail

Each Phase 3 HIGH/MEDIUM finding was rework'd in a dedicated PR, gated by `qa-adversarial-2` GO/NO-GO, then merged:

| Finding | Rework PR | Merge commit | Verdict |
|---|---|---|---|
| HIGH-1 (canary missing F4/F8/F29) | #83 `fix/canary-high1` | `6051c47` | GO (test-only, 4-line diff) |
| HIGH-3 (F38 ↔ F9 race) | #79 `fix/f38-entity-lock` | `f5b9af7` | GO (per-entity `acquire_entity_lock` + skip-and-defer) |
| HIGH-4 (Tier-A WRITE auth) | #82 `fix/diagnostics-vault-auth` | `41c01b5` | GO after rework (initial NO-GO on Gaps A+B; second pass closed both: `outcomes.py /record` + `lint.py /findings/{id}/{dismiss,resolve}` + service-layer SQL filter) |
| MEDIUM-1 (leader-lock dup) | #78 `fix/leader-lock-dedup` | `2ee752f` | GO (single source of truth + grep-guard test) |

HIGH-4 sub-history: my first verification flagged two open surfaces left by the initial CRIT-001 commit (`outcomes.py` had no `check_vault_access`, and `lint_dismiss`/`lint_resolve` operated on bare `finding_id` with no per-finding vault check). Commits `2190b27` (Gap A) and `39cc5e3` (Gap B) closed both: `outcomes.py` now gates `[resolved_vault]` after `resolve_vault_identifier`; `lint.py` adds `_gate_finding_for_write` helper + `LintService.get_finding_vault_id` route lookup + service-layer `set_status(*, vault_id=...)` SQL constraint as defense-in-depth.

### Final invariant re-check (against `41c01b5`)

| Invariant | Method | Result |
|---|---|---|
| **AC-X-7 vault scoping (server routers)** | `grep -rln check_vault_access packages/core/src/memex_core/server/` | PASS — 11 files (`auth.py` defines + 10 routers consume): `auth.py / diagnostics.py / entities.py / ingestion.py / lint.py / memories.py / notes.py / outcomes.py / retrieval.py / revisit.py / survey.py`. Compared to original report line 113-115 reproduction, the 5 previously-missing routers (`memories.py / outcomes.py / lint.py / diagnostics.py / revisit.py`) are all now present. |
| **AC-X-8 single-leader lock — literal** | `grep -rn "MEMEX_LEADER_LOCK_ID\s*=" packages/` | PASS — exactly 1 definition: `packages/core/src/memex_core/scheduler.py:18:MEMEX_LEADER_LOCK_ID = 5432789123456789`. The duplicate `LEADER_LOCK_ID` from `services/locks.py:43` is removed. |
| **AC-X-8 advisory-lock SQL — runtime files** | `grep pg_try_advisory_lock\|pg_advisory_lock packages/core/src/memex_core` | PASS — 3 runtime files: `scheduler.py:356` (`pg_try_advisory_lock(MEMEX_LEADER_LOCK_ID)`), `alembic/env.py:130` (`pg_advisory_lock(MIGRATION_LOCK_ID)` for migrations), `services/locks.py:125` (`pg_try_advisory_lock(lock_id)` per-entity). |
| **AC-X-10 boot+discovery canary** | Read `test_live_hermes.py:79-82` | PASS — 4 verbs asserted: `memex_memory_deprioritize` (F4), `memex_memory_restore` (F4), `memex_record_outcome` (F29), `memex_get_lint_flags` (F8). |
| **F38 entity-lock acquisition** | `grep acquire_entity_lock packages/core/src/memex_core/services/consolidation.py` | PASS — imported at :47, `EntityLockTimeoutError` imported at :46, `async with acquire_entity_lock(self._dsn, eid, timeout_seconds=...)` at :163 inside per-entity loop. Skip-and-defer policy preserved. |
| **F38 step ordering** | Read `consolidation.py:160-200` | PASS — contradiction (:168) → reflection (:178) → prune-stale-only (:188), all inside the per-entity lock block. Per-entity exception-handling preserves tick continuation. |
| **F20 dual-layer bool-rejection** | `grep _reject_bool_quality packages/` | PASS — HTTP layer at `server/revisit.py:43` (def) + :60 (BeforeValidator); MCP layer at `mcp/server.py:4008` (def) + :4032 (BeforeValidator). |
| **F38 ↔ F20 audit-log integration** | `grep "outcome.record" packages/core/src/memex_core` | PASS — `services/consolidation.py:8` + `:314` documents and reads `AuditLog action='outcome.record'`. |
| **F32 ↔ F26 plumbing** | `grep pending_by_type packages/core/src/` | PASS — `diagnostics/lint_dashboard.py` (helper) → `server/diagnostics.py:134` (route surfaces it). |
| **F20 service-layer cross-vault** | `grep PermissionError packages/core/src/memex_core/server/revisit.py` | PASS — :176 maps `PermissionError` → 403; service-layer enforces unit-vault check for `review_memory_unit`. |
| **AC-X-7 Tier-A new tables vault_id** | `python` AST scan of `sql_models.py` | PASS — `MaintenanceProposal` (NULL allowed, FK→vaults `ondelete=CASCADE`), `LintLLMQuota` (NOT NULL, FK→vaults), `ProcedureOutcome` (NOT NULL, FK→vaults), `ConsolidationTick` (NOT NULL, model lacks `ForeignKey()` annotation but the migration `027_consolidation_ticks.py:100-105` adds `fk_consolidation_ticks_vault_id` `ondelete=CASCADE` at the SQL layer — model/migration mismatch noted as MEDIUM follow-up; database-level invariant holds). |
| **Migration linear chain 024-029** | `ls alembic/versions/` | PASS — 6 sequential migrations (`024_intent_risk_classifier`, `025_maintenance_proposals`, `026_revisit_columns`, `027_consolidation_ticks`, `028_procedure_outcomes`, `029_lint_llm_quota`). |
| **Three-surface tool count parity** | `pytest test_tools.py::test_all_schemas_have_required_fields` | PASS — set-equality assertion holds at 46 tools (8 stream-1 + 10 stream-2 + 4 stream-3 + 6 stream-4 + 8 stream-5 + 4 quick-wins + 1 diagnostics + 1 linter + 2 revisit + 2 locks). |
| **F38 has no Hermes verb (intentional)** | LOW-2 from original report | PASS — scheduler-only per RFC-008. |

### Open findings (post-Phase 3)

**0 CRITICAL, 0 HIGH.** Phase 3 close criteria (line 166 of this report — "HIGH-1, HIGH-3, HIGH-4 resolved") fully met.

The MEDIUM and LOW findings from the original Phase 3 review are now triaged as follows:

| Finding | Status | Disposition |
|---|---|---|
| MEDIUM-1 (leader-lock dup) | RESOLVED via PR #78 | Closed — single source of truth + grep-guard test pinned. |
| MEDIUM-2 (F29 `success: bool` lax coercion) | OPEN | Track as follow-up. Symmetric pair to F20's `_reject_bool_quality`. Not a Phase 3 blocker. |
| MEDIUM-3 (Aioclock leader-only tasks not serialized) | OPEN | Track as Wave 3 invariant test (per original recommendation). Not a Phase 3 blocker. |
| LOW-1 (F10 `LintLLMQuota.count` constraint nit) | OPEN | Documentation note only. |
| LOW-2 (F38 has no canary line documenting scheduler-only) | OPEN | One-line comment add. |

### New follow-up items surfaced during close-out (NOT Phase 3 blockers)

1. **`ConsolidationTick.vault_id` model/migration mismatch**: the SQLModel definition at `sql_models.py:1653` lacks the `ForeignKey('vaults.id')` annotation that the corresponding `027_consolidation_ticks.py:100-105` migration adds at the SQL layer. Database-level invariant holds (FK exists, `ondelete=CASCADE`), but model declarations should match migration output for clarity. MEDIUM hygiene; track as backlog.
2. **`test_int_f8_lint_query.py` session-teardown** (flagged by `dev-bug-vault-scoping-3`): the existing `finally` block in `test_missing_table_raises_initialization_error` recreates the dropped table via `SQLModel.metadata.tables['maintenance_proposals'].create(checkfirst=True)`. This restores the table itself, but does NOT restore (a) database-level FK constraints from rows in other tables, (b) indexes that aren't part of `SQLModel.metadata`, or (c) any rows that were deleted via `ON DELETE CASCADE` from the original DROP. The test passes in current isolation, but cross-test session contamination remains a latent risk. LOW hygiene; track as backlog.

### Verdict

**Phase 3 → Phase 4 transition: APPROVED.**

All three blocking HIGH findings (HIGH-1, HIGH-3, HIGH-4) are resolved with route-layer auth gating, defense-in-depth at the service layer, integration tests against real Postgres, and a regression-guarding canary. AC-X-7, AC-X-8, AC-X-10 invariants verified intact via direct grep against `41c01b5`. The pre-existing "Verified clean" set (F38 step ordering, F20 dual-layer bool-rejection, F38↔F20 audit integration, F32↔F26 plumbing, migration linear chain, tool count parity) is unchanged. Tier-A surface is ready for Phase 4 close.
