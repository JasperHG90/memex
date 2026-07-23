eval: knowledge-rollback

**Definition of Done:** rolling back a note or mental model appends a NEW version
whose content equals a chosen prior version and applies it to the live row — history
is never shortened or deleted; a note rollback re-derives its downstream state
(Q4 default, pending user confirmation); and a rollback to a bad target returns a 4xx
rather than a silent no-op.

Scoring policy: all rows are deterministic assertions against testcontainer Postgres,
at a hard 100% bar. The append-only row is the load-bearing guardrail.

Fork-dependent row, written against the accepted default:
- Q4 (note rollback derived-state semantics) → row 2 assumes **re-ingest-and-
  re-extract** (derived units reflect the restored body). **PENDING USER
  CONFIRMATION.** If the user chooses body-only restore, re-pin row 2 to "derived
  units unchanged" and adjust R2 in the ticket.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Rollback is append-only; history is never shortened | A note with 3 versions; `memex_note_rollback(K, to_version=1)` | A 4th `note_versions` row appears (`change_type=rolled_back`); all 3 prior rows are byte-for-byte unchanged; the live body equals v1's content | Deterministic: assert version count 3→4, prior rows unchanged, live body == v1 | 100% |
| Note rollback re-derives downstream state *(Q4-dependent: re-ingest-and-re-extract — PENDING USER CONFIRMATION)* | Roll back a note whose newer body had produced different memory units | After rollback the derived memory units reflect the RESTORED (v1) body — extraction re-ran through the re-ingest path | Deterministic: assert the derived units match the restored body | 100% |
| Mental-model rollback restores prior observations under the CAS guard | `memex_mental_model_rollback(id, to_version=v)`; and a variant with a concurrent version bump | A new `mental_model_versions` row is appended and the live row holds `v`'s observations; under a concurrent bump the rollback fails cleanly with no lost update | Deterministic: assert restored observations + a new version row AND clean failure on the concurrent-bump variant | 100% |
| **[GUARDRAIL]** A bad target version returns 4xx, not a silent no-op | `memex_note_rollback(K, to_version=999)` (non-existent/future) | Returns HTTP 4xx (`ToolError`); no version row is written; the live row is unchanged | Deterministic: assert 4xx AND no new version AND live row unchanged | 100% |
