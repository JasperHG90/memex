# memory-unit-usage-tracking-columns

## 1. Title

Add usage-tracking substrate columns (`access_count`, `last_accessed`,
`importance_score`) to the `MemoryUnit` model with a backward-compatible
alembic migration, and increment `access_count` / stamp `last_accessed` on
the retrieval read path via one batched, off-hot-path UPDATE. This is
RFC #220 Phase 1 ONLY — pure substrate that five later RFCs read; no
cognitive behaviour ships here.

## 2. Size / Effort

**S.** Three additive columns on one existing SQLModel, one additive
alembic migration (metadata-only column adds), and one batched UPDATE folded
into an existing background-task mechanism. No new service, no new module, no
new query planner path, no lifecycle state machine. The `importance_score`
column ships with NO writer this phase (substrate for RFC #220 Phase 4). The
effort is dominated by the migration test against the testcontainer and the
read-path integration test, not by surface area.

## 3. Triggered by

Triage decision this session for RFC #220 ("Adaptive Memory Lifecycle"),
`.temp/issues/rfc-220.md` and `gh issue view 220`. RFC #220 Implementation
Order names Phase 1 ("Access Tracking") as the prerequisite for all four
later phases: "Add `access_count`, `last_accessed`, `importance_score` ...
columns ... Add a database migration. Wire tracking into retrieval results."
The five downstream consumers (the RFC's Admission, Utilisation,
Consolidation, and Forgetting phases, plus the related RFCs #210/#204/#181)
read these columns; none is in scope here. This ticket is the substrate they
depend on and nothing more.

## 4. Context

**The model.** `MemoryUnit` is the append-only fact row,
`packages/core/src/memex_core/memory/sql_models.py:625` (table
`memory_units`, `__tablename__` at
`packages/core/src/memex_core/memory/sql_models.py:631`). It already carries
a family of operational scoring/lifecycle columns that this ticket mirrors
in shape:

- `unused_co_count` (`packages/core/src/memex_core/memory/sql_models.py:713`)
  — `Column(Integer, nullable=False, server_default='0')`. The exact shape
  `access_count` should copy.
- `last_outcome_at`
  (`packages/core/src/memex_core/memory/sql_models.py:788`) —
  `Column(TIMESTAMP(timezone=True), nullable=True)`. The exact shape
  `last_accessed` should copy.
- `importance` (`packages/core/src/memex_core/memory/sql_models.py:767`) —
  `Column(Float, nullable=True)`. NOTE: this column ALREADY EXISTS and is a
  DIFFERENT concept from the RFC's `importance_score` (see §11 Q1). It is the
  intent-class-derived signal (permanent=1.0 / durable=0.7 / ephemeral=0.3).
  The RFC's `importance_score` is the Phase-4 composite forgetting score
  (`α·recency + β·access_frequency + γ·entity_centrality`). They coexist.

These operational counters live ONLY on the SQLModel `MemoryUnit`, NOT on the
shared Pydantic base `MemoryUnitBase`
(`packages/common/src/memex_common/schemas.py:484`) nor the DTO
`MemoryUnitDTO` (`packages/common/src/memex_common/schemas.py:~535`). The DTO
docstring explicitly states it is "PYDANTIC-ONLY ... never used for DDL". So
the three new columns go on the SQLModel `MemoryUnit` and nowhere else — this
bounds the surface (see §5).

**The migration convention.** Sequential numbered revisions in
`packages/core/src/memex_core/alembic/versions/`. Current head is
`069_nodes_chunks_search_tsvector`
(`packages/core/src/memex_core/alembic/versions/069_nodes_chunks_search_tsvector.py`);
`revision`/`down_revision`/`branch_labels`/`depends_on` module globals at the
file's top, a WHY-first docstring, an explicit LOCK NOTE, and a reversible
`downgrade()`. The next revision is `070_...`, `down_revision =
'069_nodes_chunks_search_tsvector'`.

**The read path.** `MemoryEngine.recall()`,
`packages/core/src/memex_core/memory/engine.py:291`, is the single
service-layer retrieval entry. It calls `self.retrieval.retrieve(session,
request)` (`packages/core/src/memex_core/memory/engine.py:303`), gets back
the ranked `list[MemoryUnit]` (`results`), and returns `(results,
resonance_task)` (`packages/core/src/memex_core/memory/engine.py:330`).
`resonance_task` is a zero-arg background coroutine built at
`packages/core/src/memex_core/memory/engine.py:316` (`_do_resonance_update`,
which opens its OWN session via the stored `session_factory`) and assigned at
`packages/core/src/memex_core/memory/engine.py:328`. The caller schedules it:
`server/retrieval.py:81` guards `if resonance_task is not None:` and
`packages/core/src/memex_core/server/retrieval.py:82` calls
`background_tasks.add_task(resonance_task)`. THIS is the mechanism the
access-count bump reuses (see §6 R3).

`recall()` has three callers, all in
`packages/core/src/memex_core/services/search.py`:
- `retrieve()` at `packages/core/src/memex_core/services/search.py:56` —
  returns the `(units, task)` tuple up through `api.search`
  (`packages/core/src/memex_core/api.py:1631`) to `server/retrieval.py:50`,
  where the task IS scheduled.
- `search()` at `packages/core/src/memex_core/services/search.py:121` — same
  upward return; task scheduled.
- `summarize_search_results()`'s internal `_search_one` at
  `packages/core/src/memex_core/services/search.py:295` — `units, _ =
  await self.memory.recall(...)`, which DISCARDS the task. This is an
  internal per-sub-query LLM-summary fan-out, not a user-facing retrieval.
  Discarding the bump here is DESIRABLE (avoids inflating counts by the
  sub-query multiple) — see §6 R3 and §11 Q3.

**The write-pattern precedent.** `record_outcome` already does exactly the
batched single-statement counter bump this ticket needs:
`packages/core/src/memex_core/services/outcomes.py:259` —
`update(MU).where(MU.id.in_(ids), MU.vault_id == vault_uuid).values(values)`
where `values` sets `counter_field: MU.__table__.c[counter_field] + 1` and
optionally `last_outcome_at: now`
(`packages/core/src/memex_core/services/outcomes.py:254`). One UPDATE over an
id list, not per-row. The read-path bump copies this shape verbatim for
`access_count` / `last_accessed`.

## 5. Non-goals / out of scope

Triage locked Phase 1 substrate only. Explicitly OUT:

- **No other RFC #220 columns.** RFC #220's "Impact" section also names
  `ephemeral` and `archived`. Do NOT add them. Phase 1 is `access_count`,
  `last_accessed`, `importance_score` only.
- **No `importance_score` writer / formula.** Add the column as nullable
  substrate with NO code that computes or writes it. The `α·recency +
  β·access_frequency + γ·entity_centrality` formula is RFC #220 Phase 4
  (Forgetting) and is not in this ticket.
- **No Note / Node columns.** RFC #220 proposes these fields on `Note`/`Node`
  too; triage scoped THIS ticket to the memory-unit model. Do NOT touch
  `Note` (`packages/core/src/memex_core/memory/sql_models.py:241`) or `Node`
  (`packages/core/src/memex_core/memory/sql_models.py:504`).
- **No admission gate, no utilisation module/service, no consolidation
  scheduler, no forgetting policy, no lifecycle state machine.** RFC #220
  Phases 2–5. None ship here.
- **No change to retrieval ranking, filtering, or ordering.** `access_count`
  and friends do NOT feed the query, the RRF fusion, MMR, or any `WHERE` /
  `ORDER BY` this phase. They are written on read and read by no one yet.
- **No new DTO / API / MCP surface.** The columns stay on the SQLModel
  `MemoryUnit`. Do NOT add them to `MemoryUnitBase`
  (`packages/common/src/memex_common/schemas.py:484`), `MemoryUnitDTO`, the
  HTTP response schemas, or any MCP tool payload. No consumer exists yet;
  surfacing them is a later phase's job.
- **No `retrieve()`-level signature change** if it can be avoided — reuse the
  existing `resonance_task` return slot / background-task mechanism rather
  than adding a second returned task (see §6 R3).

## 6. Requirements & restrictions

Triage-locked (record as requirements, not open forks):

- **R1. Columns on the SQLModel `MemoryUnit` only, mirroring existing
  shapes.**
  - `access_count: int` — `Column(Integer, nullable=False,
    server_default='0')`, copying `unused_co_count`
    (`packages/core/src/memex_core/memory/sql_models.py:713`).
  - `last_accessed: datetime | None` —
    `Column(TIMESTAMP(timezone=True), nullable=True)`, copying
    `last_outcome_at`
    (`packages/core/src/memex_core/memory/sql_models.py:788`).
  - `importance_score: float | None` — `Column(Float, nullable=True)`,
    copying `importance`
    (`packages/core/src/memex_core/memory/sql_models.py:767`), with a
    docstring that distinguishes it from the existing `importance` column
    (see §11 Q1). NO writer this phase.
  Each `Field(...)` carries a `description=` docstring consistent with the
  surrounding columns.

- **R2. Backward-compatible, reversible migration `070`.** Additive columns
  only. `access_count` NOT NULL with `server_default='0'` so existing rows
  backfill to 0 without a rewrite (constant-default `ADD COLUMN` is
  metadata-only in the Postgres version this project targets — pgvector
  `pg18`, per `tests/conftest.py:20`). `last_accessed` and `importance_score`
  nullable with no default (existing rows → NULL). `downgrade()` drops the
  three columns. Include the WHY-first docstring + LOCK NOTE style of
  `069_nodes_chunks_search_tsvector`; here the note states the adds are
  metadata-only / non-rewriting and therefore cheap. Do NOT add indexes on
  these columns this phase (no reader needs them yet; an index is speculative
  until Phase 4 queries them).

- **R3. Read-path write = ONE batched UPDATE, off the response hot path,
  reusing the existing background-task mechanism.** On the primary
  user-facing retrieval path, after `recall()` produces `results`, bump
  `access_count += 1` and set `last_accessed = now` for the returned units in
  a SINGLE statement: `update(MemoryUnit).where(MemoryUnit.id.in_(ids),
  MemoryUnit.vault_id == ...).values(access_count=MemoryUnit.__table__.c.access_count
  + 1, last_accessed=now)` — the exact shape of
  `packages/core/src/memex_core/services/outcomes.py:259`. Requirements on
  HOW it is wired:
  - It MUST run in a background coroutine that opens its OWN session (mirror
    `_do_resonance_update`,
    `packages/core/src/memex_core/memory/engine.py:316`), NOT inline in the
    request/response path. The read must not block on the write.
  - It MUST be scheduled through the SAME `resonance_task` →
    `background_tasks.add_task` path
    (`packages/core/src/memex_core/server/retrieval.py:82`). Recommended:
    fold the bump into the coroutine `recall()` already returns so no return
    signature changes and no second `add_task` is needed. Consequence — and
    this is intended — the `search.py:295` fan-out path that discards the
    task does NOT count sub-query reads (see §11 Q3).
  - It MUST be idempotent-safe against the returned id list: de-duplicate
    ids, and rely on `WHERE id IN (...)` naturally no-op'ing ids that are not
    `memory_units` rows (synthesized/virtual observation projections carry MU
    ids that map to real rows; the UPDATE touches only rows that exist).
  - It MUST scope by `vault_id` in the `WHERE` (copy the outcomes precedent)
    so a bump never crosses vault boundaries.
  - Count exactly once per returned unit per retrieval call — the bump is +1
    on the distinct returned ids, not +1 per occurrence across expanded
    sub-queries.

- **R4. `importance_score` has no writer and no reader this phase.** It is
  present in the model + migration and nothing else. Do not compute it, do
  not populate it on write, do not read it in retrieval.

Repo principles this change must respect, each cited to where the repo states
it:

- **Every change ships a test; a behaviour is not done until a test in the
  gate exercises it** (`.claude/rules/python-testing.md`, constraint
  `all-code-needs-tests`). See §8.
- **Surgical changes** — touch the model, the one migration, and the one
  read-path bump; do not refactor `recall()`, the strategies, or adjacent
  scoring code (`CLAUDE.md` §3).
- **Simplicity first** — no index, no config knob, no `importance_score`
  machinery, no speculative generality (`CLAUDE.md` §2).
- **Tests are real code, linted and type-checked; never `# type: ignore`,
  `skip`, or `xfail` to green a gate** (`.claude/rules/python-testing.md`,
  constraint `tests-are-real-code`).
- **All I/O is async** (`CLAUDE.md`, Code style). The bump uses
  `await session.exec(update(...))`.
- **Fix, never silence, any pre-existing failure a gate surfaces**
  (`.claude/rules/pre-existing-issues.md`).
- **Run the adversarial review before declaring done**
  (`.claude/rules/adversarial-reviews.md`).

## 7. Code surface

- `packages/core/src/memex_core/memory/sql_models.py:625` (`MemoryUnit`) —
  add the three `Field(...)` columns per R1, placed alongside the existing
  operational counters (near `unused_co_count` /`last_outcome_at`/
  `importance`, i.e. `packages/core/src/memex_core/memory/sql_models.py:713`
  – `packages/core/src/memex_core/memory/sql_models.py:796`). Do NOT add
  entries to `__table_args__`
  (`packages/core/src/memex_core/memory/sql_models.py:842`) — no index/check
  this phase.
- `packages/core/src/memex_core/alembic/versions/070_memory_unit_usage_tracking.py`
  (NEW) — `revision = '070_memory_unit_usage_tracking'`, `down_revision =
  '069_nodes_chunks_search_tsvector'`. `upgrade()` = three
  `op.add_column('memory_units', sa.Column(...))`; `downgrade()` = three
  `op.drop_column`. Docstring per R2.
- `packages/core/src/memex_core/memory/engine.py:291` (`recall`) — after
  `results` is produced
  (`packages/core/src/memex_core/memory/engine.py:303`), build the
  access-bump into the background coroutine returned at
  `packages/core/src/memex_core/memory/engine.py:328`/`:330`. Mirror the
  `_do_resonance_update` self-session pattern
  (`packages/core/src/memex_core/memory/engine.py:316`). If resonance is
  absent (`resonance_ctx` falsy) the bump must still run, so the returned
  task cannot be gated solely on `resonance_ctx` — return a bump-only
  coroutine when resonance is absent, or a combined one when present. (This
  is the one subtlety: today `resonance_task` stays `None` when
  `resonance_ctx` is falsy, and `server/retrieval.py:81` skips a `None`
  task. Ensure the access bump still fires in that branch.)
- `packages/core/src/memex_core/server/retrieval.py:81` — no change needed if
  the bump is folded into the existing returned task; the existing
  `background_tasks.add_task(resonance_task)`
  (`packages/core/src/memex_core/server/retrieval.py:82`) then schedules it.
  Confirm the `is not None` guard still holds for the bump-when-no-resonance
  case above.

Test homes (each test in §8 is listed here):

- `tests/test_e2e_memory_unit_usage_tracking.py` (NEW, root `./tests/`, DB
  via the session testcontainer in `tests/conftest.py:20`; NOT
  `integration`-marked so `just test` runs it) — migration-applied schema +
  read-path bump behaviour.
- `packages/core/tests/unit/` (existing tree, run manually) — a migration
  up/down structural test, modelled on
  `packages/core/tests/unit/test_alembic_031_resolved_by.py`.

## 8. Tests & validation gates

**Eval marker (acceptance layer):**
`.loop/evals/memory-unit-usage-tracking-columns.md` — AUTHORED and validated
(`loopctl eval memory-unit-usage-tracking-columns` → `valid`). 8 deterministic
guardrails at a hard 100% bar against the testcontainer metastore: (a) a
retrieval that returns a unit sets its `access_count == 1`; (b) that same
retrieval stamps `last_accessed` non-NULL; (c) two retrievals →
`access_count == 2` (idempotent +1 per call); (d) one `expand_query=True`
call counts +1, not +N per sub-query; (e) a unit never returned keeps
`access_count == 0` / `last_accessed IS NULL`; (f) `importance_score` stays
NULL (no writer); (g) pre-existing rows backfill to `access_count == 0` after
`070` upgrade (backward-compatible); (h) the `summarize_search_results`
fan-out (`services/search.py:295`, discards the task) does NOT count —
row (h) flips if open question Q3 is decided the other way.

Gates (verified this session):

- **`just test`** = `uv run pytest tests` (`justfile:66`). Collects the root
  `./tests/` tree; `addopts = "... -m 'not integration'"`
  (`pyproject.toml:78`) excludes only `integration`-marked tests. Root E2E
  tests get a real Postgres via the session-scoped `pgvector/pgvector:pg18`
  testcontainer (`tests/conftest.py:20`,
  `tests/conftest.py:121`), with the alembic head stamped by the
  session fixture (`tests/conftest.py:239`, `_stamp_alembic_head` at
  `tests/conftest.py:265`). So the gate CAN and MUST run the DB-backed
  behaviour test — it just must NOT carry the `integration` marker.
- **`just prek`** = `uv run prek run -a` (`justfile:62`) = ruff + mypy +
  format.

Tests to add:

- **Loop-gating behaviour test** —
  `tests/test_e2e_memory_unit_usage_tracking.py`, async, NOT
  `integration`-marked. It MUST assert, against the real metastore:
  1. **Schema present.** The `memory_units` table has `access_count`,
     `last_accessed`, `importance_score` after the fixtures apply head
     (proves the model + migration agree). A fresh-inserted unit reads
     `access_count == 0`, `last_accessed is None`, `importance_score is None`
     (R1/R2 defaults).
  2. **Read-path bump.** Ingest content producing at least one memory unit,
     run a retrieval that returns it through the server path (so the
     background task is scheduled and awaited — follow how existing e2e tests
     drain background tasks; e.g. the resonance/contradiction e2e tests), and
     assert the returned unit's row now has `access_count == 1` and
     `last_accessed` non-NULL (R3).
  3. **Idempotent +1 per call.** A second identical retrieval yields
     `access_count == 2` (not more), proving one increment per call even when
     query expansion fans out (R3).
  4. **Non-retrieved untouched.** A unit not matched by the query keeps
     `access_count == 0` / `last_accessed is None`.
  5. **`importance_score` unwritten.** Remains NULL through ingest + multiple
     retrievals (R4).
  Cover the parametrizable cases with `@pytest.mark.parametrize` where it
  reads naturally (`.claude/rules/python-testing.md`, "Writing good tests").
- **Migration structural test (manual)** —
  `packages/core/tests/unit/test_alembic_070_usage_tracking.py`, modelled on
  `packages/core/tests/unit/test_alembic_031_resolved_by.py`: assert
  `upgrade()` adds the three columns and `downgrade()` drops them, and that
  `down_revision` chains to `069_nodes_chunks_search_tsvector`. Runs under the
  package dir, not the loop gate.

Both `just test` and `just prek` must pass before done. Run the adversarial
review (`.claude/rules/adversarial-reviews.md`) after gates are green.

## 9. Risk assessment

- **Blast radius: narrow.** Three additive, defaulted/nullable columns on one
  table + one batched UPDATE reusing an existing background-task slot. No
  read consumer this phase, so nothing downstream can misread the new values.
- **Reversibility: high.** `downgrade()` drops the columns; the read-path
  bump reverts by removing the folded coroutine. No data migration, no
  backfill beyond the constant server-default.
- **Likeliest failure modes:**
  1. **Bump on the hot path.** Doing the UPDATE inline in `recall()` /
     `retrieve()` instead of in the background coroutine adds a synchronous
     write to every search — the exact per-search UPDATE fan-out the triage
     flagged. Guard: R3 mandates the background-task mechanism; the behaviour
     test drains the task rather than expecting a synchronous write.
  2. **Per-occurrence over-count.** Bumping once per expanded sub-query or per
     RRF occurrence instead of once per distinct returned id inflates counts.
     Guard: de-dupe ids; §8 test case 3 pins `== 2` after two calls.
  3. **Missed bump when resonance is absent.** `resonance_task` is `None` when
     `resonance_ctx` is falsy
     (`packages/core/src/memex_core/memory/engine.py:309`,
     `packages/core/src/memex_core/server/retrieval.py:81`); a naive fold
     would silently skip the access bump on those retrievals. Guard: §7 notes
     the branch; §8 test case 2 must exercise a retrieval that does not
     trigger resonance.
  4. **Naming confusion with existing `importance`.** Two float columns named
     `importance` and `importance_score` on the same table. Guard: R1
     mandates a distinguishing docstring; §11 Q1 records the decision.
  5. **Cross-vault bump.** Omitting `vault_id` from the UPDATE `WHERE` could
     touch same-id rows across vaults. Guard: R3 requires the vault scope,
     copying the outcomes precedent.

## 10. Subtickets

1. Add the three columns to `MemoryUnit`
   (`packages/core/src/memex_core/memory/sql_models.py:625`) and write
   migration `070` (up = add 3 columns, down = drop 3). Verify: migration
   structural test green; fixtures apply head cleanly.
2. Fold the batched access bump into `recall()`'s returned background task
   (`packages/core/src/memex_core/memory/engine.py:291`), copying the
   `update(MU)...values(...)` shape from
   `packages/core/src/memex_core/services/outcomes.py:259` and the
   self-session pattern from
   `packages/core/src/memex_core/memory/engine.py:316`. Handle the
   no-resonance branch. Verify: root e2e behaviour test green under `just
   test`.
3. Run `just prek` (ruff + mypy) and fix any typing on the new fields /
   coroutine. Verify: green.

Order is dependency-driven: columns + migration first (nothing to write into
otherwise), then the read-path bump, then the lint/type gate.

## 11. Open questions

- **Q1 (naming — the one real design decision): `importance_score` vs the
  existing `importance` column.** `MemoryUnit` already has `importance`
  (`packages/core/src/memex_core/memory/sql_models.py:767`), the
  intent-class-derived signal. RFC #220 Phase 1 names the new column
  `importance_score` (the future composite forgetting score). They are
  genuinely different quantities. **Recommendation:** keep the RFC name
  `importance_score` verbatim — it is the identifier the five downstream RFC
  #220 phases reference, so renaming it here would just move the reconciliation
  cost downstream — and add a docstring on both columns cross-referencing the
  other so no future reader conflates them. Adopt unless the operator prefers
  disambiguating the older column instead.
- **Q2: does `access_count` / `last_accessed` belong on the DTO too?** No
  consumer reads them this phase, and the existing operational counters
  (`success_co_count`, `unused_co_count`, `last_outcome_at`) are SQLModel-only
  — absent from `MemoryUnitBase`
  (`packages/common/src/memex_common/schemas.py:484`) and `MemoryUnitDTO`.
  **Recommendation:** SQLModel-only, matching the established pattern; a later
  phase that surfaces usage adds DTO/API fields when it has a reader. Confirm.
- **Q3: which retrieval paths count as an "access"?** The three `recall()`
  callers differ: `search.py:56` and `search.py:121` return the task upward
  and it IS scheduled; `search.py:295` (`summarize_search_results` fan-out)
  discards it. **Recommendation:** fold the bump into the returned task so the
  two user-facing paths count and the internal per-sub-query summary fan-out
  does not (counting it would multiply the increment by the sub-query count).
  This is behaviourally correct, not a gap. Confirm the internal fan-out
  should not count; if it should, R3 needs a second explicit schedule at
  `packages/core/src/memex_core/services/search.py:295`.
- **Q4: index on these columns?** Phase 4 (Forgetting) will query/sort by
  `importance_score` and `last_accessed`. **Recommendation:** no index this
  phase — no reader exists, and an unused index is speculative (`CLAUDE.md`
  §2). The Phase 4 ticket adds the index alongside the query that needs it.
