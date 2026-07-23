eval: memory-unit-usage-tracking-columns

**Definition of Done:** `MemoryUnit` gains three usage-tracking columns —
`access_count` (int, NOT NULL, default 0), `last_accessed` (timestamptz,
nullable), `importance_score` (float, nullable) — via a backward-compatible
alembic migration; a retrieval that returns a unit increments that unit's
`access_count` by exactly 1 and stamps `last_accessed`, written as one
batched, off-hot-path UPDATE, while `importance_score` gets no writer this
phase.

Scope decisions (locked as defaults per triage; §11 of the ticket):
- Q1 — keep the RFC name `importance_score` alongside the existing
  `importance` column (`sql_models.py:767`), disambiguated by docstrings on
  both. The two are different quantities.
- Q2 — columns live on the SQLModel `MemoryUnit` only; NOT on
  `MemoryUnitBase` / `MemoryUnitDTO` / any HTTP/MCP payload.
- Q3 — the internal `summarize_search_results` sub-query fan-out
  (`services/search.py:295`, discards the returned task) does NOT count as an
  access. Row 8 asserts this and is the one row that flips if Q3 is decided
  the other way.
- Q4 — no index on the new columns this phase (deferred to RFC #220 Phase 4,
  which adds the reader).

Scoring policy: deterministic assertions against the real metastore
(testcontainer) at a hard 100% bar. Every row is a guardrail — each protects
a column default, the read-path increment contract, or an invariant a later
RFC #220 phase will build on.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** A retrieved unit's access is counted | Ingest content yielding a memory unit U; run one retrieval (through the server path that schedules + drains the background task) whose results include U | U's row has `access_count == 1` | Deterministic: assert `U.access_count == 1` after the background task drains | 100% |
| **[GUARDRAIL]** A retrieved unit's `last_accessed` is stamped | Same single retrieval returning U | U's `last_accessed` is non-NULL and ≥ the retrieval start time | Deterministic: assert `U.last_accessed is not None` and `>= t_start` | 100% |
| **[GUARDRAIL]** Repeat retrievals increment by exactly 1 each | Run the same retrieval twice (two separate `recall` calls), each returning U, draining the task between | U's `access_count == 2` (not 3+, not 1) | Deterministic: assert `U.access_count == 2` | 100% |
| **[GUARDRAIL]** One call counts once despite query expansion | A single retrieval with `expand_query=True` so the engine fans out to multiple sub-query embeddings and RRF-fuses them, returning U once | U's `access_count == 1` — the bump is +1 on the distinct returned id, NOT +1 per expanded sub-query | Deterministic: assert `U.access_count == 1` after one expanded call | 100% |
| **[GUARDRAIL]** Un-retrieved units are untouched | Ingest units U (matches the query) and V (does not match); run a retrieval that returns only U | V's row is unchanged: `access_count == 0` and `last_accessed IS NULL` | Deterministic: assert `V.access_count == 0 and V.last_accessed is None` | 100% |
| **[GUARDRAIL]** `importance_score` has no writer this phase | Ingest a unit, then run several retrievals that return it | The unit's `importance_score IS NULL` throughout — no code path writes it | Deterministic: assert `U.importance_score is None` after ingest and after N retrievals | 100% |
| **[GUARDRAIL]** Migration is backward-compatible (default backfill) | Apply migration `070` to a `memory_units` table that already has rows (pre-migration semantics); read a pre-existing row | The pre-existing row reads `access_count == 0`, `last_accessed IS NULL`, `importance_score IS NULL` — no NOT-NULL violation, no rewrite failure | Deterministic: assert the pre-existing row's three column values after `upgrade()` | 100% |
| **[GUARDRAIL]** (Q3 default) Internal summarize fan-out does not count | Call `summarize_search_results` for a query whose internal `_search_one` sub-queries each return U (the path at `services/search.py:295` that discards the task) | U's `access_count` is NOT inflated by the number of sub-queries — the discarded task means these internal reads do not count | Deterministic: assert `U.access_count` did not increase by the sub-query multiple (0 delta from this path) | 100% |
