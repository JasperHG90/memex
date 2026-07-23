# mental-model-version-history: capture the prior mental model on every reflection refresh, and expose version history

> **Epic:** part 3 of 5 of the knowledge-versioning epic (RFC #234).
> **Depends on:** `knowledge-versioning-spike` (schema) and
> `note-version-history` (owns migration `070`, which per spike R4
> creates `mental_model_versions` too; this ticket adds only capture
> hooks + read surface against it).
> **Blocks:** `knowledge-temporal-queries`, `knowledge-rollback`.
> **Full delta investigation:** `.loop/knowledge-versioning-epic.md`.

## 1. Title

Make each reflection refresh append a `mental_model_versions` row
capturing the **prior** `observations`/`entity_metadata` before the
compare-and-swap `UPDATE` (and before evidence-prune deletion), then
expose the history through a read surface mirroring `get_unit_history`.
Per spike Q6, mental models ship versions + rollback (rollback lands in
`knowledge-rollback`); mental-model diff is deferred.

## 2. Size / Effort

**M — two capture hooks inside CAS-guarded paths + a read surface.** The
subtlety is snapshotting inside the same transaction as the CAS `UPDATE`
without breaking the `version`-guarded optimistic-concurrency contract.
The table already exists (migration `070` from `note-version-history`).

## 3. Triggered by

RFC #234: "mental model revisions lose the previous version." Subticket 3
of the approved split.

## 4. Context (today's state, cited)

- **Mental models are real rows**, one per `(entity_id, vault_id)`:
  `MentalModel` / `mental_models` (`sql_models.py:138/144`, unique index
  `:213-220`). Content is the `observations` JSONB (`:156-160`);
  `version` is an int counter "incremented on each update"
  (`:172-174`) — no prior content stored.
- **Overwrite path A — full reflection finalize (CAS).**
  `reflection.py:669-763`: `sa_update(MentalModel)` guarded on
  `MentalModel.version == claimed_version` (`:723`) replacing
  `observations`/`entity_metadata`/`embedding` and setting
  `version = version + 1` (`:722-729`); in-memory mirror at `:758-760`.
- **Overwrite path B — surgical single-observation refresh (CAS).**
  `reflection.py:1090-1109`, values at `:1095-1097`.
- **Prune path.** `services/mental_model_cleanup.py:76-81` mutates
  `observations` in place (`flag_modified`) and deletes the whole row if
  it prunes to zero.
- **What is lost on refresh:** prior observation titles/content, their
  evidence citations, the entity description/category, the embedding —
  except observations the LLM merge carries forward.
- **The read template:** `get_unit_history` end to end (service
  `units.py:613`, facade `api.py:1609-1623`, route
  `server/memories.py:558-562`, client `client.py:1324`, MCP
  `server.py:409-474`).
- **Schema:** `MentalModelVersion` / `mental_model_versions` defined in
  the spike; table created by migration `070` in `note-version-history`.

## 5. Non-goals / out of scope

- No mental-model `as_of` reconstruction or diff (spike Q6 defers the
  diff; `as_of` for mental models is scoped in `knowledge-temporal-queries`).
- No mental-model rollback here (`knowledge-rollback`).
- No note versioning (`note-version-history`).
- No new migration unless spike R4 chose per-subticket ownership (it
  recommended one shared `070`).
- No change to reflection's synthesis logic, the CAS `version` counter
  semantics, or the merge/carry-forward behavior.
- No memory-unit versioning.

## 6. Requirements & restrictions

**Must achieve:**

- R1. On the full-cycle finalize (`reflection.py:722-729`), snapshot the
  **prior** `observations`/`entity_metadata` into a
  `mental_model_versions` row with `change_type=reflected`, in the SAME
  transaction as the CAS `UPDATE`, keyed on `claimed_version`.
- R2. On the surgical refresh (`reflection.py:1090-1097`), snapshot the
  prior state likewise (`change_type=reflected` or `corrected` when the
  refresh follows a deprioritization — spike Q7 sourcing).
- R3. On prune (`mental_model_cleanup.py:76-81`), snapshot the prior
  `observations` before the in-place prune / row deletion
  (`change_type=corrected`).
- R4. The CAS `version` guard still holds: a concurrent `claimed_version`
  mismatch must NOT write a version row and must NOT double-update (no
  lost update, no orphan version row).
- R5. Version rows append-only, monotonic per `mental_model_id`, unique
  `(mental_model_id, version)`; record `change_type`, `changed_by`
  (default `system`), `change_reason`, `created_at`, snapshot
  `observations`/`entity_metadata`.
- R6. Read surface: `list_mental_model_versions`,
  `get_mental_model_at(version)` through service → API → route
  (`require_read`) → client → MCP tools mirroring `get_unit_history`.

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md`: tests-first; testcontainer
  Postgres; no `skip`/`xfail`/`# type: ignore`; gating tests in root
  `./tests/`.
- `CLAUDE.md:180-196`: MCP tool descriptions ≤ 1,200 chars, fenced by
  the MCP budget tests; SSOT description home.
- The snapshot MUST be inside the CAS transaction so it commits atomically
  with the `UPDATE` — a snapshot committed separately could survive a
  rolled-back CAS and create a phantom version.
- Async I/O, single quotes, line 100, mypy strict.
- `.claude/rules/pre-existing-issues.md`; `.claude/rules/adversarial-reviews.md`.

## 7. Code surface

- **Capture hooks** `packages/core/src/memex_core/memory/reflect/reflection.py:669-763`
  and `:1090-1109` — snapshot prior state inside the CAS transaction.
  **Edit.**
- **Prune hook** `packages/core/src/memex_core/services/mental_model_cleanup.py:76-81`
  — snapshot before prune/delete. **Edit.**
- **Service** — a mental-model versioning service (new module under
  `services/`, or extend the mental-model service) with
  `list_mental_model_versions`, `get_mental_model_at`. **New/Edit.**
- **Facade** `packages/core/src/memex_core/api.py` — delegators (mirror
  `get_unit_history` `:1609-1623`). **Edit.**
- **Route** `packages/core/src/memex_core/server/` — new
  `@router.get('/mental-models/{id}/versions', ...)` +
  `.../versions/{version}` with `require_read`, `_handle_error` (new
  module or the memories/entities router). **Edit/New.**
- **Client** `packages/common/src/memex_common/client.py:1324` template.
  **Edit.**
- **DTO** `packages/common/src/memex_common/schemas.py` —
  `MentalModelVersionDTO`. **Edit/New.**
- **MCP** `packages/mcp/src/memex_mcp/server.py:409-474` template —
  `memex_mental_model_versions`, `memex_mental_model_at_version`
  (`tags={'read'}`, `readOnlyHint`). **Edit.**
- Read-only: `sql_models.py:138-220` (MentalModel),
  `MentalModelVersion` (added in the spike).

## 8. Tests & validation gates

**Gates:** `just test` + `just prek`. Run `uv run pytest packages/mcp/tests`
for the description fences.

**Reproducing test first — `tests/test_mental_model_versioning.py`**
(root `./tests/`, testcontainer Postgres):
- A reflection refresh writes a `mental_model_versions` row holding the
  prior `observations` with monotonic `version`.
- CAS guard: simulate a `claimed_version` mismatch — assert NO version
  row is written AND the live row is unchanged (no lost update, no
  phantom version).
- Prune writes a version holding the pre-prune `observations`; a
  prune-to-zero writes the final version before deleting the row.
- Read: `list_mental_model_versions` ordered; `get_mental_model_at`
  returns the historical observations.

**Eval marker (required):** `.loop/evals/mental-model-version-history.md`
pins the guardrails (refresh preserves prior version; CAS guard intact).
Validate with `loopctl eval mental-model-version-history`.

## 9. Risk assessment

- **Blast radius: the reflection loop** — background, CAS-guarded,
  concurrency-sensitive. A snapshot outside the transaction, or one that
  perturbs the `version` guard, could corrupt the optimistic-concurrency
  contract. Mitigation: snapshot inside the same transaction; test the
  mismatch path.
- **Reversibility:** additive; back out = drop hooks + read surface (the
  table is shared with notes, dropped only if the whole feature backs
  out).
- **Failure modes:** (1) phantom version from a rolled-back CAS — snapshot
  in-transaction, test the mismatch; (2) snapshotting post-update state;
  (3) prune-to-zero losing the final version before row deletion — test
  it; (4) JSONB snapshot cost on large observation sets (acceptable per
  spike Q3 full-snapshot decision); (5) MCP budget overflow.

## 10. Subtickets (ordered steps)

1. Confirm `mental_model_versions` exists (migration `070`); if spike R4
   chose per-subticket ownership, generate `071` instead. → verify:
   table present.
2. Add the CAS-transaction snapshot to both reflection paths. → verify:
   refresh test + CAS-mismatch test green.
3. Add the prune snapshot. → verify: prune test green.
4. Service + facade + route + client + DTO + MCP tools. → verify: read
   tests + `uv run pytest packages/mcp/tests` green.
5. Adversarial review. → verify: clean; `just test` + `just prek` green.

## 11. Open questions

Substantive forks settled in the spike (Q6 versions+rollback now,
mental-model diff deferred; Q7 `changed_by` sourcing). No new fork. One
implementation detail for the implementer, not an operator fork: whether
the mental-model version read surface lives in a new router/service
module or extends an existing one — pick the lower-drift option and note
it in the diff.

---

**Eval marker:** `.loop/evals/mental-model-version-history.md`
(`require_eval: true`).
