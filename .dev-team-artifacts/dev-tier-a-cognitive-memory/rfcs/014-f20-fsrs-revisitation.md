# RFC-014: F20 FSRS-based memory revisitation

| Field | Value |
|-------|-------|
| **Status** | In Review (C1 adjudicated; AC-F20-3 patched in requirements.md) |
| **Author** | staff-eng-2 |
| **Reviewers** | staff-eng-1 (pending round-3 ratification) |
| **Created** | 2026-04-30 |
| **Updated** | 2026-04-30 |
| **Feature** | F20 |
| **§4 anchor** | `cognitive-memory-research-report.md:869` |
| **POC required** | YES — FSRS reference parity |
| **AC patch required** | DONE — AC-F20-3 patched (mw_score-driven comprehensive predicate) |

## Problem Statement

F20 introduces an FSRS scheduler:
- Three new columns on `MemoryUnit`: `next_review_at`, `review_interval_days`, `review_stability`.
- Module `memory/revisit.py` implementing the FSRS reference formula.
- MCP tools `memex_get_due_for_review(vault_id)` and `memex_memory_review(unit_id, quality)`.
- Daily scheduler task that populates initial schedules and surfaces due units.
- Integration with F1 outcome counters: `quality='again'/'hard'` → `failure_co_count++`; `'good'/'easy'` → `success_co_count++`.

**RESOLVED — AC-F20-3 patched in requirements.md (2026-04-30):** the original AC-F20-3 referenced `importance >= threshold`, but the `importance` column does not exist on `MemoryUnit` (verified at `sql_models.py:490-744`). The patched AC mandates a comprehensive eligibility predicate driven by columns that DO exist:

```
intent_class IN ('permanent', 'durable')
AND status = 'active'
AND is_deprioritized = false
AND confidence >= 0.5
AND mw_score >= 0.4
```

Where `mw_score = compute_mw_score(success_co_count, failure_co_count)` — the Beta-Bernoulli posterior mean from `services/outcomes.py:33`. Cold-start units (0/0 outcomes) have `mw_score = 0.5` and `confidence = 1.0` (default), so they pass; only units with established negative outcomes (mw_score < 0.4 → at least 2 failures and 0 successes given the Beta-Bernoulli formula) OR with confidence dragged below 0.5 by contradiction detection are excluded.

**Per Wave 0 §6 + F4 sticky-deprioritize semantics**, F20 does NOT auto-resurface a deprioritized unit even if its `mw_score` later recovers; reversal requires an explicit `memex_memory_restore` call. This prevents F20 from undoing F4 user intent.

**Schema columns referenced (all verified to exist at HEAD `1c0a464`):**
- `intent_class` at `sql_models.py:584`
- `status` at the unit row (active | stale | superseded — used elsewhere)
- `is_deprioritized` at `sql_models.py:578`
- `confidence` at `sql_models.py:596`
- `success_co_count` / `failure_co_count` at `sql_models.py:566/572` (input to `compute_mw_score`)

**Sensitive-unit override — design freezes WITHOUT it (per team-lead direction, 2026-04-30):** AC-F20-3 marks `risk_class='sensitive'` override as TBD pending PO governance decision. **This RFC's design freezes around the comprehensive-predicate-WITHOUT-override path.** Rationale: PO has been pinged directly; the override is one-line config in Phase 2 if adopted, or dropped entirely if PO defers it to F6 governance lint — either resolution is non-blocking for convergence.

If PO adopts the override post-Phase-1, the addition is mechanical:

```python
# Conditional addition (NOT in v1; only if PO ratifies):
# if unit.risk_class == 'sensitive':
#     return _passes_intent_status_deprioritize(unit)  # bypass mw + confidence
```

If PO defers entirely, the TBD sentence is removed from AC-F20-3 and this RFC's "Sensitive-unit override" section is deleted. Convergence does NOT block on the resolution.

## Proposed Solution

### Design

#### Schema migration (alembic 026)

```python
# alembic/versions/026_revisit_columns.py
op.add_column('memory_units', sa.Column('next_review_at', sa.DateTime(timezone=True), nullable=True))
op.add_column('memory_units', sa.Column('review_interval_days', sa.Float, nullable=True))
op.add_column('memory_units', sa.Column('review_stability', sa.Float, nullable=True))
op.create_index('idx_memory_units_next_review_at', 'memory_units', ['next_review_at'],
                postgresql_where=sa.text('next_review_at IS NOT NULL'))
```

All three columns nullable — units that don't qualify for FSRS scheduling have NULL values; the scheduler-due query is `WHERE next_review_at <= now() AND next_review_at IS NOT NULL`.

#### Eligibility predicate — `services/revisit.py::is_eligible`

```python
def is_eligible_for_review(unit: MemoryUnit) -> bool:
    """Comprehensive eligibility predicate per AC-F20-3 (patched 2026-04-30).

    Wave 0 §6 + F4 sticky semantics: deprioritized units do NOT auto-resurface
    even if mw_score recovers. Reversal requires memex_memory_restore.
    """
    if unit.intent_class not in ('permanent', 'durable'):
        return False
    if unit.status != 'active':
        return False
    if unit.is_deprioritized:
        return False
    if unit.confidence < 0.5:
        return False
    mw_score = compute_mw_score(unit.success_co_count, unit.failure_co_count)
    if mw_score < settings.revisit.mw_threshold:  # default 0.4
        return False
    # Sensitive-unit override deferred (NOT in v1 per team-lead direction);
    # if PO adopts post-Phase-1, add: `if unit.risk_class == 'sensitive': return _passes_intent_status_deprioritize(unit)`
    return True
```

This predicate runs at:
- `populate_initial_schedules` time — only eligible units get a `next_review_at` assigned.
- `memex_get_due_for_review` time — units that became ineligible since scheduling are filtered out (e.g., user deprioritized them after they were scheduled).

The query-time daily scheduler additionally filters `next_review_at <= now()`.

#### FSRS implementation — `memory/revisit.py`

Use the **FSRS-5 reference formulas** (open algorithm, public-domain) as implemented by `py-fsrs == 4.1.2`. See ADR-006 for the version-pinning decision and the py-fsrs-version-vs-algorithm-version cross-check (the package version number does NOT match the algorithm version number — py-fsrs 4.1.2 implements FSRS-5, not FSRS-4.5).

> **Weights-divergence footnote.** The 19-weight tuple shown below is the FSRS-4.5 reference vector (originally cited in this RFC and embedded for documentation). The shipped scheduler delegates to `py-fsrs == 4.1.2`, which implements FSRS-5 with its own default weights and update rules; those defaults can diverge from the FSRS-4.5 vector listed here. The values below are retained as a paper-trail anchor for the original cited reference and MUST NOT be treated as authoritative — the authoritative defaults are whatever `py-fsrs 4.1.2` exposes at runtime. Any future bump of `py-fsrs` MUST trigger a fresh paper cross-check (per ADR-006) before this RFC's weights block is updated.

```python
@dataclass(frozen=True)
class FSRSParams:
    # Default weights shown for reference parity with the original FSRS-4.5 citation;
    # the shipped scheduler uses py-fsrs 4.1.2 (FSRS-5) defaults — see ADR-006 +
    # the weights-divergence footnote above. Per-vault tuning is still expressed
    # through this dataclass at the call site.
    w: tuple[float, ...] = (
        0.4197, 1.1869, 3.0412, 15.2441, 7.1434,
        0.6477, 1.0007, 0.0674, 1.6597, 0.1712,
        1.1178, 2.0225, 0.0904, 0.3025, 2.1214,
        0.2498, 2.9466, 0.4891, 0.6468,
    )
    request_retention: float = 0.9   # target retention probability
    maximum_interval: float = 36500   # 100 years cap

class Quality(Enum):
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4

def schedule(
    unit_state: dict | None,
    quality: Quality,
    now: datetime,
    params: FSRSParams = FSRSParams(),
) -> tuple[datetime, float, float]:
    """Compute (next_review_at, interval_days, new_stability) per FSRS-5 (py-fsrs 4.1.2; see ADR-006).

    unit_state: {'review_stability': float | None, 'review_interval_days': float | None,
                 'last_reviewed_at': datetime | None}
    Returns the new (next_review_at, interval, stability) tuple.

    First-review path (unit_state is None or stability is None): use FSRS init formulas.
    Subsequent-review path: use FSRS update formulas based on stability + retrievability.
    """
    ...
```

The implementation is a near-verbatim port of the reference algorithm. We will pin `fsrs` package version if a maintained Python implementation exists, OR vendor the formula directly to keep the dep surface minimal. **Decision: vendor it inline** — the algorithm is small (~100 LOC), public, frozen for our use, and the test suite below validates parity against published reference outputs. Avoids an external dep that may go stale.

#### Daily scheduler task

```python
async def daily_revisit_task(api: 'MemexAPI') -> None:
    async with background_session('bg-sched-revisit'):
        for vault in await api.list_vaults():
            # 1. Populate initial schedules for unscheduled qualifying units
            await api.revisit.populate_initial_schedules(vault.id)
            # 2. Surface due units to maintenance ledger
            await api.revisit.surface_due_units(vault.id)
```

Registers under existing `MEMEX_LEADER_LOCK_ID`. Default cadence: every 24 h.

#### Behavior — sticky-deprioritize is unconditional

**`is_deprioritized=false` is a hard gate that the user MUST flip via `memex_memory_restore`.** It is NOT an automatic side effect of any positive signal.

Concretely: if `record_outcome(success=true)` raises a unit's `confidence` past 0.5 AND its `mw_score` past 0.4, the unit becomes eligible on every other gate — but it remains ineligible for FSRS scheduling because `is_deprioritized=true` still fails the predicate. F20 will NOT auto-resurface the unit. Reversal is a deliberate user/agent action through `memex_memory_restore`, which clears `is_deprioritized` and is the ONLY supported path back to the schedulable set.

This is intentional. F4 deprioritize is the user/agent saying "do not surface this." A future contributor may be tempted to add an "auto-restore on positive outcomes" branch ("the unit is doing well now, why keep it suppressed?") — that branch MUST NOT be added. The curation contract is: outcome counters reflect the system's experience with the unit; deprioritize reflects the user's intent for the unit; the two are independent dimensions and outcomes do not override intent.

Test (g) in the eligibility test list (`test_int_f20_sticky_deprioritize.py::test_deprioritize_sticky_across_outcome_recovery`) is the canary: it seeds a deprioritized unit, drives positive outcomes that recover both `confidence` and `mw_score`, and asserts F20 does NOT re-schedule it. If a future change accidentally introduces auto-restore, this test fails immediately.

**QA confirmations (2026-04-30) on the patched AC, baked into the design here:**
- `confidence` field is constrained `0.0 ≤ confidence ≤ 1.0` (`sql_models.py:670`) and covered by an existing index (`:683`); the `confidence >= 0.5` floor is index-friendly.
- The eligibility predicate `(intent_class, status, is_deprioritized, confidence, success_co_count, failure_co_count)` is covered by existing indices — no new index needed for the daily-tick scheduler query.
- Default `confidence=1.0` (`:597`) means cold-start units pass the floor cleanly.
- `confidence` is mutated downward only by `services/contradiction/engine.py`; a single contradiction event will not drop a unit below 0.5, but sustained contradiction will. The 0.5 floor therefore only excludes substantially-contradicted units — calibration is good and the threshold stays.

#### MCP tools

```
memex_get_due_for_review(vault_id) -> list[DueReviewDTO]
  Returns: [{unit_id, text_preview, next_review_at, intent_class, mw_score, ...}, ...]

memex_memory_review(unit_id, quality: 'again'|'hard'|'good'|'easy') -> ReviewResultDTO
  Side effects:
    - schedule advances per FSRS
    - quality in (again, hard) → failure_co_count++
    - quality in (good, easy) → success_co_count++
  Returns: {unit_id, new_interval_days, new_next_review_at, mw_score_after}
```

Both tools registered with descriptions verbatim from §4 step 6.

#### Three-surface parity

| Surface | What ships |
|---|---|
| MCP server | `memex_get_due_for_review` + `memex_memory_review` |
| Hermes plugin | sync wrappers; briefing template adds "N memories due for review" line; new `memory_review` Hermes tool |
| Claude Code plugin | rule text mentions both verbs and the FSRS quality vocabulary |

#### CLI

- `memex review due [--vault X]` — list due units
- `memex review complete <unit_id> --quality {again|hard|good|easy}` — record review

### Implementation Steps

1. **POC FIRST** — validate FSRS reference parity (POC topic below).
2. Alembic migration (`XXX_revisit_columns.py`).
3. `memory/revisit.py` with `schedule()`, `Quality` enum, `FSRSParams`.
4. `services/revisit.py` with vault-scoped policy + initial-schedule population + due-unit query.
5. Hook quality → outcome: `quality in {'again','hard'}` → call `record_outcome(success=False)`; `{'good','easy'}` → `record_outcome(success=True)` against the unit + vault.
6. HTTP routes.
7. MCP tools.
8. CLI commands.
9. Hermes wrappers + briefing block.
10. Claude Code rule text.
11. Tests.

## Alternatives Considered

### Alternative A: add a new `importance` column to `MemoryUnit`
Rejected. AC-F20-3 references it but it does not exist; adding a column without a clear extraction-time signal would produce `NULL` everywhere. MW score is the operational signal we already have.

### Alternative B: use `confidence` (`sql_models.py:596`) as the importance proxy
Considered. `confidence` is mutated by contradiction detection (lowered when contradicted). It tracks *truthfulness*, not *importance*. A low-confidence unit may still be important (worth re-reviewing); a high-confidence trivial fact may not be worth scheduling. MW (proven retrieval load-bearing) is the better proxy.

### Alternative C: use `intent_class` alone (no second filter)
Rejected. Filtering only by intent would schedule every `permanent` and `durable` unit — including those proven irrelevant by their MW. The patched AC-F20-3 layers four additional filters (`status`, `is_deprioritized`, `confidence`, `mw_score`) to match §4's "high-importance + intent=permanent/durable" intent while respecting Wave 0 invariants.

### Alternative C': mw_score alone (no confidence floor)
Considered in round 2. Rejected: a heavily-contradicted unit (confidence dragged below 0.5) may still have decent MW (it was retrieved before the contradiction landed); scheduling it for review re-asserts wrong information. The confidence floor at 0.5 prevents that without coupling FSRS to confidence-as-multiplier.

### Alternative C'': 4-factor combined score (intent + status + mw_score + confidence weighted into a single threshold)
Considered in round 2. Rejected on QA/PO lean toward `mw_score`-alone with hard floors on the other dimensions. Reasoning: a single combined score obscures *why* a unit was excluded (was it the MW? the confidence? the status?); five hard predicates make exclusion auditable per dimension.

### Alternative D: depend on the `fsrs` Python package
Rejected. External dep adds maintenance + transitive risk; the algorithm is small enough to vendor with full test coverage.

### Alternative E: implement SM-2 (older SuperMemo algorithm) instead of FSRS
Rejected. §4 explicitly names FSRS; SM-2 is less accurate per the FSRS literature.

### Alternative F: schedule cadence per-unit (not vault-wide daily)
Rejected. Vault-wide daily tick keeps the scheduler simple. Per-unit precision is achieved by the `next_review_at` timestamp; the daily tick just surfaces units whose `next_review_at <= now()`.

### Alternative G: threshold = 0.5 (cold-start at boundary)
Considered. With `mw_score = 0.5` for cold-start, threshold 0.5 is exactly on the boundary — an `>=` predicate includes cold-start. Threshold 0.4 is one notch below: cold-start is well-included, but units with proven *negative* signal (mw_score < 0.4 → at least 2 failures, 0 successes) are excluded. Cleaner separation.

## Risk Assessment

| Risk | Certainty Impact | Mitigation |
|------|-----------------|------------|
| FSRS reference parity drifts | High before POC | POC validates against published FSRS reference outputs |
| Cold-start units flood the schedule | Medium | Default threshold 0.4 means cold-start passes; populate-initial task batches insertions; daily scheduler bounds work |
| `quality → outcome` mapping doesn't match user intent | Medium | Document mapping in tool description verbatim; LLM-turn test |
| FSRS algorithm changes upstream (FSRS-5+) | Low | We pin to FSRS-5 (per ADR-006, py-fsrs 4.1.2 implements FSRS-5; the original 4.5 reference is superseded). Future upgrades are an explicit Tier B feature and require a paper cross-check per ADR-006. |
| `next_review_at` column index dominates the table for huge vaults | Low | Partial index on `next_review_at IS NOT NULL` |
| AC patch required for `importance` column | Resolved — AC-F20-3 patched in requirements.md | See "Resolved" section above |
| Sticky-deprioritize bypass: F20 schedules a unit, user deprioritizes it, MW recovers, F20 re-schedules | High before patch | Eligibility predicate hard-filters `is_deprioritized=true` at both populate-time and query-time. Reversal requires `memex_memory_restore`. Test asserts "deprioritize-then-positive-outcomes does NOT re-schedule". |
| Sensitive-unit override deferred; design might need a one-line addition post-Phase-1 if PO adopts | Low | Per team-lead direction, RFC freezes WITHOUT the override; addition is one config line in Phase 2 if PO ratifies. Convergence does not block on PO resolution. |
| Daily tick takes > 60 s on vaults > 100k units | Medium | AC-F20-6 sets the bound; scheduler chunks work; metric exposes p95 |
| `populate_initial_schedules` runs unbounded on first deploy | Medium | Cap at N=10000 units per tick; subsequent ticks pick up the rest |

**Current certainty (post-AC patch + design-freeze + QA index/calibration confirmations, pre-POC)**: 93%
**After POC (expected)**: 95%
**What raised it**: AC-F20-3 patched in requirements.md to mw_score-driven comprehensive predicate; sticky-deprioritize sentence added + canonicalized as a Behavior section (test (g) is the canary); sensitive-override deferred; QA verified the predicate is index-coverable and that calibration of the 0.5 confidence floor is correct given how `services/contradiction/engine.py` mutates confidence.
**What would raise it further**: POC validates FSRS reference parity.
**Residual risks after mitigation**: cadence + cap defaults may need tuning post-launch; one-line override addition if PO ratifies post-Phase-1.

## POC Topic — REQUIRED

**Question**: Does the vendored `schedule()` implementation produce numeric outputs within tolerance of the published FSRS-5 reference (per ADR-006; py-fsrs 4.1.2 implements FSRS-5 — the original 4.5 reference is superseded) for a parametrised set of (initial state, quality) inputs?

**Approach**:
- Pull a set of (initial state, quality) → expected (interval, stability) tuples from the FSRS reference repository's test suite.
- Run our `schedule()` on each input.
- Assert numeric equality within ±0.001 (interval) and ±0.0001 (stability).

**Success criteria**:
- All 50+ reference tuples pass within tolerance.
- First-review and subsequent-review paths both validated.

**Failure criteria** that would force redesign:
- Algorithm divergence > 1% on any test point.
- An undocumented branch in the reference that the port misses.

**Effort**: Small (4–8 hours).

**Save POC results to**: `.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/003-f20-fsrs-parity/`.

## Required AC corrections (LANDED in requirements.md)

- **AC-F20-3** patched in `requirements/requirements.md` on 2026-04-30 (team-lead). See `requirements.md:150` for the canonical text. The patched AC matches the comprehensive predicate documented in §"Eligibility predicate" of this RFC; this RFC's design is now strictly downstream of the patched AC.

## Required Tests

| Code Path | Test Type | Test Description | Status |
|-----------|-----------|-----------------|--------|
| Migration adds three columns + partial index | Integration | `test_int_f20_migration.py` | Pending |
| Migration is reversible | Integration | `test_int_f20_migration.py::test_downgrade` | Pending |
| FSRS `schedule()` first-review path matches reference | Unit (parametrised) | `test_revisit_fsrs.py::test_first_review_parity` (50+ cases) | Pending |
| FSRS `schedule()` subsequent-review path matches reference | Unit (parametrised) | `test_revisit_fsrs.py::test_subsequent_review_parity` | Pending |
| (a) `intent_class='ephemeral'` excluded | Unit | `test_revisit_policy.py::test_ephemeral_excluded` | Pending |
| (b) Cold-start `permanent`/`durable` units WITH default `confidence=1.0` ARE scheduled | Unit | `test_revisit_policy.py::test_coldstart_included` | Pending |
| (c) `confidence=0.3` excluded regardless of MW | Unit | `test_revisit_policy.py::test_low_confidence_excluded` | Pending |
| (d) `mw_score < 0.4` excluded | Unit | `test_revisit_policy.py::test_low_mw_excluded` | Pending |
| (e) `is_deprioritized=true` excluded | Unit | `test_revisit_policy.py::test_deprioritized_excluded` | Pending |
| (f) `status='stale'` excluded | Unit | `test_revisit_policy.py::test_stale_excluded` | Pending |
| (g) Sticky-deprioritize: deprioritize-then-positive-outcomes does NOT re-schedule | Integration | `test_int_f20_sticky_deprioritize.py::test_deprioritize_sticky_across_outcome_recovery` | Pending |
| `populate_initial_schedules` populates NULL `next_review_at` rows | Integration | `test_int_f20_populate.py::test_initial_population` | Pending |
| `populate_initial_schedules` skips ineligible units | Integration | `test_int_f20_populate.py::test_skips_ineligible` | Pending |
| `memex_get_due_for_review` returns due units | Integration | `test_int_f20_get_due.py` | Pending |
| `memex_memory_review('again')` decrements MW + advances schedule | Integration | `test_int_f20_review.py::test_again_path` | Pending |
| `memex_memory_review('good')` increments MW + advances schedule | Integration | `test_int_f20_review.py::test_good_path` | Pending |
| Scheduler daily tick fires + scopes per vault | Integration | `test_int_f20_scheduler.py` | Pending |
| Tool descriptions match §4 verbatim | Unit | `test_int_f20_mcp.py::test_descriptions` | Pending |
| CLI `memex review due` + `memex review complete` smoke | CLI | `test_cli_review.py` | Pending |
| Hermes briefing surfaces "N due for review" | LLM-turn | `test_int_f20_hermes_briefing.py` | Pending |
| Real-LLM-turn validation drives review verbs | LLM | `test_int_f20_llm_turn.py` (`@pytest.mark.llm`) | Pending |
| Vault-scoping invariant: counters update only on `MemoryUnit` (not global `Entity`) | Integration | `test_int_f20_vault_scoping.py` | Pending |

## Decision

**Adopt comprehensive eligibility predicate** (per team-lead C1 lean adjudication + sign-off, 2026-04-30):
- Five hard predicates: `intent_class ∈ {permanent, durable}` AND `status='active'` AND `is_deprioritized=false` AND `confidence >= 0.5` AND `mw_score >= 0.4`.
- Sticky-deprioritize: F20 does NOT auto-resurface deprioritized units; reversal via `memex_memory_restore`.
- **Sensitive-unit override DEFERRED**: design freezes WITHOUT it. If PO ratifies post-Phase-1, addition is one config line in Phase 2; if PO defers to F6 governance lint, the TBD clause is dropped from AC-F20-3 entirely. Convergence does not block on PO resolution.
- **Sticky-deprioritize canonicalized as a Behavior section**: `is_deprioritized=false` is a hard gate cleared ONLY by `memex_memory_restore`; positive outcomes never auto-restore. Test (g) is the canary.
- POC required for FSRS reference parity; certainty 93% pre-POC, target 95% post-POC. POC outcome (`pocs/003-f20-fsrs-parity/paper-cross-check.md`) established that py-fsrs 4.1.2 implements FSRS-5, not FSRS-4.5 — captured in ADR-006 and reflected throughout this RFC.

## Review Comments

### staff-eng-1 — round 2
Proposed 4-factor combined score. Conceded after QA+PO leaned `mw_score`-alone with hard floors on the other dimensions (auditability per dimension > combined opacity).

### team-lead — 2026-04-30 (C1 + PO must-fix + sign-off)
PO must-fix: predicate must include `is_deprioritized=false` + `status='active'` + sticky-deprioritize sentence. AC-F20-3 patched in requirements.md to bake in the design. Sensitive-override DEFERRED — RFC-014 freezes without it; one-line addition in Phase 2 if PO ratifies, dropped entirely if PO defers to F6 governance lint. Convergence does not block on PO resolution.

### QA — 2026-04-30 (post-AC-patch verification)
Verified the patched AC-F20-3 against the live schema:
- `confidence` constraint `0.0 ≤ confidence ≤ 1.0` at `sql_models.py:670`; covering index at `:683` — `confidence >= 0.5` floor is index-friendly.
- Eligibility predicate `(intent_class, status, is_deprioritized, confidence, success_co_count, failure_co_count)` is fully index-coverable; daily-tick scheduler query needs no new index.
- Default `confidence=1.0` at `:597` confirms cold-start passes the floor.
- Calibration of the 0.5 floor is correct: `services/contradiction/engine.py` is the only downward mutator; a single contradiction event will not drop a unit below 0.5, but sustained contradiction will. Threshold stays.
- Flagged that the prose should explicitly state the sticky-deprioritize independence dimension (positive outcomes never auto-restore even if confidence and mw_score recover) so a future F20 dev does not introduce an auto-restore branch. This RFC's "Behavior — sticky-deprioritize is unconditional" section is that documentation; test (g) is the canary.

QA also withdrew their prior 5-WS lean — the 6-WS partition with bounded WS-shared seed-PR mechanism is the converged engineering call.
