eval: retrieval-provenance-trace

**Definition of Done:** every `memex_memory_search` persists exactly one
append-only `retrieval_trace` row (query text, ordered `{unit_id, rank, score}`,
timestamp, resolved vault scope, session id) and returns a `trace_id` that
`get_retrieval_trace` round-trips — while no counter/confidence/deprioritization/
consolidation code moves, no credit-assignment module is created, and survey /
note_search emit no trace.

Scoring policy: all rows are deterministic assertions (on persisted DB state, on
the wire response, or on the diff) at a hard 100% bar. Rows tagged **[GUARDRAIL]**
protect an invariant the later credit-assignment slices depend on and must pass
100%.

Fork-dependent rows are written against the planner's §11 recommendation (the
default the team lead directed). If the operator settles a fork differently,
re-pin the marked row:
- **Q2 (surfacing mechanism) → recommended: nullable per-unit DTO/MCP field.**
  Row 4 assumes `trace_id` rides a field on each returned unit. If the operator
  chooses a header or a synthetic system-hint unit, re-pin row 4's Scorer.
- **Q3 (record_outcome consumption) → recommended: DEFER.** There is deliberately
  NO row asserting `record_outcome` consumes a `trace_id`; consumption is a
  follow-up slice. If the operator pulls consumption into this slice, add a row.
- **Q4 (retention policy) → recommended: age-based TTL delete.** Row 9 assumes an
  age threshold. If the operator chooses a per-vault row cap, re-pin row 9's Input
  and Expected to the cap semantics.
- **Q5 (multi-vault vault recording) → recommended: record the resolved scope;
  first slice may be single-vault only.** Row 6 asserts isolation for a
  single-vault search; multi-vault scope recording is NOT asserted here and is
  deferred with the fork.
- **Q6 (sync vs background write) → recommended: synchronous best-effort.** Row 4
  assumes the `trace_id` is persisted before the response returns (so the
  round-trip read cannot race the write).

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| A memory search leaves a provenance trace of what it returned | `memex_memory_search(query="how did we rotate prod db creds", vault=V)` returning N units | Exactly one `retrieval_trace` row exists for the call carrying the query text, an ordered `units` payload of `{unit_id, rank, score}` for all N returned units, a `created_at` timestamp, vault `V`, and the request session id | Deterministic: assert one row; assert its `query`, `vault_id`, `session_id`, `created_at` populated; assert `units` unit_ids == the returned set | 100% |
| **[GUARDRAIL — write-once]** One trace row per query, never one per unit | A `memex_memory_search` returning 12 units | Exactly ONE new `retrieval_trace` row (not 12); its `units` array has length 12 | Deterministic: assert `count(rows added) == 1` AND `len(row.units) == 12` | 100% |
| **[GUARDRAIL — chokepoint scope]** Only memory_search traces; survey and note_search do not | Run `memex_survey(query=...)` and `memex_note_search(query=...)`, each returning results | No `retrieval_trace` row is written by either call | Deterministic: assert `count(retrieval_trace rows) == 0` after each of the two calls | 100% |
| `trace_id` reaches the agent and resolves back to the stored trace *(Q2-dependent: field surfacing; Q6-dependent: sync write)* | `memex_memory_search(query=...)` then `get_retrieval_trace(trace_id)` using the id from the response | The search response carries a non-null `trace_id` (identical across all returned units); `get_retrieval_trace(that_id)` returns the persisted row for the same query and unit set | Deterministic: assert response `trace_id` is non-null and equal on every unit; assert `get_retrieval_trace` returns a row whose `id == trace_id` and `units` match | 100% |
| Rank is captured reliably; score is captured when present, null when absent | Offline: an ordered list of unit-like objects, some with a `.score`, some without, fed to the trace payload builder | Payload is `[{unit_id, rank, score}, ...]` with 0-based ranks in list order; `score` is the object's score where present and `null` where absent | Deterministic offline unit test (root `tests/`), parametrized | 100% |
| **[GUARDRAIL — vault isolation]** A trace never leaks across vaults *(Q5-dependent: single-vault scope)* | Write a trace in vault A, then attempt `get_retrieval_trace(trace_id)` scoped to vault B | The vault-B-scoped read does not return vault A's trace (404 / None); the row stays readable only under vault A | Deterministic: assert vault-B read is empty/404 AND vault-A read returns the row | 100% |
| **[GUARDRAIL — no scope creep]** No credit-assignment or scoring code is touched | The implementation diff for this ticket | No change to `services/outcomes.py` counter arithmetic (`_bump_counter` / `compute_mw_*`), `services/deprioritize_score.py`, `services/consolidation.py`, or `memory/confidence.py`; files `credit_assignment.py` / `confidence_update.py` do NOT exist | Deterministic: `git diff` touches none of those symbols/files AND `test ! -e` for the two modules | 100% |
| **[GUARDRAIL — best-effort]** A trace-write failure never breaks search | `memex_memory_search` where the trace insert is forced to raise | The search still returns its ranked units to the caller (the failure is logged, not propagated); no 5xx from the trace path | Deterministic: assert search response returns the expected units AND status is success despite the injected trace-write error | 100% |
| Traces do not grow unbounded *(Q4-dependent: age-based TTL)* | Seed traces older than the retention threshold and some newer than it, then run the prune task | Rows past the threshold are deleted; rows within it are retained | Deterministic: assert old rows gone AND recent rows present after prune | 100% |
| **[GUARDRAIL — gate visibility]** The loop-visible tests actually exercise the change | `just test` (root `tests/`, `-m 'not integration'`) and the integration tier | The offline tests live in root `tests/test_retrieval_provenance_trace.py` and pass under `just test`; `tests/test_e2e_retrieval_provenance_trace.py` exists, is `@pytest.mark.integration`, and passes under `-m integration` | Deterministic: `just test` green AND the integration file present and green under `-m integration` | 100% |
