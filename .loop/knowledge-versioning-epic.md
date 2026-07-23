# knowledge-versioning: append-only version history, temporal queries, and rollback for notes and mental models

## 1. Title

Add append-only version history to the two knowledge surfaces that are
**mutated in place today** — notes and mental models — so an update
preserves the prior version instead of destroying it. On top of the
history, deliver temporal reconstruction ("what did this note say as of
date Y?"), a between-versions diff, and rollback to a prior version.
This is RFC #234, scoped down in triage to the mutable-in-place
surfaces only.

**This is an epic parent ticket.** It carries the shared context,
delta, and decisions once, then decomposes into an ordered subticket
sequence (§10). Each subticket in §10 is sized to run as its own loop
iteration; §11 Q1 asks the operator whether to split them into separate
plan files before the loop picks this up (recommended).

## 2. Size / Effort

**L — new schema on two write paths, plus a temporal read/rollback
surface.** Effort drivers: (a) two new append-only tables with an
Alembic migration each; (b) intercepting the note overwrite/append path
and the mental-model reflection CAS path to snapshot the prior version
*before* it is replaced, without breaking idempotency or the CAS
version counter; (c) a rollback whose derived-state semantics must be
decided explicitly (§11 Q4) because Memex derived copies survive
deletion of their source (RFC #259). Not algorithmically hard; the risk
is scope and the interception points, not the data model — the repo
already has two append-only version-ledger precedents to mirror
(`procedural_entry_versions`, `experiential_entry_versions`).

## 3. Triggered by

RFC #234 (GitHub issue #234, `arxiv:agentic-memory-scan-2026-06`):
"Memex has no mechanism for tracking how knowledge changes over time.
Note updates overwrite prior content, mental model revisions lose the
previous version." Triage decided the scope below. This history is also
the evidence base for conflict-typing (#233 — "this note changed on
date X" is what classifies a conflict as TEMPORAL rather than HARD) and
the recovery path after a bad ingest.

## 4. Context (today's state, cited)

### 4.1 Notes are overwritten in place — prior body is unrecoverable

`note_key` maps deterministically to a fixed `Note.id`
(`packages/common/src/memex_common/note_utils.py:9-18`,
`derive_note_uuid_from_key` = `UUID(md5(note_key))`), so re-sending the
"same" note always targets the same primary-key row.

- **Overwrite on changed content.** The ingest router detects "note
  exists, content_hash differs" and proceeds to an incremental update
  (`packages/core/src/memex_core/services/ingestion.py:412-443`; the
  branch logs "exists but content changed. Incremental update." at
  `:432` and increments `NOTE_ADD_OVERLAPS_EXISTING_TOTAL` at
  `:437-443` — the codebase already flags this overwrite as a hazard
  and tells callers to prefer `append`). It then calls
  `memory.retain(..., note_id=note_uuid, ...)`
  (`ingestion.py:495-503`).
- **The old row is destroyed.**
  `packages/core/src/memex_core/memory/extraction/storage.py:152-154`
  `DELETE`s the prior `Note` row on the first batch (cascade-deleting
  `chunks` and `memory_units` via the relationship cascades at
  `sql_models.py:376-381`), and `:193-217` re-inserts via
  `on_conflict_do_update`, whose `set_clause` overwrites `original_text`
  (`:199`), `content_hash`, `doc_metadata`, etc. The prior
  `original_text` is never copied off first.
- **Append concatenates in place.** `append_to_note`
  (`ingestion.py:534-916`) builds `new_body = parent_body + sep + delta`
  (`:782-786`) and re-ingests under the **same** `note_id`
  (`:819-829`). Appendability is gated on `parent.status == 'active'`
  and `archived_at IS NULL` (`:743-754`); replay idempotency lives in
  `note_appends` (`:756-777`).
- **The one existing per-update record keeps no content.** `NoteAppend`
  / `note_appends` (`packages/core/src/memex_core/memory/sql_models.py:1873-1926`)
  stores `delta_sha256` and `delta_bytes` (`:1891-1898`) plus
  `resulting_content_hash` — the **hash and byte count of the delta,
  not its text**. Prior note bodies cannot be reconstructed from it.
- **Note content lives in the DB, not the FileStore.** The authoritative
  body is `Note.original_text` (`sql_models.py:278-282`). The FileStore
  (`packages/core/src/memex_core/storage/filestore.py`) only holds asset
  files, keyed `assets/{vault}/{note_id}/...`
  (`ingestion.py:451-456`); `filestore_path` is set to that asset
  directory only (`ingestion.py:485`), never to a body file. So a note
  version snapshot must capture `Note.original_text` from the DB — there
  is no markdown file to fall back on.
- **Existing note lineage columns are single-level pointers, not
  self-versioning.** `Note.superseded_by` (`sql_models.py:334-338`) and
  `Note.appended_to` (`:340-344`) point at *other* notes; `status IN
  ('active','superseded')` (CHECK at `:385-388`); `updated_at`
  (`:373`) is one last-modified timestamp. `set_note_status`
  (`packages/core/src/memex_core/services/notes.py:195-312`) flips these
  pointers. None of them retains a prior body. There is **no**
  `version`, `revision`, `previous_version`, or content-snapshot column
  on `Note`, and no `note_versions` table.

### 4.2 Mental models are overwritten in place on reflection refresh

`MentalModel` / `mental_models` (`sql_models.py:138/144`) is a real
table, one row per `(entity_id, vault_id)` (unique index at
`:213-220`). Its synthesized content is the `observations` JSONB
(`:156-160`); `version` (`:172-174`) is a single monotonic **integer
counter**, "incremented on each update" — it stores no prior content.

- Reflection Phase 5 finalize does a compare-and-swap `UPDATE`
  (`packages/core/src/memex_core/memory/reflect/reflection.py:669-763`)
  that replaces the whole `observations` JSONB and bumps `version`
  (`:722-729`). The surgical single-observation refresh does the same
  (`:1090-1109`, values at `:1095-1097`).
- Evidence pruning mutates `observations` in place and deletes the row
  if it prunes to zero
  (`packages/core/src/memex_core/services/mental_model_cleanup.py:76-81`).
- On refresh, prior observation titles/content, their evidence
  citations, the entity description, and the embedding are lost, except
  for observations the LLM merge chooses to carry forward. There is
  **no** `mental_model_versions` table.

### 4.3 What ALREADY exists (the delta boundary — do NOT reinvent)

- **Memory units are already effectively append-only** and are OUT of
  scope (§5). `get_unit_history`
  (`packages/core/src/memex_core/services/units.py:613-772`; API
  `api.py:1609-1623`; route `server/memories.py:558-562`; client
  `packages/common/src/memex_common/client.py:1324`; MCP tool
  `memex_get_unit_history`) synthesizes "history" on read by walking the
  contradiction `MemoryLink` graph. Contradiction handling is a
  non-destructive confidence downweight plus an edge
  (`packages/core/src/memex_core/memory/contradiction/engine.py:340-376`),
  never a delete. `MemoryUnit` (`sql_models.py:625/631`) has only
  `status active|stale` (`:680-683`) and `is_deprioritized`
  (`:723-727`) — no version columns needed.
- **Bitemporal `as_of` already exists but ONLY on entity-relation
  edges.** `valid_from`/`valid_to` live only on `EntityCooccurrence`
  (`sql_models.py:1197-1207`, migration `020`); `_apply_as_of_filter`
  (`packages/core/src/memex_core/memory/retrieval/strategies.py:173-190`)
  and `RetrievalRequest.as_of`
  (`packages/core/src/memex_core/memory/retrieval/models.py:130`,
  threaded at `engine.py:703-705`) time-travel the entity graph only.
  This ticket's note/mental-model `as_of` is a **separate** temporal
  axis over the new version tables; it must not be conflated with, or
  routed through, the cooccurrence filter (§11 Q5).
- **Lineage is a live derivation DAG, not a version history.**
  `LineageService.get_lineage`
  (`packages/core/src/memex_core/services/lineage.py:79-86`) computes
  note↔unit↔mental-model provenance at query time; there is no lineage
  table and no notion of prior versions.
- **Two real append-only version ledgers already exist — mirror them.**
  `ProceduralEntryVersion` / `procedural_entry_versions`
  (`sql_models.py:2267-2326`: monotonic `version` per `entry_id`, unique
  `(entry_id, version)`) is the model/table precedent. Migration
  `packages/core/src/memex_core/alembic/versions/061_experiential_entries.py:291-344`
  (the `experiential_entry_versions` ledger: FK `ondelete='CASCADE'`,
  unique `(entry_id, version)`, index on `(entry_id, created_at)`) is
  the migration precedent.
- **Entity-merge history is destructive and OUT of scope** (§5):
  forward-only, losers hard-deleted, explicitly irreversible
  (`packages/core/src/memex_core/services/proposal_actions/merge_entities.py:1-11,74-79,150`).

### 4.4 Gates (verified this session)

- `just test` → `uv run pytest tests` (`justfile:65`) — runs the **root
  `./tests/` suite only** (E2E against testcontainer Postgres).
  `packages/core/tests` and `packages/mcp/tests` are **not** collected
  by this gate (confirmed precedent:
  `.loop/plans/retrieval-staleness-score-spike.md:116-122`). Any test
  the loop must verify has to live under root `./tests/`.
- `just prek` → `uv run prek run -a` (`justfile:61`) — ruff + mypy
  (strict), line length 100, single quotes.
- `require_eval: true` (`.loop/config.json`) — the loop refuses pickup
  until an eval marker exists (§8, §11).
- Migrations: `uv run memex database revision -m "..."` (autogenerate;
  `packages/cli/src/memex_cli/db.py:124-142`). Current single head:
  `069_nodes_chunks_search_tsvector`
  (`packages/core/src/memex_core/alembic/versions/069_nodes_chunks_search_tsvector.py`).
  Autogenerate picks up any `SQLModel, table=True` class defined in
  `sql_models.py` (imported by `alembic/env.py:21`; `target_metadata =
  SQLModel.metadata` at `:39`).

## 5. Non-goals / out of scope

- **No memory-unit versioning.** Units are already append-only via the
  contradiction/supersession graph (§4.3). Do not add version columns to
  `MemoryUnit` or duplicate `get_unit_history`.
- **No entity-merge history and no merge reversibility.** Merges are
  forward-only hard deletes by design (`merge_entities.py:150`); leave
  that path untouched.
- **No `EvolutionTracker` analytics** (stability/volatility/change-
  frequency detection from the RFC's Phase 3). That feeds crystal
  promotion (#15) and consolidation (#220) and is a separate follow-up
  ticket once the version substrate exists. Building it here is scope
  creep.
- **No git-like branching/merging of versions.** Append-only linear
  version chains only (the RFC itself rejects branching for v1).
- **No change to the existing entity-relation `as_of` filter**
  (`strategies.py:173-190`) or `RetrievalRequest.as_of`. The new
  temporal axis is over note/mental-model versions, added alongside.
- **No coalescing/compression of versions.** Every qualifying update
  writes one version row; retention/compression is deferred (§11 Q3).
- **No new note lifecycle status value.** Do not extend the
  `('active','superseded')` CHECK (`sql_models.py:385-388`); versioning
  is orthogonal to lifecycle state.

## 6. Requirements & restrictions

**Must achieve:**

- R1. Every update that today overwrites a note body writes an
  append-only `note_versions` row capturing the **prior** content
  before it is replaced — covering the changed-content overwrite
  (`storage.py:152-217` via `ingestion.py:432`), the append
  (`ingestion.py:782-829`), and the initial create (version 1).
  Monotonic `version` per `note_id`, unique `(note_id, version)`,
  never modified or deleted (mirror `procedural_entry_versions`,
  `sql_models.py:2267-2326`).
- R2. Every reflection refresh that overwrites a mental model writes an
  append-only `mental_model_versions` row capturing the prior
  `observations`/`entity_metadata` before the CAS `UPDATE`
  (`reflection.py:722-729`, `:1090-1097`) and before evidence-prune
  deletion (`mental_model_cleanup.py:76-81`).
- R3. Each version row records `change_type` (an enum: at least
  `created`, `edited`, `appended`, `consolidated`, `reflected`,
  `corrected`, `rolled_back`), `changed_by` (`system`|`agent`|`user`),
  `change_reason` (nullable), and `created_at`, so the history answers
  "why did this change?".
- R4. Temporal reconstruction: a service + read path that returns a
  surface's content **as of** a timestamp or a specific version number,
  plus a between-versions diff and a version listing. Expose via MCP
  read tools mirroring the `memex_get_unit_history` end-to-end shape.
- R5. Rollback: appending a new version whose content equals a target
  prior version's content (never mutating/deleting history), applied to
  the live note/mental-model row. Its derived-state behavior is fixed by
  §11 Q4 and must be explicit in the tool contract.
- R6. Backward compatible: existing ingest/append/reflection behavior
  and idempotency (`note_appends` replay at `ingestion.py:756-777`; the
  CAS `version` guard at `reflection.py:723`) are preserved. A skipped
  ingest (identical hash, `ingestion.py:429-431`) writes **no** version.

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md` (`all-code-needs-tests`,
  `tests-are-real-code`, `dont-mock-what-you-can-run`): every change
  ships a test; DB behavior is tested against real (testcontainer)
  Postgres, not a mocked metastore; tests pass ruff + mypy with no
  `skip`/`xfail`/`# type: ignore`. Tests the loop gate must run live in
  root `./tests/` (§4.4).
- `CLAUDE.md` agent-surface constraints (`:180-196`): each new MCP tool
  description stays within the 1,200-char cap and is fenced by
  `packages/mcp/tests/test_description_budgets.py:118-131` and
  `packages/mcp/tests/test_no_universal_content_in_descriptions.py:80-89`.
  Decide description home per the SSOT rule (`CLAUDE.md:180-186`):
  inline in `server.py` if MCP-only, `tool_descriptions.py` if Hermes
  mirrors it.
- CLAUDE.md code style: async I/O throughout, single quotes, line length
  100, strict mypy, Python ≥ 3.12.
- Alembic migrations chain off the current head `069` and must be
  idempotent in the style of `061` (`_table_exists`/`_index_exists`
  guards) so integration tests that build schema via
  `SQLModel.metadata.create_all(..., checkfirst=True)` (`env.py:217`)
  and the migration path agree.
- `.claude/rules/pre-existing-issues.md`: fix, do not skip, any
  pre-existing failure the work surfaces.
- `.claude/rules/adversarial-reviews.md`: each subticket runs an
  adversarial review before it is reported done.
- RFC #259 (closed) derived-copies insight: **derived state survives
  deletion of its source.** A note's memory units and the mental models
  built from them persist independently of the note body. Rollback of a
  note body therefore does NOT implicitly revert its derived units or
  models; §11 Q4 fixes what rollback does with them.

## 7. Code surface (files to touch; anchors to re-open and cite)

**Schema (new tables + migration):**
- `packages/core/src/memex_core/memory/sql_models.py` — **add** two
  classes, `NoteVersion` (`note_versions`) and `MentalModelVersion`
  (`mental_model_versions`), each mirroring `ProceduralEntryVersion`
  (`:2267-2326`): monotonic `version`, unique `(<parent>_id, version)`,
  FK `ondelete='CASCADE'`, `__table_args__` index on `(<parent>_id,
  created_at)` in the style of `Note.__table_args__` (`:383-399`). Add a
  `ChangeType` enum.
- **new** Alembic migration(s) off head `069`, using the `061`
  table+FK+unique+index template (`061_experiential_entries.py:291-344`)
  with `_table_exists`/`_index_exists` guards. Migration ownership is
  fixed by the ST1 spike (§10, §11 Q1): either one `070` creating both
  `note_versions` and `mental_model_versions`, or a per-subticket
  `070` (notes) + `071` (mental models). Do not name a migration for a
  table it does not ship.

**Note version capture (write interception):**
- `packages/core/src/memex_core/services/ingestion.py:412-443` — capture
  the prior `Note.original_text` into `note_versions` at the
  "content changed" branch (`:432`) before `retain` overwrites it;
  write version 1 on first create (`ingestion.py:429-431` skip path
  writes nothing).
- `packages/core/src/memex_core/services/ingestion.py:782-829` — capture
  the pre-append body before `append_to_note` re-ingests.
- Alternatively (an interception-point fork, §11 Q2) snapshot inside
  `packages/core/src/memex_core/memory/extraction/storage.py:151-217`
  where the old row is read; the ingestion-layer point is recommended
  because the old row and the `change_reason` are both in hand there.

**Mental-model version capture:**
- `packages/core/src/memex_core/memory/reflect/reflection.py:669-763`
  and `:1090-1109` — snapshot prior `observations`/`entity_metadata`
  into `mental_model_versions` inside the same transaction as the CAS
  `UPDATE`, keyed on `claimed_version`.
- `packages/core/src/memex_core/services/mental_model_cleanup.py:76-81`
  — snapshot before prune-in-place / row deletion.

**Service + facade + route + client + MCP (temporal read, diff,
rollback), mirroring the `get_unit_history` end-to-end path:**
- `packages/core/src/memex_core/services/notes.py` (`NoteService`,
  `:114`; `get_note` `:586-595`) — add `list_note_versions`,
  `get_note_at`, `diff_note_versions`, `rollback_note`.
- A new mental-model versioning service (or extend the mental-model
  service) for the parallel read/rollback.
- `packages/core/src/memex_core/api.py` — thin delegators (mirror
  `get_note` `:1292-1294`; services wired in the constructor near
  `:549`). `get_unit_history` at `api.py:1609-1623` is the closest
  existing shape.
- `packages/core/src/memex_core/server/notes.py:52,253-260` — new
  `@router.get(...)` routes with `Depends(require_read)` (read tools)
  and a guarded `@router.post(...)` for rollback, `_handle_error`
  mapping.
- `packages/common/src/memex_common/client.py:648-651` (`get_note`
  template) and `:1324` (`get_unit_history` template) — new client
  methods + their DTOs.
- `packages/mcp/src/memex_mcp/server.py:409-474` (`memex_read_note`
  template; `mcp` at `:335-337`) — new tools
  `memex_note_versions`, `memex_note_at_version`, `memex_note_diff`,
  `memex_note_rollback` (and the mental-model equivalents if Q6 keeps
  them), `tags={'read'}`/`{'search'}` + `readOnlyHint` for reads,
  `ToolError` for 4xx. Descriptions per the SSOT rule (§6).

**DTOs:**
- `packages/common/src/memex_common/schemas.py` — new `NoteVersionDTO`,
  diff DTO, mirroring `UnitHistoryNodeDTO`.

## 8. Tests & validation gates

**Gates:** `just test` (`uv run pytest tests`, root `./tests/` only) and
`just prek`. Both green at each subticket's close.

**Where tests live:** the loop gate runs **only** root `./tests/`
(§4.4), so the E2E tests that gate this feature go there (they need
testcontainer Postgres, which root `./tests/` already uses). Package-
level unit tests under `packages/core/tests/unit` may be added for pure
logic (diff, `change_type` selection) but will NOT be gated by the loop
— do not rely on them for the Definition of Done.

**Tests to add (each subticket names its reproducing test first):**
- `tests/test_note_versioning.py` — (a) `add_note` with the same
  `note_key` and changed content writes a `note_versions` row holding
  the **prior** `original_text`, monotonic `version`, correct
  `change_type='edited'`; (b) initial create writes version 1
  `change_type='created'`; (c) identical-hash re-ingest
  (`ingestion.py:429-431`) writes **no** version; (d) `append` writes a
  version holding the pre-append body with `change_type='appended'` and
  preserves `note_appends` replay idempotency.
- `tests/test_mental_model_versioning.py` — a reflection refresh writes
  a `mental_model_versions` row with the prior `observations`, and the
  CAS `version` guard still works under a concurrent claimed-version
  mismatch (no double-write, no lost update).
- `tests/test_knowledge_temporal_queries.py` — `get_note_at(as_of=T)`
  returns the version live at T; `diff` between two versions reports the
  change; version listing is ordered and complete. Assert the note
  `as_of` does NOT touch the entity-cooccurrence `as_of` path.
- `tests/test_knowledge_rollback.py` — rollback appends a NEW version
  equal to the target (history length grows, nothing deleted), the live
  note body updates, and the derived-state behavior fixed by §11 Q4
  holds (e.g. derived units are/are-not re-extracted, per the resolved
  fork).
- `packages/mcp/tests/test_description_budgets.py` and
  `test_no_universal_content_in_descriptions.py` — auto-discover the new
  tools; keep every new description within budget and off the banned
  list. (These run in the MCP package suite, not the loop gate; run them
  explicitly with `uv run pytest packages/mcp/tests` before close.)

**Eval marker (required — `require_eval: true`):** co-author with the
`create-eval` skill before implementation. The Definition of Done the
eval pins: an update preserves the prior version (not overwrite),
`as_of` reconstructs the historical content, and rollback is append-only
(history is never shortened). Because rollback carries a guardrail
(never mutate/delete history; honor the §11 Q4 derived-state contract),
author the eval rather than deferring it.

## 9. Risk assessment

- **Blast radius: the two hottest write paths in the system** — every
  note ingest/append and every reflection cycle. A bug that writes a
  version row inside the CAS transaction incorrectly could deadlock or
  break the mental-model version guard; a bug in the ingest interception
  could double-write or corrupt idempotency. Mitigation: snapshot in the
  same transaction as the existing write, keyed on the value already
  being read; extensive E2E coverage of the skip/replay paths.
- **Reversibility: additive.** New tables + new read/rollback surface;
  the capture hooks are the only edits to existing paths. Backing out =
  drop the tables and the hooks. History rows are append-only, so a bad
  deploy cannot corrupt prior good history.
- **Storage growth** is real (append-only, full-content snapshots). §11
  Q3 defers retention; note the trade in the migration/README so
  operators know it is unbounded until a retention ticket lands.
- **Likeliest failure modes:**
  1. *Snapshotting the NEW content instead of the prior content* — the
     capture must run before `retain`/the CAS `UPDATE` lands. A test
     must assert the row holds the pre-update body.
  2. *Writing a version on a no-op skip* — the identical-hash skip
     (`ingestion.py:429-431`) must write nothing; assert it.
  3. *Conflating note `as_of` with the entity-relation `as_of`* — they
     are different axes over different tables (§4.3, §11 Q5).
  4. *Rollback silently reverting derived state* (or silently NOT
     reverting it) — the #259 derived-copies semantics must be an
     explicit, tested contract, not an accident (§11 Q4).
  5. *Placing gating tests under `packages/*/tests`* where the loop gate
     never runs them (§4.4). Root `./tests/` only.

## 10. Subtickets (ordered, each a loop-runnable iteration)

1. **`knowledge-versioning-spike`** — resolve the §11 forks and pin the
   design. Read-only + a short decision note: finalize the two table
   schemas and the `ChangeType` enum against
   `procedural_entry_versions`; choose the note-capture interception
   point (§11 Q2); fix the rollback derived-state contract (§11 Q4);
   decide the note `as_of` plumbing (§11 Q5), the mental-model read
   surface (§11 Q6), and migration ownership (one `070` for both tables
   vs. per-subticket `070`/`071`, §7). → verify: every §11 fork has an
   operator-approved answer; no product code changed.
2. **`note-version-history`** — `note_versions` table + its migration
   (numbered per the ST1 decision); capture hooks in `ingestion.py`
   (overwrite, append, create);
   `list_note_versions` + `get_note_at` service/API/route/client + MCP
   `memex_note_versions`, `memex_note_at_version`. → verify:
   `tests/test_note_versioning.py` green; skip/replay idempotency
   intact.
3. **`mental-model-version-history`** — `mental_model_versions` table
   (its own migration only if ST1 chose per-subticket ownership;
   otherwise capture hooks + read surface against the ST2 migration);
   capture hooks in `reflection.py`
   CAS paths + `mental_model_cleanup.py`; read surface. → verify:
   `tests/test_mental_model_versioning.py` green; CAS guard intact.
4. **`knowledge-temporal-queries`** — `note_diff` / version-diff service
   + MCP tools; the note/mental-model `as_of` read (separate axis from
   the cooccurrence filter). → verify:
   `tests/test_knowledge_temporal_queries.py` green.
5. **`knowledge-rollback`** — append-only rollback for notes and mental
   models honoring the §11 Q4 derived-state contract; MCP
   `memex_note_rollback` (+ mental-model equivalent). → verify:
   `tests/test_knowledge_rollback.py` green; history never shortened.

Dependency order: 1 → 2 → 3 → (4, 5). 4 and 5 both depend on 2/3 but are
independent of each other.

## 11. Open questions (forks for the operator)

- **Q1 — Split this epic into separate plan files?** This ticket is one
  file with five subtickets. *Recommendation: yes — promote each §10
  subticket to its own registered plan file so the loop runs them one at
  a time under `require_eval`, starting with the spike. Keep this file
  as the parent context.* Operator's call before pickup.
- **Q2 — Note-capture interception point.** Snapshot the prior body in
  `services/ingestion.py` (`:432`, `:782`) or deeper in
  `extraction/storage.py` (`:151-217`)? *Recommendation: ingestion
  layer — the old row and the `change_reason`/`changed_by` context are
  both in hand there, and `storage.py` runs per-batch (risking multiple
  version rows for one logical update).*
- **Q3 — Retention / storage growth.** Full-content snapshots are
  unbounded. *Recommendation: keep full content for all versions in v1
  (simplest, correct), document the growth in the migration + README,
  and file a separate retention/compression ticket (keep last N full +
  older-as-diffs) rather than building it here.*
- **Q4 — Rollback derived-state semantics (the load-bearing fork, per
  RFC #259).** When a note body is rolled back, what happens to its
  already-derived memory units and the mental models built from them
  (which survive independently)? Options: (a) restore only the body,
  leave derived state as-is (fast, but the note and its units disagree);
  (b) restore the body AND re-run extraction on it so units re-derive
  (consistent, but costs an LLM pass and may re-contradict newer facts);
  (c) restore the body and mark derived units stale without
  re-extraction. *Recommendation: (b) route rollback through the normal
  re-ingest path so extraction re-runs and contradiction detection
  reconciles — this reuses existing machinery and keeps the note and its
  derivations consistent; make the re-extraction explicit in the tool
  contract. Whichever is chosen, it must be a tested, documented
  contract.*
- **Q5 — Note `as_of` plumbing.** Add a new `as_of` axis for
  note/mental-model version reconstruction as its own service path, or
  extend `RetrievalRequest.as_of` (`retrieval/models.py:130`)?
  *Recommendation: a separate service path — the existing `as_of` is
  entity-relation temporal validity (`strategies.py:173-190`) and
  overloading it would couple two unrelated temporal axes. Reuse the
  parameter *name* for agent familiarity, not the code path.*
- **Q6 — Mental-model read surface breadth.** Ship the full
  versions/at/diff/rollback set for mental models, or start with
  versions + rollback and defer mental-model diff? *Recommendation:
  ship versions + rollback for mental models in ST3/ST5; defer
  mental-model diff to the temporal-queries follow-up, since observation
  JSONB diffs are a distinct UX problem from note-text diffs.*
- **Q7 — `change_type` for agent vs. system edits.** The enum spans
  `created/edited/appended/consolidated/reflected/corrected/rolled_back`.
  How is `changed_by`/`change_reason` sourced at each hook (e.g. is the
  agent identity available at `ingestion.py:432`)? *Recommendation: pass
  `changed_by`/`change_reason` down from the caller where available and
  default `changed_by='system'` with a derived `change_reason` at
  internal hooks (reflection → `reflected`, contradiction resolution →
  `corrected`); do not block v1 on a full actor-attribution audit.*

---

**Eval marker:** `.loop/config.json` sets `require_eval: true` — the
loop refuses pickup until an eval marker exists. Co-author it with the
`create-eval` skill before implementation (see §8).
