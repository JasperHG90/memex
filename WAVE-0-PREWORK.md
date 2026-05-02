# Wave 0 Pre-Work: Adversarial Finding Resolution & Prerequisite Audit

**Date:** 2026-04-29
**Scope:** Systematic verification of all adversarial review findings (C1–C6, M1–M7, Mn1–Mn8) against current source, plus resolution of the four pre-implementation blockers identified in the backlog review: MW cold start, archive semantics, retrieval parameterization scope, and F1 splitting strategy.
**Status:** Ready for implementation planning

---

## Executive summary

Of the 21 adversarial findings, **5 are resolved as-stated in v6.7**, **6 require code changes before Wave 1 can start**, **7 are real but manageable (address during the relevant feature wave)**, and **3 are documentation-only fixes**. No finding blocks the start of Wave 1 outright, but three specific items must ship before F1's first PR:

1. **C3** — The retrieval layer has **12 `status == ACTIVE` hardcodes** (not the 8 cited in the report). `include_stale` and `include_superseded` use *different mechanisms* (SQL WHERE filter vs post-hydration threshold). `include_deprioritized` needs its own mechanism and must touch all 12 sites.
2. **M3** — `archive` is confirmed destructive on the entity graph (`_deactivate_note_units` → `prune_stale_evidence` can delete MentalModels) and confirmed accumulating on disk (FileStore bytes never removed). **Resolved in §2 below**: the destructive cascade is intentional cleanup; F4's `memory_deprioritize` (non-destructive boolean flag, no cascades) is the designated non-destructive verb. Two distinct verbs, not one ambiguous one. F4's tool description must contrast the two; no code change to archive.
3. **Mn4** — MW cold start (`0/0`) is undefined. A bootstrap heuristic must be specified before the F1 schema migration.

Additionally, the F1 feature should be split into **three sub-PRs** (schema+API, retrieval scope, MW composition) to isolate the riskiest change (retrieval-layer surgery) behind its own test gate.

---

## Finding resolution table

| # | Finding | Verified? | Resolution | Blocks Wave 1? | Owner wave |
|---|---------|-----------|------------|-----------------|------------|
| C1 | Entity line reference wrong; MW-on-Entity breaks multi-tenancy | **Partially** — line 701 is correct for `Entity`; v6.7 already specifies vault-scoped tables (`UnitEntity`, `MentalModel`, `MemoryUnit`). But `MemoryUnit.access_count` (line 554) is dead code — no write-site found. | Report already correct. Add `access_count` dead-code cleanup to F1 scope. | No | F1 |
| C2 | `deprioritized` status doesn't exist; `ContentStatus` only has ACTIVE/STALE | **Confirmed.** v6.7 correctly chose a separate `is_deprioritized` boolean column instead. No action needed — report v6.7 already resolved this. | Already resolved in v6.7. | No | N/A |
| C3 | Retrieval pipeline hardcodes `status == ACTIVE`; `include_superseded` is NOT a SQL filter — it's post-hydration threshold. `include_deprioritized` needs its own mechanism. | **Confirmed and worse than reported.** 12 hardcodes (not 8) in strategies.py + document_search.py. `include_superseded` is a *confidence threshold filter* after hydration (engine.py:450–452), not a SQL WHERE. `include_stale` IS a SQL WHERE parameterized through `apply_generic_filters`. These are two different patterns. `include_deprioritized` needs to decide which pattern it follows. | **Must specify before F1 implementation.** See §3 below. | **Yes — must spec before F1b** | F1b |
| C4 | No agent-level session/episode boundary; `session_id` ContextVar is per-request | **Confirmed.** `context.py:9-29` — `_session_id_ctx` defaults to `'global'`, set per-request via `set_session_id`. No span across retrieve→write. v6.7 already chose explicit feedback API (`memex_record_outcome`) instead of implicit attribution. | Already resolved in v6.7. No code change needed. | No | N/A |
| C5 | Scheduler uses single global advisory lock, not per-task locks | **Confirmed.** `scheduler.py:18` — `MEMEX_LEADER_LOCK_ID = 5432789123456789`. `run_scheduler_with_leader_election` acquires it and runs ALL `@clock.task`s as leader. v6.7 correctly specified: lint registers under existing leader. | Already resolved in v6.7. New tasks register under `MEMEX_LEADER_LOCK_ID`. | No | N/A |
| C6 | `prune_stale_evidence` parameterized for *deleted* units, not stale/deprioritized ones | **Confirmed.** `mental_model_cleanup.py:21-26` — `prune_stale_evidence(session, entity_ids, deleted_unit_ids, vault_id)`. v6.7 correctly specified that `memory_deprioritize` does NOT call `prune_stale_evidence`. | Already resolved in v6.7. F4 must not cascade to this function. | No | F4 |
| M1 | MW formula composition site doesn't match existing pipeline | **Partially confirmed.** The pipeline is: RRF (DB) → hydrate → superseded threshold filter → cross-encoder reranker → recency×temporal boost (line 1140) → MMR. MW soft-factor would compose at line 1140 alongside recency/temporal. This is the right composition site. But the report's formula `final_rank = RRF × sigmoid(MW − threshold)` is wrong — RRF runs in DB, reranker overwrites order. Correct formula: `final_score = sigmoid(reranker_raw) × recency_boost × temporal_boost × sigmoid(MW − threshold)` applied per-unit after reranker. | **Formula correction needed in F1c spec.** See §4 below. | No (design clarification) | F1c |
| M2 | `Entity.retrieval_count` and hybrid score overlap with MW counters | **Confirmed.** `entities.py:90-94` — `0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality`. MW counters on `UnitEntity`/`MentalModel` are orthogonal (per-unit behavioral signal vs per-entity popularity signal). `retrieval_count` on `Entity` is incremented on retrieval; `success_co_count` on `UnitEntity` is incremented on outcome recording. Different signals, different tables. | Document the relationship: Entity hybrid score is for entity ranking; MW on UnitEntity is for unit retrieval ranking. No migration needed. | No | F1 (documentation) |
| M3 | `set_note_status('archived')` is destructive (deletes MentalModels) and accumulates disk | **Confirmed.** `_deactivate_note_units` (notes.py:279-326) marks units stale + calls `prune_stale_evidence` which can `session.delete(model)` (mental_model_cleanup.py:77). `delete_note` (notes.py:943) removes FileStore bytes, but archive does NOT. | **Resolved (final v6.9):** keep destructive cascade as intentional; F4 `memory_deprioritize` is the non-destructive verb. Two distinct verbs, no code change. See §2. | No (decision only; F4's prompt-text contrasts the verbs) | F4 (prompt-text only) |
| M4 | Reflection uses process-local `asyncio.Lock`, not distributed | **Confirmed.** `reflection.py:55` — `self._reflection_lock = asyncio.Lock()`. Only serializes within one process. Multi-worker uvicorn can race on same entity. | Accept for v1 (same as report). F9 introduces per-entity advisory lock. Document limitation. | No | F9 |
| M5 | Heavy test coverage on affected modules | **Confirmed.** 12+ integration test files and dozens of unit tests touch `MemoryUnit`, retrieval, and status semantics. | Add test-impact analysis to each F1 sub-PR. | No | F1a/b/c |
| M6 | `ContradictionEngine.detect_contradictions` takes `unit_ids`, not entities | **Confirmed.** `contradiction/engine.py:39-45` — signature is `(session_factory, document_id, unit_ids, vault_id)`. F9 must include entity→unit_ids resolver step. | Already acknowledged in v6.7 F9 spec. | No | F9 |
| M7 | `reflection.py:29` is a class, not a function | **Confirmed.** Line 29 is `class ReflectionService:`. The actual method is `reflect()` at line 88. | Line reference fix. Documentation only. | No | N/A |
| Mn1 | `schemas.py:323` is `MemoryUnitBase`, not `MemoryUnitDTO` | **Confirmed.** `MemoryUnitBase` is at line 323, `MemoryUnitDTO` at line 354. | Line reference fix. Documentation only. | No | N/A |
| Mn2 | `reflection.py:69` is class declaration, not method | **Confirmed.** Line 69 is inside `ReflectionService.background_reflect`. `reflect_on_entity` at line 543 is marked "Legacy wrapper." | Documentation only. | No | N/A |
| Mn3 | `mental_model_cleanup.py:17` is import, function at line 21 | **Confirmed.** Line 17 is `from memex_core.memory.reflect.queue_service import ReflectionQueueService` (TYPE_CHECKING import). | Documentation only. | No | N/A |
| Mn4 | MW cold start: `0/0` is undefined; no backfill plan | **Confirmed and blocking.** `access_count` (line 554) exists but is never incremented — dead column, no write-site found anywhere. Existing units have `access_count = 0`, which means MW cold start is `0/0 = undefined`. | **Must spec before F1a.** See §5 below. | **Yes — must spec before F1a** | F1a |
| Mn5 | No observability spec for MW/soft-factor | Valid. Report §3.3 says "emits Prometheus metrics" but no metrics are specified. | Add to F1 spec: `memex_outcome_recorded_total`, `memex_mw_score_distribution` histograms. | No | F1 |
| Mn6 | Selective citation of ZenBrain; KinthAI is anonymous comment | Valid concerns. Report v6.2 already corrected FSFM framing. KinthAI is treated as design intuition, not evidence. | Already addressed in v6.7. No action needed. | No | N/A |
| Mn7 | `Entity.retrieval_count` already provides a one-counter analog | **Partially confirmed.** `retrieval_count` is incremented per retrieval; `access_count` on MemoryUnit is dead. But MW requires *two* counters (success co-occurrence + failure co-occurrence), which `retrieval_count` cannot provide. MW §3.4's point stands: one counter conceals mixed-outcome contexts. | Document that `retrieval_count` is entity-level popularity, MW is unit-level behavioral signal. Different tables, different granularity, different semantics. | No | F1 (documentation) |
| Mn8 | `access_count` exists but is never incremented | **Confirmed.** `sql_models.py:554` defines it, `extraction/storage.py:73` initializes it to 0, but no code ever increments it. It is dead. | Add cleanup to F1a: either wire `access_count` as a retrieval counter (lighter alternative to MW for v0.5) or remove it to avoid confusion. | No | F1a |

---

## §1. Resolved findings (no action needed)

These were correctly handled in v6.7 and require no further work:

- **C2** — `is_deprioritized` boolean column (not enum extension) is the right design.
- **C4** — Explicit `memex_record_outcome` API replaces implicit session attribution.
- **C5** — Lint registers under existing `MEMEX_LEADER_LOCK_ID`.
- **C6** — `memory_deprioritize` explicitly avoids `prune_stale_evidence`.
- **M4** — Process-local lock accepted for v1; F9 adds distributed locking.
- **M6** — Entity→unit_ids resolver acknowledged in F9 spec.
- **Mn6** — KinthAI treated as design intuition; FSFM framing corrected in v6.2.

---

## §2. Archive semantics (blocking F4)

**Current behavior (verified):**

```python
# notes.py:244-245
elif status == 'archived':
    await self._deactivate_note_units(session, note_id, note_vault_id)
```

`_deactivate_note_units` (notes.py:279-326):
1. Marks all linked MemoryUnits as `stale`
2. Collects entity IDs from those units
3. Calls `prune_stale_evidence(session, entity_ids, unit_ids, vault_id)`
4. `prune_stale_evidence` can `session.delete(model)` for MentalModels with empty observations (mental_model_cleanup.py:77)

**Disk side:** `set_note_status('archived')` does NOT touch FileStore. Only `delete_note` calls `txn.delete_file(doc.filestore_path, recursive=True)`.

**The apparent problem:** Archive looks *simultaneously* too destructive (entity graph cascade) and not destructive enough (disk preserved). F4's `deprioritize` is designed to be fully non-destructive (boolean flag, no cascades).

**On further review (final v6.9 position):** the cascade isn't a problem — it's the intended behavior. The two-verb design that makes this coherent: **archive = "I'm done; clean up the graph too"; deprioritize = "keep around, lower the weight."** F4 was specifically greenlit as the non-destructive curation verb; if archive is also non-destructive, F4 is redundant. Mental models whose only evidence is archived notes are epistemically empty — `prune_stale_evidence` deleting them is correct, not collateral damage. The "MentalModel deletion is irreversible" framing was real but the right mitigation is **bulk-archive human-gating** (≥10 notes), not weakening the single-archive verb. The disk-side asymmetry (FileStore preserved on archive) is a separate Tier B concern.

**Recommendation — three options:**

### Option A: Make archive non-destructive on the entity graph (originally recommended; reversed)

Change `_deactivate_note_units` to NOT call `prune_stale_evidence` when the trigger is `archive` (only call it when the trigger is `supersede`). Archive still marks units `stale`, but MentalModels are preserved (their evidence now points to stale units, which is informational). Add a separate `prune_stale_evidence` call to a maintenance cron if desired.

```python
async def _deactivate_note_units(self, session, note_id, vault_id, cascade_to_models=True):
    # ... existing stale-marking code ...
    if cascade_to_models:
        affected_entity_ids = await prune_stale_evidence(session, entity_ids, unit_ids, vault_id)
        # ... queue reflection ...
```

Then in `set_note_status`:
```python
elif status == 'archived':
    await self._deactivate_note_units(session, note_id, note_vault_id, cascade_to_models=False)
elif status == 'superseded':
    await self._deactivate_note_units(session, note_id, note_vault_id, cascade_to_models=True)
```

**Pro:** Consistent with non-destructive principle. Archived notes' MentalModels still exist but reference stale units. **Con:** Stale units in MentalModel observations may confuse retrieval. Mitigate by having `apply_generic_filters` exclude stale units from evidence resolution (already done — stale units are filtered out of retrieval).

### Option B: Keep archive destructive, document it explicitly (final v6.9 choice)

Update the operation taxonomy:
- Archive: **Destructive on entity graph** (MentalModels can be deleted). Human-gate required.
- Add FileStore cleanup to archive so it's consistently destructive on both axes.

**Pro:** No code change. **Con:** Inconsistent with F4's non-destructive principle. Archive becomes the "hard delete light" operation.

### Option C: Make archive truly non-destructive (preserve units AND MentalModels)

Don't mark units stale at all. Just set `note.status = 'archived'` and exclude archived notes from retrieval by adding `note.status != 'archived'` to retrieval filters.

**Pro:** Cleanest semantics. **Con:** Requires adding note-status filters to all retrieval strategies (more WHERE clauses, same C3 scope). Archived notes still participate in entity resolution and MentalModel evidence.

**Original recommendation: Option A.** It requires minimal code change (one parameter), preserves the existing `supersede` behavior, makes archive consistent with the non-destructive principle, and stale units are already excluded from retrieval.

**Final recommendation (revised): Option B — keep current destructive-cascade behavior; document it explicitly; F4 (`memory_deprioritize`) is the designated non-destructive verb.** Reasoning:

1. **F4's existence is the strongest argument.** F4 was greenlit specifically as the non-destructive curation verb (boolean flag, no cascades). If archive *also* avoided cascades, F4 would be redundant. The cleaner design is **two distinct verbs with distinct intents**: archive = destructive cleanup; deprioritize = non-destructive downweight.
2. **Empty MentalModels from archived notes are noise, not signal.** A synthesis whose only evidence is archived units is epistemically empty. `prune_stale_evidence` removing it is the correct cleanup, not collateral damage.
3. **The bulk-archive blast-radius concern is addressed elsewhere.** Single-note archive's blast radius is small (one note's evidence at most); bulk archive (≥10 notes) keeps human-gating precisely because the cascade is destructive. Weakening single-archive to mitigate bulk-archive's risk is the wrong layer to fix it.
4. **No code change required.** The original Wave 0 task ("pass `cascade_to_models=False` for archive") is dropped. F4's prompt-text and `memory_deprioritize` tool description must explicitly contrast with archive: *"deprioritize when the unit should remain queryable but ranked lower; archive when the note is genuinely done and the synthesized models built from it should be cleaned up too."*

**Restoration path** for the rare archive-then-undo case: re-ingest the note + re-reflect. Synthesis is derivable from evidence; it's not load-bearing state worth preserving "just in case."

---

## §3. Retrieval parameterization scope (blocking F1b)

### 3.1 Current status filter inventory

*Note: §3.3 below resolves the 12-site count to 4 effective change surfaces — `apply_generic_filters` centralizes the 8 in-scope strategy sites; `document_search.py` (4 sites) and `engine.py:1357` are explicitly excluded.*

I found **12 hardcoded `status == ACTIVE` filters** across two files:

**`strategies.py` (8 sites):**
1. Line 90 — `apply_generic_filters`: `if not include_stale: WHERE status == ACTIVE`
2. Line 238 — `SemanticStrategy._build_semantic_seed_cte`: `WHERE status == ACTIVE` (no `include_stale` override)
3. Line 721 — `EntityCooccurrenceNoteGraphStrategy`: `if not include_stale: WHERE Chunk.status == ACTIVE`
4. Line 763 — `EntityCooccurrenceNoteGraphStrategy` second order: `if not include_stale: WHERE Chunk.status == ACTIVE`
5. Line 980 — `CausalNoteGraphStrategy`: `if not include_stale: WHERE Chunk.status == ACTIVE`
6. Line 1004 — `CausalNoteGraphStrategy` expansion: `if not include_stale: WHERE Chunk.status == ACTIVE`
7. Line 1210 — `LinkExpansionNoteGraphStrategy`: `if not include_stale: WHERE Chunk.status == ACTIVE`
8. Line 957 — `CausalNoteGraphStrategy` (MemoryUnit-level): uses `apply_generic_filters`

**`document_search.py` (4 sites):**
9. Line 341 — `Node.status == 'active'` in skeleton-tree node fetch
10. Line 482 — `Node.status == 'active'` in keyword CTE
11. Line 638 — `Chunk.status == 'active'` in summary fetch
12. Line 660 — `Node.status == 'active'` in text-fetch fallback

**`engine.py` (1 site):**
13. Line 1357 — Virtual unit construction uses `status=ContentStatus.ACTIVE`

### 3.2 The two existing patterns

**Pattern 1: `include_stale` — SQL WHERE parameterization (strategies.py)**

Passed via `kwargs` through `apply_generic_filters()`. This is the correct pattern for F1's `include_deprioritized`. Each strategy's `get_statement()` receives kwargs and the filter is applied at the SQL level.

**Pattern 2: `include_superseded` — Post-hydration confidence threshold (engine.py:450-452)**

```python
if not request.include_superseded:
    threshold = self.retrieval_config.superseded_threshold
    final_results = [u for u in final_results if getattr(u, 'confidence', 1.0) >= threshold]
```

This runs AFTER hydration and AFTER the reranker. It doesn't affect which units are fetched from DB — it only filters the final result list. This is appropriate for superseded units because they have lower confidence but are still active.

### 3.3 Recommendation for `include_deprioritized`

**Use Pattern 1 (SQL WHERE) for `is_deprioritized`.** Reason: deprioritized units should be excluded at the DB level for default queries (performance + correctness). Post-hydration filtering would mean deprioritized units consume reranker budget before being filtered out.

Implementation approach:

```python
# In apply_generic_filters (strategies.py):
include_deprioritized = kwargs.get('include_deprioritized', False)
if not include_deprioritized:
    statement = statement.where(col(MemoryUnit.is_deprioritized) == False)
```

This adds exactly **one WHERE clause** per strategy call, applied through the existing `apply_generic_filters` function that's already called by every strategy. The `include_stale` pattern proves this works.

**For document_search.py** (4 sites): These filter on `Chunk.status` and `Node.status`, not `MemoryUnit.status`. Deprioritization is a `MemoryUnit` property, not a `Chunk`/`Node` property. The document_search path would need a JOIN to `MemoryUnit` to check `is_deprioritized`, which is a more invasive change. **Recommendation:** defer document_search parameterization to F1b-v2 or F32. Document search's primary use case (source drill-down) should show deprioritized content with status metadata per §3.6 of the report.

**For `engine.py:1357`** (virtual unit construction): Virtual units from MentalModels should NEVER be deprioritized. No change needed.

**Total scope for F1b:** 8 strategy sites parameterized via `apply_generic_filters` + 1 `engine.py` request model change + 1 server-level parameter pass-through. Not 12+ rewrites — the `apply_generic_filters` function centralizes this.

---

## §4. MW soft-factor composition (F1c design clarification)

### 4.1 Correct pipeline order (verified from source)

```
1. Query expansion (multi-query)
2. Embeddings
3. RRF (in-DB) → ranked candidate IDs
4. Hydrate objects from DB
5. Superseded threshold filter (post-hydration, confidence-based)
6. Cross-encoder reranker → sigmoid-normalized scores [0,1]
7. Recency boost × temporal boost → boosted_scores (line 1140)
8. Position-aware blending (optional)
9. MMR diversity filtering
10. Attach citations
```

### 4.2 Correct composition formula

The report's formula `final_rank = RRF × sigmoid(MW − threshold)` is incorrect because RRF runs in-DB and the reranker overwrites RRF order.

Correct formula for the composition site at line 1140:

```python
# Current (engine.py:1140):
boosted_scores.append(ce_score * recency_boost * temporal_boost)

# After F1c:
mw_score = sigmoid(success_co_count / (success_co_count + failure_co_count + alpha + beta) - threshold)
boosted_scores.append(ce_score * recency_boost * temporal_boost * mw_boost)
# where mw_boost = 1.0 + mw_alpha * (mw_score - 0.5)
```

**Why additive-on-multiplicative, not pure multiplicative:**

A pure `× sigmoid(MW − threshold)` would zero out units with no outcome data (MW = 0/0 → sigmoid(−threshold) ≈ 0.1). This is the cold-start problem. Instead, use an **additive-marginal** approach:

```python
mw_boost = 1.0 + mw_alpha * (mw_score - 0.5)
```

Where:
- `mw_score = 0.5` (Beta-Bernoulli prior with α=β=1) → `mw_boost = 1.0` (neutral)
- `mw_score > 0.5` (mostly successful) → `mw_boost > 1.0` (boost)
- `mw_score < 0.5` (mostly failing) → `mw_boost < 1.0` (downweight, but never zero)
- `mw_alpha` is a tunable parameter (default: 0.3, same magnitude as recency_alpha and temporal_alpha)

This preserves the composition model from §3.4 while avoiding the cold-start zeroing problem.

---

## §5. MW cold start (blocking F1a)

### 5.1 The problem

New MemoryUnits have `success_co_count = 0` and `failure_co_count = 0`. The MW ratio is `0/0 = undefined`. With the additive-marginal formula above, this resolves to `mw_score = 0.5` (Beta-Bernoulli prior), giving `mw_boost = 1.0` (neutral). This is correct behavior — new units should not be penalized or boosted.

However, `MemoryUnit.access_count` (line 554) is dead code — never incremented. This means there's no existing "times retrieved" counter to bootstrap from.

### 5.2 Bootstrap strategy

**Recommended: Use Beta-Bernoulli prior as the sole cold-start mechanism.**

- New units: `success_co_count = 0`, `failure_co_count = 0` → `mw_score = 0.5` → `mw_boost = 1.0` (neutral)
- After first outcome: `mw_score` moves away from 0.5 based on actual signal
- No backfill needed — the prior is intentionally neutral
- `access_count` remains dead code; it's not needed for MW computation

**Alternative (rejected):** Bootstrap `success_co_count = retrieval_count` from Entity. This would conflate entity-level popularity with unit-level behavioral signal and leak cross-tenant data (Entity is global, MW should be vault-scoped).

### 5.3 Dead code cleanup

`MemoryUnit.access_count` (line 554) and its index (line 634) should be either:
- **Removed** in F1a's migration (preferred — eliminates confusion), or
- **Wired** as a retrieval counter (incremented on each `memex_memory_search` that returns the unit)

Recommendation: **remove it.** MW provides a richer behavioral signal. If a retrieval counter is needed later, it should be vault-scoped and outcome-aware, not a dead column.

---

## §6. F1 splitting strategy

### F1a: Schema migration + outcome API (~1.5 weeks)

- Alembic migration: add `success_co_count` (INTEGER, default 0), `failure_co_count` (INTEGER, default 0), `is_deprioritized` (BOOLEAN, default FALSE) to `MemoryUnit`; add same counters to `UnitEntity` and `MentalModel`
- Remove `access_count` column and index (dead code)
- `services/outcomes.py` — new module with `record_outcome()` function
- `memex_record_outcome` MCP tool
- API symmetry fixes: `reference_date` on `memex_note_search`, `after`/`before`/`reference_date` on `memex_survey`
- Integration tests for outcome recording
- Prometheus metrics: `memex_outcome_recorded_total` counter, `memex_mw_score_distribution` histogram

### F1b: Retrieval scope parameterization (~1.5 weeks)

- Add `include_deprioritized: bool = False` to `RetrievalRequest` model
- Extend `apply_generic_filters()` in `strategies.py` to add `WHERE is_deprioritized = FALSE` when `include_deprioritized=False`
- Pass parameter through `engine.py` → strategies → `apply_generic_filters`
- Add `include_deprioritized` to `memex_memory_search` MCP tool
- Do NOT change `document_search.py` in this PR (document search shows deprioritized content with metadata)
- Integration tests for scope filtering

### F1c: MW soft-factor composition (~1.5 weeks)

- Add MW boost to `engine.py:1140` using the additive-marginal formula (§4.2)
- Add `mw_alpha` config parameter (default 0.3)
- Load MW scores during hydration (batch query for `success_co_count`, `failure_co_count`)
- Integration tests verifying MW boost composes correctly with recency/temporal
- Benchmark: retrieval quality before/after on a test corpus

**Total F1: ~4.5 weeks** (vs. original 3-4w estimate — the extra week accounts for the parameterization scope and test impact)

---

## §7. Additional pre-work items

### 7.1 Line reference corrections for the report

| Report citation | Actual | Correction |
|---|---|---|
| `sql_models.py:46` for Entity | Line 701 | Correct in F1 spec |
| `schemas.py:323` for MemoryUnitDTO | Line 323 is MemoryUnitBase; line 354 is MemoryUnitDTO | Correct in F1 spec |
| `reflection.py:29` as function | Line 29 is `class ReflectionService:` | Correct in F5 spec |
| `mental_model_cleanup.py:17` as function | Line 17 is import; line 21 is `prune_stale_evidence` | Correct in F6/F9 spec |
| `engine.py:1116` as composition site | Correct line; correct location | No change needed |

### 7.2 `ContentStatus` enum

Current: `ACTIVE = 'active'`, `STALE = 'stale'` (sql_models.py:35-39).

F1 adds `is_deprioritized` as a **separate boolean column**, not a new enum value. This avoids:
- Alembic CHECK constraint migration
- Enum extension in Python
- Status-query complexity (`status=ACTIVE AND is_deprioritized=FALSE` vs `status IN (ACTIVE, DEPRIORITIZED)`)

This is the correct design. No action needed.

### 7.3 `apply_generic_filters` centralization

The `apply_generic_filters` function (strategies.py:78-92) is the correct injection point for `is_deprioritized`. It's already called by every strategy that produces MemoryUnit-level results. The note-level strategies (Chunk/Node filters in document_search.py) should NOT get this filter — they operate on different entities.

### 7.4 Hermes plugin surface

Current Stream-1 tools (8 tools, verified):
1. `memex_memory_search`
2. `memex_note_search`
3. `memex_survey`
4. `memex_add_note`
5. `memex_append_note`
6. `memex_list_entities`
7. `memex_get_entity_mentions`
8. `memex_get_entity_cooccurrences`

F1 adds: `memex_record_outcome` + `include_deprioritized` scope param on tools 1-2.
F4 adds: `memory_deprioritize`.
F5 adds: `memory_summarize_node`.

Coordinate with `packages/hermes-plugin` for each wave.

---

## §8. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Retrieval scope parameterization breaks existing queries | Medium | High (retrieval is core path) | F1b as separate PR with full integration test suite; `include_deprioritized=False` default means no behavior change until explicitly enabled |
| MW cold-start period produces flat ranking | High (first 1-2 weeks after deploy) | Low (neutral boost = no harm) | Accept neutral boost as correct behavior; document for users |
| Archive destructiveness confuses users | Medium | Medium | Document the two-verb design clearly: archive = destructive cleanup; F4 `memory_deprioritize` = non-destructive downweight. F4's tool description must explicitly contrast with archive. Bulk-archive (≥10 notes) human-gated. |
| F1b JOIN on `is_deprioritized` slows retrieval | Low | Medium | Column is boolean with DEFAULT FALSE; partial index `WHERE is_deprioritized = TRUE` keeps default-scope queries fast |
| MW boost interacts unexpectedly with MMR | Low | Medium | Add MW before MMR (current design); MMR operates on already-boosted scores; test with benchmark |

---

## §9. Summary of blocking items

| Item | Blocks | Action needed | Estimated effort |
|------|--------|--------------|-----------------|
| Archive semantics decision | F4 | **Resolved**: keep current destructive-cascade behavior (Option B); F4 `memory_deprioritize` is the non-destructive complement. No code change required; F4's prompt-text must contrast the two verbs. | 0 days (decision only; no code change) |
| MW cold-start spec | F1a | Document Beta-Bernoulli prior as cold-start; remove `access_count` | 0.5 day |
| Retrieval scope spec | F1b | Document `apply_generic_filters` approach; defer `document_search.py` changes | 0.5 day |
| F1 splitting | F1 execution plan | Confirm F1a/F1b/F1c split | 0 days (this doc is the spec) |
| Line reference corrections | Report accuracy | Update report citations | 0.5 day |

**Total Wave 0 effort: ~1.5 days** — specs + documentation updates only. The originally-planned archive code change (`cascade_to_models=False`) is dropped because the destructive cascade is now intentional (F4 `memory_deprioritize` is the non-destructive verb). Wave 1 can start immediately after Wave 0.
