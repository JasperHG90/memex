eval: note-version-history

**Definition of Done:** every in-place note update appends a `note_versions` row
holding the PRIOR body before it is overwritten — on create (v1), on the
changed-content overwrite, and on append — while an identical-hash skip writes
nothing; version rows are append-only, monotonic per note, unique `(note_id,
version)`; and the history is readable via `memex_note_versions` /
`memex_note_at_version`.

Scoring policy: all rows are deterministic assertions on persisted DB state against
testcontainer Postgres, at a hard 100% bar. The guardrail rows defend the core
invariant — an update preserves the prior version instead of destroying it — and the
no-op-skip and idempotency asymmetries.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** A changed-content overwrite preserves the prior body | `add_note(note_key=K, content="A")` then `add_note(note_key=K, content="B")` | `note_versions` contains a row whose `content == "A"` with `change_type=edited` and a monotonic `version`; the live note body is `"B"` | Deterministic: assert the prior-body row exists with `change_type=edited` and live body `"B"` | 100% |
| **[GUARDRAIL]** Initial create writes version 1 | First `add_note(note_key=K, content="A")` | A `note_versions` row `version=1`, `change_type=created` | Deterministic: assert `version==1` and `change_type=created` | 100% |
| **[GUARDRAIL]** An identical-hash skip writes no version | Re-`add_note(note_key=K, content="A")` with unchanged content (hits the skip at `ingestion.py:429-431`) | No new `note_versions` row is created | Deterministic: assert the version count for `K` is unchanged | 100% |
| **[GUARDRAIL]** Append captures the pre-append body and stays idempotent | `append_note(K, delta="X")`, then replay the same `append_id` | A `note_versions` row holds the pre-append body with `change_type=appended`; the replayed `append_id` (`ingestion.py:756-777`) writes NO extra version | Deterministic: assert one appended-version row AND replay adds none | 100% |
| History is readable and ordered | `memex_note_versions(K)` and `memex_note_at_version(K, 1)` | `note_versions` returned ordered by `version`; `note_at_version(K,1)` returns content `"A"` | Deterministic: assert ordering and that version 1 content is `"A"` | 100% |
| **[GUARDRAIL]** Append-only and unique | Any sequence of updates to `K` | No two rows share `(note_id, version)`; no previously-written version row is ever modified or deleted | Deterministic: assert unique `(note_id, version)` and that earlier rows are byte-for-byte unchanged after later updates | 100% |
