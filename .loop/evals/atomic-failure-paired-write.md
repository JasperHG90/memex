eval: atomic-failure-paired-write

**Definition of Done:** a single failure-path operation records `not_helpful` and
deprioritizes the identical unit subset together; the success path is unchanged; a
mixed batch deprioritizes only the `not_helpful` subset; and the two underlying
primitives remain independently callable.

Scoring policy: all rows are deterministic assertions on persisted DB state at a hard
100% bar. Rows 1–4 are guardrails (the pairing, the atomicity, the mixed-batch subset,
the success-path asymmetry) and must pass 100%.

Fork-dependent rows are marked, written against the planner's recommendation:
- Q2 (single-transaction refactor) → row 2 assumes **true atomic rollback** (neither
  write persists on partial failure). If the operator chooses sequential-with-defined-
  failure, re-pin row 2 to that semantics.
- Q4 (observation-UUID target) → row 6 assumes **surface-and-rollback**.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Failure op stamps BOTH outcome and deprioritize on the same unit | Atomic failure op on unit `U` (verb `not_helpful`, reason "stale") | `OutcomeAuditLog` has a `not_helpful` row citing `U` AND `U` is deprioritized (surface state off) | Deterministic: assert both the outcome row exists AND `U.is_deprioritized == True` | 100% |
| **[GUARDRAIL — atomicity]** Partial failure persists neither write *(Q2-dependent: atomic rollback)* | Atomic failure op on `U` where the deprioritize step is forced to fail | Neither the outcome row nor the deprioritize is persisted — no partial state | Deterministic: assert `OutcomeAuditLog` has no new row for `U` AND `U.is_deprioritized == False` | 100% |
| **[GUARDRAIL — subset]** Mixed batch deprioritizes only the `not_helpful` units | Atomic op on batch `[U1: not_helpful, U2: helpful]` | `U1` deprioritized; `U2` NOT deprioritized; both receive their respective outcome rows | Deterministic: assert `U1.is_deprioritized == True` AND `U2.is_deprioritized == False` AND both outcome rows exist | 100% |
| **[GUARDRAIL — success asymmetry]** Success path never deprioritizes | `record_outcome(units=[{U, verb: helpful}])` (or the atomic op with only helpful verbs) | `U` gets a `helpful` outcome; `U` is NOT deprioritized | Deterministic: assert outcome row exists AND `U.is_deprioritized == False` | 100% |
| Orthogonality preserved — primitives still work standalone | Call `record_outcome` alone, then `memory_deprioritize` alone, on separate units | Each primitive behaves exactly as before this change (outcome-only; deprioritize-only) | Deterministic: assert each standalone call produces exactly its own single effect | 100% |
| Observation UUID surfaces 400 and rolls back *(Q4-dependent: surface-and-rollback)* | Atomic failure op targeting a virtual/observation unit UUID | Op returns HTTP 400 with `source_memory_units`; the `not_helpful` outcome is NOT left persisted | Deterministic: assert `status_code == 400` AND `'source_memory_units' in body` AND no dangling outcome row | 100% |
