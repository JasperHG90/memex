# note-version-history: capture the prior note body on every in-place update, and expose read-only version history

> **Epic:** part 2 of 5 of the knowledge-versioning epic (RFC #234).
> **Depends on:** `knowledge-versioning-spike` (the `NoteVersion` /
> `ChangeType` schema and the migration-numbering decision are settled
> there).
> **Blocks:** `knowledge-temporal-queries`, `knowledge-rollback`.
> **Full delta investigation:** `.loop/knowledge-versioning-epic.md`.

## 1. Title

Make note updates append a `note_versions` row capturing the **prior**
`original_text` before it is overwritten — on the changed-content
overwrite, on append, and on initial create — and expose the history via
a service + API + route + client + MCP read surface
(`memex_note_versions`, `memex_note_at_version`) mirroring the existing
`get_unit_history` end-to-end shape.

## 2. Size / Effort

**M — one migration, three capture hooks, one read path end to end.**
The schema is fixed by the spike; effort is the interception (snapshot
before overwrite without breaking idempotency) and the layered read
surface. The `get_unit_history` path is a near-exact template.

## 3. Triggered by

RFC #234: "Note updates overwrite prior content." Subticket 2 of the
approved split — the foundation the temporal-query and rollback tickets
build on.

## 4. Context (today's state, cited)

- **Overwrite on changed content.** `ingestion.py:412-443` detects
  "exists but content changed. Incremental update." (`:432`) and calls
  `memory.retain(..., note_id=note_uuid, ...)` (`:495-503`);
  `extraction/storage.py:152-154` DELETEs the prior `Note` row and
  `:193-217` re-inserts overwriting `original_text` (`:199`). The prior
  body is never copied off.
- **Identical-hash skip writes nothing.** `ingestion.py:429-431` returns
  `status='skipped'` — no version must be written here.
- **Append concatenates in place.** `append_to_note`
  (`ingestion.py:534-916`): `new_body = parent_body + sep + delta`
  (`:782-786`), re-ingested under the same `note_id` (`:819-829`);
  appendability gated on `status=='active'` + `archived_at IS NULL`
  (`:743-754`); replay idempotency in `note_appends` (`:756-777`).
- **Body lives in the DB**, `Note.original_text`
  (`sql_models.py:278-282`); the FileStore holds assets only, so the
  snapshot source is that column.
- **Existing lineage columns are not versioning.** `superseded_by`
  (`sql_models.py:334`), `appended_to` (`:340`) are single-level
  cross-note pointers; `note_appends` stores only delta hash + bytes
  (`:1891-1898`).
- **The read template.** `get_unit_history` exists end to end:
  service `packages/core/src/memex_core/services/units.py:613-772`,
  facade `api.py:1609-1623`, route `server/memories.py:558-562`, client
  `packages/common/src/memex_common/client.py:1324`, MCP tool template
  `packages/mcp/src/memex_mcp/server.py:409-474` (`memex_read_note`).
- **Schema (from the spike):** `NoteVersion` / `note_versions` +
  `ChangeType` already defined in `sql_models.py`; this ticket generates
  the migration.

## 5. Non-goals / out of scope

- No `as_of` reconstruction / diff (that is `knowledge-temporal-queries`)
  and no rollback (`knowledge-rollback`).
- No mental-model versioning (`mental-model-version-history`).
- No memory-unit versioning; no change to the entity-relation `as_of`.
- No new note lifecycle status; do not touch the
  `('active','superseded')` CHECK (`sql_models.py:385-388`).
- No `EvolutionTracker` analytics.
- No retention/compression — full snapshots per spike Q3.

## 6. Requirements & restrictions

**Must achieve:**

- R1. On initial create, write `note_versions` version 1 with
  `change_type=created`.
- R2. On a changed-content overwrite (`ingestion.py:432`), capture the
  **prior** `original_text` into a new `note_versions` row (monotonic
  `version`, `change_type=edited`) BEFORE `retain` overwrites it, in the
  same transaction.
- R3. On append (`ingestion.py:782`), capture the **pre-append** body
  with `change_type=appended`, preserving `note_appends` replay
  idempotency (`:756-777`).
- R4. An identical-hash skip (`ingestion.py:429-431`) writes **no**
  version.
- R5. Version rows are append-only, monotonic per `note_id`, unique
  `(note_id, version)`; each records `change_type`, `changed_by`
  (default `system`, spike Q7), `change_reason`, `created_at`, and the
  snapshot `content` (+ `summary`/`content_hash` if cheap).
- R6. Read surface: `list_note_versions(note_id)` and
  `get_note_at(note_id, version)` through service → API → route
  (`Depends(require_read)`) → client → MCP tools `memex_note_versions`,
  `memex_note_at_version`, mirroring `get_unit_history`. `get_note_at`
  by version number only here (by-timestamp `as_of` is the next ticket).

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md`: tests-first; DB behavior against
  real testcontainer Postgres; no `skip`/`xfail`/`# type: ignore`;
  gating tests in root `./tests/` (the `just test` gate runs only that
  dir).
- `CLAUDE.md:180-196`: new MCP tool descriptions ≤ 1,200 chars, fenced
  by `packages/mcp/tests/test_description_budgets.py` and
  `test_no_universal_content_in_descriptions.py`; description home per
  the SSOT rule (inline in `server.py` if MCP-only).
- Async I/O, single quotes, line length 100, mypy strict (CLAUDE.md).
- Alembic migration chains off head `069`, idempotent in the `061` style
  (`_table_exists`/`_index_exists`) so
  `SQLModel.metadata.create_all(..., checkfirst=True)` (`env.py:217`)
  and the migration agree.
- `.claude/rules/pre-existing-issues.md`; `.claude/rules/adversarial-reviews.md`.

## 7. Code surface

- **Migration** `packages/core/src/memex_core/alembic/versions/070_note_and_mental_model_versions.py`
  (per spike R4: one `070` creating `note_versions` and, ahead of the
  next ticket, `mental_model_versions`). Template:
  `061_experiential_entries.py:291-344`. **New.** Generated via
  `uv run memex database revision -m "note and mental model versions"`.
- **Capture hooks** `packages/core/src/memex_core/services/ingestion.py`
  — create (v1), the `:432` overwrite branch (snapshot prior body),
  `:782` append branch (snapshot pre-append body); leave `:429-431`
  skip untouched. **Edit.**
- **Service** `packages/core/src/memex_core/services/notes.py`
  (`NoteService` `:114`; `get_note` `:586-595`) — add
  `list_note_versions`, `get_note_at`. **Edit.**
- **Facade** `packages/core/src/memex_core/api.py` — delegators (mirror
  `get_note` `:1292-1294`; `get_unit_history` `:1609-1623`). **Edit.**
- **Route** `packages/core/src/memex_core/server/notes.py:52,253-260` —
  `@router.get('/notes/{note_id}/versions', ...)` and
  `.../versions/{version}` with `Depends(require_read)`, `_handle_error`.
  **Edit.**
- **Client** `packages/common/src/memex_common/client.py:648-651`
  template — `list_note_versions`, `get_note_at`. **Edit.**
- **DTO** `packages/common/src/memex_common/schemas.py` — `NoteVersionDTO`
  (mirror `UnitHistoryNodeDTO`). **Edit/New.**
- **MCP** `packages/mcp/src/memex_mcp/server.py:409-474` template —
  `memex_note_versions`, `memex_note_at_version` (`tags={'read'}`,
  `readOnlyHint`, `ToolError` for 4xx). **Edit.**

## 8. Tests & validation gates

**Gates:** `just test` + `just prek`, both green. Run the MCP-package
suite explicitly for the description-budget fences:
`uv run pytest packages/mcp/tests`.

**Reproducing test first — `tests/test_note_versioning.py`** (root
`./tests/`, testcontainer Postgres):
- Overwrite: `add_note(note_key=K, content=A)` then
  `add_note(note_key=K, content=B)` → `note_versions` has a row holding
  **A** (the prior body) with `change_type=edited`, monotonic `version`.
- Create: first `add_note` writes version 1 `change_type=created`.
- Skip: re-`add_note` with identical content writes **no** new version.
- Append: `append_note(K, delta)` writes a version holding the
  pre-append body `change_type=appended`; a replayed `append_id`
  (`ingestion.py:756-777`) writes no extra version.
- Read: `list_note_versions` returns rows ordered by `version`;
  `get_note_at(K, 1)` returns content A.
- Uniqueness: two versions never share `(note_id, version)`.

**Eval marker (required):** `.loop/evals/note-version-history.md` pins
the guardrails (update preserves prior version; create=v1; skip writes
nothing; rollback-none-here). Validate with
`loopctl eval note-version-history`.

## 9. Risk assessment

- **Blast radius: the note ingest/append path — the hottest write path.**
  A bad hook could double-write or break idempotency. Mitigation:
  snapshot in the same transaction as the existing write, keyed on the
  row already read; assert the skip/replay paths write nothing.
- **Reversibility:** additive (new table + hooks + read surface). Back
  out = drop the table + hooks. History is append-only.
- **Failure modes:** (1) snapshotting the NEW body instead of the prior
  — test asserts the row holds the pre-update body; (2) writing a
  version on the no-op skip — test asserts none; (3) an append replay
  writing a duplicate version — test asserts idempotency; (4) MCP
  description over budget — run the MCP budget test; (5) gating test
  under `packages/*/tests` where the loop gate won't run it — root
  `./tests/` only.

## 10. Subtickets (ordered steps)

1. Generate migration `070` (both tables per spike R4). → verify:
   `uv run memex database upgrade` clean; autogenerate diff empty after.
2. Add the three capture hooks in `ingestion.py`. → verify: overwrite +
   append tests green; skip writes nothing.
3. Service + facade + route + client + DTO. → verify: read tests green.
4. MCP tools `memex_note_versions`, `memex_note_at_version`. → verify:
   `uv run pytest packages/mcp/tests` green (budgets + fences).
5. Adversarial review. → verify: clean; `just test` + `just prek` green.

## 11. Open questions

All substantive forks were settled in the spike (Q2 interception point =
ingestion layer; Q7 `changed_by=system` default). No new fork. If the
migration-ownership decision from spike R4 changed, re-point the §7
migration accordingly.

---

**Eval marker:** `.loop/evals/note-version-history.md`
(`require_eval: true`).
