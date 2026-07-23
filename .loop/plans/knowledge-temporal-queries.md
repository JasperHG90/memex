# knowledge-temporal-queries: as-of reconstruction and between-version diff over note and mental-model history

> **Epic:** part 4 of 5 of the knowledge-versioning epic (RFC #234).
> **Depends on:** `note-version-history` and
> `mental-model-version-history` (the version tables + read surface must
> exist). Independent of `knowledge-rollback`.
> **Full delta investigation:** `.loop/knowledge-versioning-epic.md`.

## 1. Title

Add temporal reconstruction — return a note's (and a mental model's)
content **as of** a timestamp — and a between-versions diff, exposed as
MCP tools. This is the new temporal axis over the version tables and is
kept strictly separate from the pre-existing entity-relation `as_of`
(spike Q5). Per spike Q6, mental-model diff is deferred; mental models
get `as_of` reconstruction only.

## 2. Size / Effort

**M — one temporal-select service method per surface + a diff, plus MCP
tools.** No new tables. The risk is conceptual (not conflating the two
`as_of` axes), not volume.

## 3. Triggered by

RFC #234: temporal queries ("what did I know about X on date Y?") and
diffs. This history is the temporal evidence conflict-typing (#233) needs
to classify a conflict as TEMPORAL rather than HARD. Subticket 4 of the
approved split.

## 4. Context (today's state, cited)

- **Version tables exist** (`note_versions`, `mental_model_versions`) with
  append-only rows carrying `version`, `created_at`, and the snapshot
  content, plus `list_*`/`get_*_at(version)` read surfaces from
  subtickets 2–3.
- **A DIFFERENT `as_of` already exists — do not reuse its path.**
  `RetrievalRequest.as_of` (`packages/core/src/memex_core/memory/retrieval/models.py:130`)
  drives `_apply_as_of_filter`
  (`packages/core/src/memex_core/memory/retrieval/strategies.py:173-190`)
  over `EntityCooccurrence.valid_from/valid_to`
  (`sql_models.py:1197-1207`), threaded at `engine.py:703-705`. That is
  entity-relation temporal validity — when two entities were related —
  NOT object content history. The new note/mental-model `as_of` reads the
  version tables and must not be routed through the cooccurrence filter.
- **Read-tool template:** `get_unit_history` end to end (service
  `units.py:613`, facade `api.py:1609-1623`, route
  `server/memories.py:558-562`, client `client.py:1324`, MCP
  `server.py:409-474`).

## 5. Non-goals / out of scope

- No rollback (`knowledge-rollback`).
- No change to the entity-relation `as_of`
  (`strategies.py:173-190`) or `RetrievalRequest.as_of` — the new axis is
  additive and separate.
- No mental-model diff (spike Q6 defers it).
- No `EvolutionTracker` analytics (stability/volatility/frequency).
- No new version-writing; this ticket is read-only over existing history.
- No general time-travel of retrieval results — reconstruction is
  per-object (a specific note / mental model), not a whole-vault `as_of`
  search.

## 6. Requirements & restrictions

**Must achieve:**

- R1. `get_note_at(note_id, as_of: datetime)` returns the note content of
  the version live at `as_of` — the latest `note_versions` row with
  `created_at <= as_of` (or the current body if `as_of` is after the last
  version). Reads the version table only; does not touch the cooccurrence
  `as_of` filter.
- R2. `get_mental_model_at(mental_model_id, as_of: datetime)` — the
  analogous reconstruction over `mental_model_versions`.
- R3. `diff_note_versions(note_id, from_version, to_version)` returns a
  textual diff between two note versions (a stable line/unit diff — reuse
  a stdlib differ, e.g. `difflib`, no new dependency without `uv add`).
- R4. MCP tools `memex_note_at` (accepting a version OR an `as_of`
  timestamp — extend the `get_note_at` tool from subticket 2 rather than
  a second tool if cleaner) and `memex_note_diff`, mirroring the
  `get_unit_history` shape.
- R5. The two `as_of` axes stay independent: a note `as_of` query returns
  note content unaffected by any cooccurrence validity, and vice versa —
  asserted by test.

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md`: tests-first; testcontainer Postgres;
  no `skip`/`xfail`/`# type: ignore`; gating tests in root `./tests/`.
- `.claude/rules/uv-installer.md`: if a diff library is needed beyond
  stdlib, add it with `uv add`, not `uv pip`. Prefer stdlib `difflib`.
- `CLAUDE.md:180-196`: MCP descriptions ≤ 1,200 chars, fenced by the MCP
  budget tests; SSOT description home.
- Async I/O, single quotes, line 100, mypy strict.
- `.claude/rules/pre-existing-issues.md`; `.claude/rules/adversarial-reviews.md`.

## 7. Code surface

- **Service** `packages/core/src/memex_core/services/notes.py` — extend
  `get_note_at` to accept `as_of`; add `diff_note_versions`. The
  mental-model versioning service — add `get_mental_model_at(as_of=...)`.
  **Edit.**
- **Facade** `packages/core/src/memex_core/api.py` — delegators. **Edit.**
- **Route** `packages/core/src/memex_core/server/notes.py` (and the
  mental-model router) — add `as_of` query param to the version-at route;
  add a `.../versions/diff` GET. `require_read`, `_handle_error`. **Edit.**
- **Client** `packages/common/src/memex_common/client.py` — `get_note_at`
  (as_of), `diff_note_versions`. **Edit.**
- **DTO** `packages/common/src/memex_common/schemas.py` — a diff DTO
  (from/to version, unified-diff text or structured hunks). **Edit/New.**
- **MCP** `packages/mcp/src/memex_mcp/server.py:409-474` template —
  `memex_note_diff` (+ `as_of` on the at-version read tool);
  `tags={'read'}`/`{'search'}`, `readOnlyHint`, `ToolError`. **Edit.**
- Read-only: `strategies.py:173-190`, `retrieval/models.py:130` (the
  OTHER `as_of` — confirm it is untouched).

## 8. Tests & validation gates

**Gates:** `just test` + `just prek`. Run `uv run pytest packages/mcp/tests`
for the description fences.

**Reproducing test first — `tests/test_knowledge_temporal_queries.py`**
(root `./tests/`, testcontainer Postgres):
- Seed a note with three versions at distinct `created_at` values;
  `get_note_at(as_of=T)` for a T between v2 and v3 returns v2's content;
  `as_of` after the last version returns the current body; `as_of` before
  v1 returns empty/not-found (assert the chosen sentinel).
- `diff_note_versions(v1, v3)` reports the changed lines.
- Mental-model `as_of` reconstruction returns the historical
  observations.
- **Axis independence:** a note `as_of` query returns note content
  regardless of any `EntityCooccurrence.valid_from/valid_to`, and a
  cooccurrence `as_of` retrieval is unaffected by note versions —
  asserting the two axes do not cross.

**Eval marker (required):** `.loop/evals/knowledge-temporal-queries.md`
pins the guardrails (`as_of` reconstructs the historical content; the two
`as_of` axes stay separate). Validate with
`loopctl eval knowledge-temporal-queries`.

## 9. Risk assessment

- **Blast radius: read-only over existing history.** No writes, no schema.
  Low.
- **Reversibility:** trivial — remove the read methods/tools.
- **Failure modes:** (1) conflating the two `as_of` axes — the load-
  bearing risk; assert independence; (2) off-by-one on the `created_at
  <= as_of` boundary (inclusive vs. exclusive) — test the exact boundary;
  (3) `as_of` after the last version silently returning stale snapshot
  instead of the current body — test it; (4) a diff library added via
  `uv pip` instead of `uv add` (prefer stdlib `difflib`); (5) MCP budget
  overflow.

## 10. Subtickets (ordered steps)

1. `get_note_at(as_of=...)` reconstruction + boundary tests. → verify:
   as-of tests green.
2. `diff_note_versions` (stdlib `difflib`). → verify: diff test green.
3. Mental-model `as_of` reconstruction. → verify: mm as-of test green.
4. Routes + client + DTOs + MCP tools. → verify:
   `uv run pytest packages/mcp/tests` green.
5. Axis-independence test + adversarial review. → verify: clean;
   `just test` + `just prek` green.

## 11. Open questions

Substantive forks settled in the spike (Q5 separate `as_of` path; Q6
mental-model diff deferred). One implementer detail, not an operator
fork: whether `as_of` and `version` share one MCP read tool
(`memex_note_at` with either arg) or two tools — pick the within-budget,
lower-tool-count option and note it.

---

**Eval marker:** `.loop/evals/knowledge-temporal-queries.md`
(`require_eval: true`).
