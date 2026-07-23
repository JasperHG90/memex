# knowledge-versioning-spike: pin the version-table schema and record the settled fork decisions for RFC #234

> **Epic:** part 1 of 5 of the knowledge-versioning epic (RFC #234).
> **Depends on:** nothing — this is the first iteration.
> **Blocks:** `note-version-history`, `mental-model-version-history`,
> `knowledge-temporal-queries`, `knowledge-rollback`.
> **Full delta investigation:** `.loop/knowledge-versioning-epic.md`
> (the reviewed umbrella; retired from the ledger, kept as reference).

## 1. Title

Produce the design of record for knowledge versioning: the exact column
schema for the two new append-only tables (`note_versions`,
`mental_model_versions`), the `ChangeType` enum, the migration-numbering
decision, and a short written record of the fork decisions the operator
already settled (Q2–Q7). **This is a spike. The deliverable is a written
design doc plus the two `SQLModel` table classes added to `sql_models.py`
(no migration, no capture hooks, no read surface).** It exists so the
two implementation tickets build against one agreed schema instead of
each re-deriving it.

## 2. Size / Effort

**S — one design doc plus two table-class definitions.** The forks are
already settled (§4.3); the work is writing them down concretely and
committing the two SQLModel classes (mirroring an existing precedent) so
autogenerate and the implementation tickets have a fixed target. No
runtime behavior changes.

## 3. Triggered by

RFC #234, scoped in triage to notes + mental models, then split into 5
subtickets (operator-approved). This spike is subticket 1: settle the
schema and record the decisions so the loop can run subtickets 2–5
against a stable design.

## 4. Context (today's state, cited)

### 4.1 Why two version tables (the gap)

Both surfaces are overwritten in place today; prior versions are
unrecoverable. Notes: `add_note` on a changed body deletes-and-reinserts
the `Note` row overwriting `original_text`
(`packages/core/src/memex_core/memory/extraction/storage.py:152-217`,
routed at `packages/core/src/memex_core/services/ingestion.py:432`);
`append` concatenates in place (`ingestion.py:782-829`); `note_appends`
keeps only the delta hash + byte count
(`packages/core/src/memex_core/memory/sql_models.py:1891-1898`). Mental
models: reflection replaces the whole `observations` JSONB via CAS
`UPDATE` and bumps an int `version` counter
(`packages/core/src/memex_core/memory/reflect/reflection.py:722-729`,
`:1090-1097`; `sql_models.py:172-174`). Neither has a version ledger.

### 4.2 The precedent to mirror (do NOT invent a new shape)

`ProceduralEntryVersion` / `procedural_entry_versions`
(`sql_models.py:2267-2326`): monotonic `version` per parent id, unique
`(entry_id, version)`, FK `ondelete='CASCADE'`. Migration template:
`packages/core/src/memex_core/alembic/versions/061_experiential_entries.py:291-344`
(the `experiential_entry_versions` ledger — FK cascade, unique
`(entry_id, version)`, index on `(entry_id, created_at)`). SQLModel
tables live in `sql_models.py`; `__table_args__` indexes as on
`Note.__table_args__` (`:383-399`). Autogenerate picks up any new
`SQLModel, table=True` class imported via `alembic/env.py:21`
(`target_metadata = SQLModel.metadata`, `:39`). Current Alembic head:
`069_nodes_chunks_search_tsvector`.

### 4.3 Fork decisions already settled by the operator (record these)

- **Q1** (approved): split the epic into 5 dependency-ordered plan
  files. Done.
- **Q2** (accepted default): capture the prior note body in the
  **ingestion service layer** (`ingestion.py:432`, `:782`), not deeper
  in `extraction/storage.py` — the old row and the change reason are
  both in hand there.
- **Q3** (accepted default): v1 keeps **full-content snapshots** for all
  versions; document the unbounded growth; defer compression to a
  separate ticket.
- **Q4** (accepted default, **pending user confirmation**): note
  rollback routes through **re-ingest-and-re-extract** so derived state
  reconciles (see `knowledge-rollback`).
- **Q5** (accepted default): the note/mental-model `as_of` is a
  **separate service path**, not an extension of the entity-cooccurrence
  `RetrievalRequest.as_of` (`retrieval/models.py:130`,
  `strategies.py:173-190`).
- **Q6** (accepted default): mental models ship **versions + rollback**
  now; mental-model diff deferred.
- **Q7** (accepted default): `changed_by` defaults to `system` with a
  derived `change_reason` at internal hooks; no full actor audit in v1.

### 4.4 Gates

- `just test` → `uv run pytest tests` (root `./tests/` only;
  `justfile:65`). `just prek` → `uv run prek run -a` (`justfile:61`).
- `require_eval: true` — a marker `.loop/evals/knowledge-versioning-spike.md`
  must exist before pickup.

## 5. Non-goals / out of scope

- No Alembic migration (that is `note-version-history` / the numbering
  decision recorded here, applied there).
- No capture hooks, no read/diff/rollback surface, no MCP tools.
- No memory-unit or entity-merge versioning (excluded epic-wide).
- No `EvolutionTracker` analytics.

## 6. Requirements & restrictions

**Must achieve:**

- R1. A written design doc (location per §11 Q-doc; recommend
  `.loop/knowledge-versioning-epic.md` appended, or a `docs/` design
  note) that states, concretely: every column of `note_versions` and
  `mental_model_versions`, the `ChangeType` enum values, the unique
  constraint and index on each table, and the FK/cascade choice —
  each mirroring `procedural_entry_versions` with a cited anchor.
- R2. The record of Q2–Q7 decisions from §4.3, so subtickets 2–5 do not
  re-open them.
- R3. The two `SQLModel, table=True` classes (`NoteVersion`,
  `MentalModelVersion`) and the `ChangeType` enum ADDED to
  `sql_models.py`, so autogenerate in `note-version-history` diffs
  against them. Classes only — the migration is generated in the next
  ticket.
- R4. The migration-numbering decision: one `070` creating both tables
  vs. `070` (notes) + `071` (mental models). Recommend **one `070` for
  both** (they are introduced together and share the pattern), created
  in `note-version-history`, with `mental-model-version-history` adding
  only capture hooks + read surface against it.

**Restrictions:**

- Mirror `procedural_entry_versions` exactly for the ledger shape; do
  not invent new column semantics.
- `sql_models.py` classes must pass `just prek` (ruff + mypy strict,
  single quotes, line length 100).
- `.claude/rules/adversarial-reviews.md`: adversarial-review the design
  doc + classes before close.
- `.claude/rules/slop-scan-for-docs.md`: if the design doc is markdown,
  run the P0 hallucination check — every cited anchor must resolve.

## 7. Code surface

- `packages/core/src/memex_core/memory/sql_models.py` — add
  `class ChangeType(str, Enum)`, `class NoteVersion(SQLModel, table=True)`
  (`note_versions`), `class MentalModelVersion(SQLModel, table=True)`
  (`mental_model_versions`), mirroring `ProceduralEntryVersion`
  (`:2267-2326`) and `Note.__table_args__` (`:383-399`). **Edit.**
- The design doc (markdown) recording R1/R2/R4. **Write.**
- Read-only references: `061_experiential_entries.py:291-344`,
  `alembic/env.py:21,39`, `ingestion.py:432,782`,
  `reflection.py:722-729,1090-1097`.

## 8. Tests & validation gates

**Gates:** `just test` + `just prek`, both green. Adding two unused-yet
SQLModel classes must not break autogenerate or the model import.

**Test to add:** a minimal `tests/test_note_versioning.py` seed that
imports `NoteVersion`, `MentalModelVersion`, `ChangeType` and asserts
each table's `__tablename__`, that `version` is present, and that the
unique constraint on `(<parent>_id, version)` and the `created_at` index
are declared (introspect `__table_args__`). This pins the schema shape
the next tickets depend on. Root `./tests/` so `just test` runs it. Must
pass ruff + mypy with no `skip`/`xfail`/`# type: ignore`.

**Eval marker (required):** `.loop/evals/knowledge-versioning-spike.md`
pins the deliverables — schema columns present, enum enumerated, Q2–Q7
recorded, migration numbering decided. Validate with
`loopctl eval knowledge-versioning-spike`.

## 9. Risk assessment

- **Blast radius: minimal.** Two new unused table classes + a doc.
  Nothing writes to the tables yet.
- **Reversibility: trivial.** Delete the classes and the doc.
- **Failure modes:** (1) a class that breaks `SQLModel.metadata` import
  fails the whole test suite — assert import in the seed test; (2)
  drifting from the `procedural_entry_versions` shape — cite it and
  match it; (3) autogenerate later producing a surprise diff — keep the
  classes complete here so `070` is clean.

## 10. Subtickets (ordered steps within this iteration)

1. Re-open `procedural_entry_versions` (`sql_models.py:2267-2326`) and
   `061` (`:291-344`); draft the two table schemas + `ChangeType`. →
   verify: columns/constraints/index enumerated with anchors.
2. Add the two classes + enum to `sql_models.py`. → verify: `just prek`
   green; `SQLModel.metadata` imports.
3. Write the design doc (R1/R2/R4). → verify: every anchor resolves.
4. Add the schema-shape seed test. → verify: `just test` green.
5. Adversarial review + slop scan. → verify: clean.

## 11. Open questions

- **Q-doc — where does the design doc live?** Append to
  `.loop/knowledge-versioning-epic.md`, or a new `docs/` design note?
  *Recommendation: append to the epic umbrella — it is the natural home
  and avoids inviting the docs slop gate on an internal design record.*
  All substantive forks (Q1–Q7) are already settled (§4.3).

---

**Eval marker:** `.loop/evals/knowledge-versioning-spike.md`
(`require_eval: true`).
