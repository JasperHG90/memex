eval: knowledge-versioning-spike

**Definition of Done:** the spike produces a design of record that pins the exact
column schema for `note_versions` and `mental_model_versions`, enumerates the
`ChangeType` enum, records the migration-numbering decision, and documents the
settled fork decisions (Q2–Q7) — plus the two SQLModel classes added to
`sql_models.py` — without changing any runtime behavior (no migration, no capture
hooks, no read surface).

Scoring policy: deliverable-pinning rows for a design spike. Rows are deterministic
checks against the added classes / seed test and the design doc, at a hard 100% bar.
All substantive forks (Q1–Q7) are already settled by the operator; these rows verify
they are written down and the schema exists to build against.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** `note_versions` schema is pinned as an append-only ledger | Introspect the `NoteVersion` class added to `sql_models.py` | Table `note_versions` with a monotonic `version`, a snapshot `content`, `change_type`/`changed_by`/`change_reason`/`created_at`, unique constraint on `(note_id, version)`, FK to notes `ondelete='CASCADE'`, and an index on `(note_id, created_at)` | Deterministic: assert the class/`__table_args__` declare each column, the unique constraint, and the index | 100% |
| **[GUARDRAIL]** `mental_model_versions` schema is pinned likewise | Introspect the `MentalModelVersion` class | Table `mental_model_versions` snapshotting `observations`/`entity_metadata`, monotonic `version`, unique `(mental_model_id, version)`, FK cascade, `(mental_model_id, created_at)` index | Deterministic: assert columns, unique constraint, index | 100% |
| `ChangeType` enum enumerates every change source | Introspect the `ChangeType` enum | Values include `created`, `edited`, `appended`, `consolidated`, `reflected`, `corrected`, `rolled_back` | Deterministic: assert the enum members | 100% |
| Settled fork decisions (Q2–Q7) are written down | Read the design doc | It records: Q2 capture at the ingestion layer; Q3 full snapshots + deferred compression; Q4 re-ingest-and-re-extract (pending user confirmation); Q5 separate `as_of` path; Q6 mental-model versions+rollback, diff deferred; Q7 `changed_by=system` default | Deterministic: assert each decision string is present in the doc | 100% |
| Migration-numbering decision is recorded | Read the design doc | One `070` creates both version tables (created in `note-version-history`); `mental-model-version-history` adds only hooks + read surface | Deterministic: assert the decision is stated | 100% |
| **[GUARDRAIL]** No runtime behavior changes in the spike | `git diff` for the spike commit | Only `sql_models.py` (two classes + enum), the design doc, and a schema-shape seed test change; no migration file, no capture hook, no route/MCP tool | Deterministic: assert the changed-file set excludes `alembic/versions/*`, `ingestion.py`, `reflection.py`, `server/*`, MCP tools | 100% |
