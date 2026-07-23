eval: mental-model-version-history

**Definition of Done:** each reflection refresh appends a `mental_model_versions` row
holding the PRIOR `observations`/`entity_metadata` before the compare-and-swap
`UPDATE` (and before evidence-prune deletion), inside the same transaction; the CAS
`version` guard is preserved (a concurrent mismatch writes no version and loses no
update); and the history is readable.

Scoring policy: all rows are deterministic assertions on persisted DB state against
testcontainer Postgres, at a hard 100% bar. The CAS-guard row is the load-bearing
guardrail — a snapshot committed outside the CAS transaction would create phantom
versions.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** A reflection refresh preserves the prior observations | Refresh a mental model whose `observations` were `O1`, producing `O2` | A `mental_model_versions` row holds `O1` with a monotonic `version` and `change_type=reflected`; the live row now holds `O2` | Deterministic: assert the prior-observations row exists and the live row is `O2` | 100% |
| **[GUARDRAIL — CAS]** A version mismatch writes no version and loses no update | Attempt the finalize `UPDATE` with a stale `claimed_version` (concurrent bump) | The CAS `UPDATE` does not apply; NO `mental_model_versions` row is written; the live row is unchanged (no lost update, no phantom version) | Deterministic: assert no new version row AND live row unchanged | 100% |
| Prune captures the pre-prune observations, including prune-to-zero | Prune stale evidence from a model; separately prune a model down to zero observations | A version row holds the pre-prune `observations`; the prune-to-zero case writes the final version row BEFORE the mental-model row is deleted | Deterministic: assert the pre-prune snapshot exists in both cases | 100% |
| History is readable | `memex_mental_model_versions(id)` and `memex_mental_model_at_version(id, v)` | Versions returned ordered; the at-version read returns the historical `observations` | Deterministic: assert ordering and historical content | 100% |
