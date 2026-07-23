# ghost-memory-fact-succession: detect non-contradictory fact succession and make retrieval succession-aware

## 1. Title

Detect when a newly-ingested fact **supersedes** an older one without
directly contradicting it ("lives in London" 2024 → "moved to Paris"
2025; "we use X" → "we switched to Y"), record the succession as an
append-only link + state on the old unit, and make default retrieval
succession-aware so a superseded fact stops surfacing as the *current*
answer while remaining reachable for historical/audit queries.

Source: RFC #247 (Ghost Memory Resolution). This ticket implements the
**triaged scope** the team decided on — succession detection + a
retrieval state, NOT the RFC's full "evidence-packet / answer-time
resolution overlay" (see §5 Non-goals).

## 2. Size / Effort

**L — spans schema, the contradiction engine, and the retrieval
filter chain, plus a decoupled eval.** What drives it: succession is a
*second verdict* that the contradiction subsystem structurally cannot
express today (its relation vocabulary is `Literal['reinforce',
'weaken', 'contradict']`, `signatures.py:33`), so detection needs a new
relation + link type + a persisted state on the superseded unit; and
retrieval has three separate filter seams
(`apply_generic_filters`, the hydration pre-filter, and the exploration
injector) that must all learn the new state or superseded units will
leak back in through whichever seam is missed. Decomposed into two
ordered subtickets (§10) that each fit one loop iteration.

## 3. Triggered by

RFC #247: "ghost memory" — old, current, and transition facts coexist
in the bank, stay mixed at retrieval, and mislead the answer model.
The old fact was never *contradicted*; it was *superseded* by a newer,
non-contradictory fact, so contradiction resolution never fires. This
is the single most common real correctness failure of a personal
memory system: the user moved, changed jobs, switched tools — and the
assistant still answers with the stale fact.

## 4. Context (today's state, cited)

### 4.1 Why succession slips past contradiction detection

The contradiction engine runs at ingest as a deferred post-commit
task: `MemoryEngine.retain` schedules
`self.contradiction.detect_contradictions(...)` at
`packages/core/src/memex_core/memory/engine.py:229`, returned as
`contradiction_task` and awaited post-commit by the ingest service
(`packages/core/src/memex_core/services/ingestion.py:523`, `:899`,
`:1128`).

Inside the engine (`memory/contradiction/engine.py`), the flow is:
one **triage** LLM call over all new units (`_triage`,
`engine.py:235`), then per flagged unit a **classify** LLM call
(`_classify`, `engine.py:394`) over candidate units retrieved by
entity-overlap + semantic similarity (`candidates.py:16`).

The triage signature **already embraces succession**:
`TriageNewUnits` (`memory/contradiction/signatures.py:46-50`) asks the
LLM to flag units that "correct, update, revise, or **supersede** prior
information … including both explicit corrections and **natural state
changes (e.g. someone replaced someone, a value changed)**." So
"moved to Paris" is very likely flagged.

Succession dies at the **classify** step. `ClassifyRelationships`
outputs `relation: Literal['reinforce', 'weaken', 'contradict']`
(`signatures.py:33-35`, `:75-77`) and instructs the LLM to treat
anything else as **neutral** and "Skip neutral pairs entirely"
(`signatures.py:66-68`). "Lives in London (2024)" and "moved to Paris
(2025)" are *both true at their own times* — not a sentence-level
inversion — so the LLM either drops the pair as neutral, or is forced
into `weaken`/`contradict`, which only nudges `confidence` and never
records that the old fact is superseded. The relation→effect mapping
is `memory/contradiction/engine.py:317-351`: `weaken` → −alpha
confidence + a `weakens` link; `contradict` → −2·alpha + a
`contradicts` link. **No branch marks the old unit superseded; it stays
`status=active` and keeps surfacing.**

The downstream lint path *can* mark succession (`supersede_loser_note`
/ `mark_loser_stale` in
`packages/core/src/memex_core/services/contradiction_resolution.py:261-325`)
but only fires on a `propose_contradiction_winner` proposal, which only
exists when `CheckSemanticContradiction` returns
`has_contradiction=True` — and its STRICT RULE
(`memory/lint_llm/signatures.py:53-56`) treats compatible facts like
London-vs-Paris as *not* a contradiction. So that path never triggers
for succession either.

### 4.2 The data model has no first-class succession primitive

- `MemoryUnit` (`memory/sql_models.py:625`) has `status: ContentStatus`
  with exactly two values, `active`/`stale` (`ContentStatus`,
  `sql_models.py:38-42`; CHECK `status IN ('active','stale')` at
  `sql_models.py:856`). **STALE today means** "content the extraction /
  reflection pipeline superseded during re-ingestion, kept append-only,
  downranked but never deleted" — it is set by `extraction/storage.py`
  (Chunk `:595`, Node `:819`) and reflection paths, **never by the
  contradiction engine**, and the `prune_stale_evidence` job operates
  on `status=STALE` units (`sql_models.py:2760-2763`).
- Non-destructive downweight is a separate boolean `is_deprioritized`
  (`sql_models.py:723-727`), the FSFM mechanism.
- `MemoryLink` (`sql_models.py:1495`) is the only unit-to-unit link
  table. `link_type` is a **CHECK-constraint string** (not a Python
  enum) with 11 values —
  `temporal, semantic, entity, causes, caused_by, enables, prevents,
  reinforces, weakens, contradicts, refines` (`sql_models.py:1562`).
  **There is no `supersedes` value.** `link_metadata` JSONB is already
  documented "supersession provenance" (`sql_models.py:1533-1537`).
- Note-level succession exists (`Note.superseded_by: UUID | None`,
  `sql_models.py:334-337`) but there is **no unit-level equivalent**.

### 4.3 Retrieval filtering — three seams, and an already-present flag

- The exclude-by-default / keep-for-audit pattern to mirror is
  `apply_generic_filters` (`memory/retrieval/strategies.py:113-132`):
  `if not include_stale: WHERE status == ACTIVE` (`:124-125`) and
  `if not include_deprioritized: WHERE is_deprioritized == False`
  (`:128-130`). Called from every strategy.
- `apply_pre_filter` (default `True`, `memory/retrieval/models.py:98`)
  gates a *different* set — the memory-worth, FSFM-decay, and
  confidence<0.2 branches in `_build_pre_filter_clause`
  (`memory/retrieval/engine.py:254-341`), bypassed wholesale for audit
  when `False` (`engine.py:290-291`). It does **not** gate the
  status/`is_deprioritized` WHERE filters.
- An `include_superseded` request flag **already exists** but today
  only drives a fuzzy Python confidence cut at stage 6b
  (`memory/retrieval/engine.py:804-809`: drop units whose confidence <
  `superseded_threshold`, default 0.3). It does **not** yet gate any
  status/link/column filter.
- The exploration injector re-applies exclusions in Python
  (`memory/retrieval/exploration.py:154-162`:
  `not u.is_deprioritized and u.status == ContentStatus.ACTIVE`), so a
  new succession state must be honored **here too** or exploration will
  re-surface superseded units.
- Retrieval already reads `contradicts`/`weakens` links back into a
  synthetic `unit_metadata['superseded_by']` at hydration
  (`memory/retrieval/engine.py:1693-1735`).

### 4.4 `get_unit_history` already walks the succession graph

`UnitsService.get_unit_history`
(`packages/core/src/memex_core/services/units.py:613`) is a graph walk
over `MemoryLink`, defaulting to
`DEFAULT_HISTORY_LINK_TYPES = ('contradicts', 'weakens')`
(`units.py:31`), following `from=authoritative → to=superseded` edges
to build a predecessor tree (`UnitHistoryNodeDTO`,
`packages/common/src/memex_common/schemas.py:1214`). **Adding
`supersedes` to that tuple surfaces succession chains with no
structural change to the walk** (endpoint at
`packages/core/src/memex_core/server/memories.py:562`).

### 4.5 Cost context

LLM calls go through `run_dspy_operation` (`memex_core/llm.py:56`) with
a process-wide circuit breaker and Prometheus metrics; **no batching**,
each call independent. Extraction already costs ~1–3 LLM calls/note;
contradiction adds 1 triage + 1 classify per flagged unit. **Riding the
existing `ClassifyRelationships` call (extend its relation vocabulary)
adds ZERO marginal LLM calls** — the succession verdict comes back in a
classification the engine is already making. A standalone succession
detector would cost +1 triage + N candidate judgments per note; §10
prefers the zero-marginal-cost path.

## 5. Non-goals / out of scope

- **No RFC-style "evidence packet" / "answer-time resolution" overlay.**
  RFC #247 sketches three new modules
  (`memory/state.py`, `state_retrieval.py`, `state_resolution.py`) that
  assemble state-labelled packets and push temporal reasoning into the
  answer model. This ticket deliberately does NOT build them — it uses
  the existing filter/link/state machinery per CLAUDE.md "Simplicity
  First" and "Surgical Changes". Deferred to a follow-up RFC if the
  filter approach proves insufficient.
- **No `current`/`historical`/`transition` three-state tag.** Scope is
  binary: a unit is either superseded or not. First-class "transition
  record" units are out of scope.
- **No content mutation and no deletion of superseded units.**
  Append-only is invariant (see §6). The superseded unit's text is
  never edited; the unit is never deleted.
- **No change to the confidence-decrement mechanics** of the existing
  `weaken`/`contradict` relations, nor to the lint contradiction path
  (`contradiction_resolution.py`).
- **No retroactive back-fill** of succession state onto the existing
  historical bank in this ticket (the RFC's "retroactive tagging via
  lint"). New succession detection applies to newly-ingested facts;
  back-fill is a separate follow-up.
- **No MCP/agent-surface prose changes** unless a tool starts returning
  a new field; if subticket 2 adds a `superseded` marker to a search
  DTO, the per-tool description budget rules in CLAUDE.md apply and that
  is called out inline, but no universal `agent_surface` edits.

## 6. Requirements & restrictions

- **Append-only is invariant.** Superseded units are never edited or
  deleted; succession is a *link + state*, never a content mutation.
  This is the storage doctrine in CLAUDE.md
  (`.claude/rules/memex-agent-surface.md`: "Memory units — append-only
  facts … NEVER edit/replace/delete") and the existing engine already
  respects it (writes links + confidence deltas, never mutates text,
  `engine.py:368-378`). Any deletion or text edit of a superseded unit
  is a violation.
- **Historical facts stay reachable.** A superseded unit must remain
  retrievable by audit/historical queries via the existing bypass seams
  (`apply_pre_filter=False` and/or `include_superseded=True`) and must
  appear in `get_unit_history`. RFC #247 explicitly rejects deleting
  superseded facts.
- **Default retrieval must not surface a superseded fact as the current
  answer.** This is the correctness goal; verified by the eval (§8).
- **Every changed line traces to this ticket.** Do not "improve"
  adjacent contradiction/retrieval code (CLAUDE.md "Surgical Changes").
- **All new code ships with tests** (`.claude/rules/python-testing.md`):
  a reproducing test first, unit tests for detection, integration tests
  (testcontainer Postgres) for the schema + retrieval filter, since the
  filter is a SQL WHERE clause and schema drift must surface in CI.
- **Migrations via alembic** (`just db-revision "..."`,
  CLAUDE.md Commands) — the new column + link_type CHECK change need a
  migration; hand-editing the schema without one is a violation.
- **Use `uv`, single quotes, line length 100, strict mypy, all I/O
  async** (CLAUDE.md Code style). Add deps (if any) with `uv add`
  (`.claude/rules/uv-installer.md`).
- **Adversarial review before done** (`.claude/rules/adversarial-reviews.md`)
  — the loop's `loop-reviewer` pass satisfies this.

## 7. Code surface

**Subticket 1 — detection + schema:**

- `packages/core/src/memex_core/memory/sql_models.py:1562` — add
  `'supersedes'` to the `link_type` CHECK constraint.
- `packages/core/src/memex_core/memory/sql_models.py:625-856` — add a
  nullable `superseded_by_unit_id: UUID | None` column to `MemoryUnit`
  (mirroring `Note.superseded_by`, `sql_models.py:334-337`) + a partial
  index `WHERE superseded_by_unit_id IS NOT NULL` (mirror the
  `is_deprioritized` partial index at `sql_models.py:881-885`). *(Fork
  Q1: column-vs-status-reuse-vs-link-only — see §11.)*
- `packages/core/src/memex_core/memory/contradiction/signatures.py:33-35,66-77`
  — add `'supersede'` to the `ContradictionRelationship.relation`
  Literal and teach `ClassifyRelationships` to emit it for
  non-contradictory temporal succession (a value/state change where
  both facts held at their own times). Zero marginal LLM cost.
- `packages/core/src/memex_core/memory/contradiction/engine.py:317-378`
  — add a `supersede` branch to the relation→effect mapping: write a
  `MemoryLink(from=authoritative, to=superseded, link_type='supersedes')`
  with provenance in `link_metadata`, and set
  `superseded.superseded_by_unit_id = authoritative.id` in the same
  transaction. No confidence decrement (succession is not a confidence
  signal). Reuse `_resolve_authority`/`_temporal_default`
  (`engine.py:422-449`) for "later event_date wins".
- `packages/core/src/memex_core/services/units.py:31` — add
  `'supersedes'` to `DEFAULT_HISTORY_LINK_TYPES` so history surfaces
  succession chains.
- Alembic migration under
  `packages/core/src/memex_core/alembic/versions/` (via
  `just db-revision`) — the new column, its index, and the CHECK
  constraint change.

**Subticket 2 — succession-aware retrieval:**

- `packages/core/src/memex_core/memory/retrieval/strategies.py:113-132`
  — in `apply_generic_filters`, add
  `if not include_superseded: WHERE superseded_by_unit_id IS NULL`,
  mirroring the `is_deprioritized` predicate at `:128-130`.
- `packages/core/src/memex_core/memory/retrieval/engine.py:662-671` —
  thread `include_superseded` into the `filters` dict passed to
  strategies (as `include_stale`/`include_deprioritized` are threaded).
- `packages/core/src/memex_core/memory/retrieval/engine.py:804-809` —
  keep the existing confidence-based `include_superseded` cut; the new
  column filter is additive (belt-and-suspenders). Confirm no
  double-filtering regression.
- `packages/core/src/memex_core/memory/retrieval/exploration.py:154-162`
  — add `superseded_by_unit_id is None` to the Python exclusion so
  exploration cannot re-surface superseded units.
- `packages/core/src/memex_core/services/search.py:65-67,105-107` —
  confirm `include_superseded` is already plumbed here (it is); no
  change unless a new default flag is needed.
- *(If a search DTO gains a `superseded: bool` marker — optional —
  the MCP tool-description budget rules in CLAUDE.md apply; call out
  the exact DTO + description file in the subticket.)*

**Tests (files):**

- `packages/core/tests/unit/` — new
  `test_contradiction_succession.py` (detection: succession → link +
  `superseded_by_unit_id`, with a mocked/`MockDspyLM` classify verdict).
- `packages/core/tests/integration/` — new
  `test_succession_retrieval.py` (real Postgres: superseded unit absent
  from default retrieval, present with `include_superseded=True` and
  with `apply_pre_filter=False`, and surfaced by `get_unit_history`).
- `packages/eval/src/memex_eval/suites/ghost_memory_succession/` — a
  new eval suite (§8, §10; per `.claude/rules/eval-suites.md` this is a
  suite package, NOT a bespoke pytest e2e harness).

## 8. Tests & validation gates

**Gates (from `.loop/config.json` + repo):**

- `just test` (pytest; default run is offline — integration/llm markers
  excluded via `addopts`). Integration tests here carry
  `@pytest.mark.integration` (real Postgres via testcontainers) and
  `@pytest.mark.llm_mock` for the detection test (deterministic
  `MockDspyLM` golden verdict — do NOT gate CI on a real LLM call).
- `just prek` (ruff + mypy, strict).
- `just db-upgrade` must apply the new migration cleanly on a fresh DB.
- Adversarial review pass (`loop-reviewer`).

**Reproducing test first (bug-style):** before any fix, add an
integration test that ingests "user lives in London" then "user moved
to Paris", runs a default `memory_search("where does the user live")`,
and asserts the London unit is returned today (red baseline / documents
the ghost), then flips to asserting it is NOT in the default result set
after detection lands.

**Required eval (`require_eval: true` in `.loop/config.json` — MANDATORY,
the loop will not pick the ticket up without the marker
`.loop/evals/ghost-memory-fact-succession.md`).** Decoupled across the
three A-TMA levels per the RFC, five-column Behavior/Input/Expected/
Scorer/Threshold:

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL — bank]** Succession recorded, old fact retained | Ingest "lives in London" (2024), then "moved to Paris" (2025) | A `supersedes` link authoritative(Paris)→superseded(London); London unit still present with unedited text; `superseded_by_unit_id` = Paris unit | Deterministic DB assert: link row exists, London text unchanged, column set | 100% |
| **[GUARDRAIL — append-only]** Superseded unit never deleted/edited | Same ingest | London unit row exists, `status` unchanged by content mutation, text byte-identical | Deterministic DB assert | 100% |
| **[retrieval]** Default retrieval returns current, not superseded | `memory_search("where does the user live?")`, default flags | Paris unit in results; London unit absent | Deterministic: London unit_id not in default result IDs; Paris present | 100% |
| **[GUARDRAIL — audit reachability]** Historical query still reaches superseded | Same query with `include_superseded=True` (and separately `apply_pre_filter=False`) | London unit present in results | Deterministic: London unit_id in results under each bypass flag | 100% |
| **[retrieval]** `get_unit_history` surfaces the chain | `get_unit_history(paris_unit_id)` | Predecessor tree includes London via a `supersedes` edge | Deterministic: London in predecessors, `link_type=='supersedes'` | 100% |
| **[answer]** Compatible non-succession facts are NOT superseded | Ingest "likes coffee" then "likes tea" (additive, both hold) | No `supersedes` link created; both remain in default retrieval | LLM-judge or golden `MockDspyLM`: relation != 'supersede' | ≥90% |
| **[answer — precision guard]** Direct contradiction still routes to contradict, not supersede | Ingest "the meeting is at 3pm" then "the meeting is at 4pm" | Relation is `contradict`/`weaken` (confidence signal), not silently converted to succession | Golden verdict assert | ≥90% |

Rows 1–5 are deterministic guardrails at 100%. Rows 6–7 guard against
over-firing (succession must not swallow additive facts or direct
contradictions) and use `MockDspyLM` golden verdicts or a scoped
LLM-judge at ≥90%. Author with the `create-eval` skill as a suite
package under `packages/eval/.../suites/ghost_memory_succession/`.

**Eval marker (authored, `loopctl eval` → valid):**
`.loop/evals/ghost-memory-fact-succession.md` — the 7-row spec above,
with fork defaults encoded (Q1 dedicated column, Q2 fold-into-classify,
Q3 no confidence decrement). Rows 1/3/4 are pinned to the
`superseded_by_unit_id` column; re-pin them if the operator resolves Q1
differently.

## 9. Risk assessment

- **Blast radius: retrieval correctness (high-value, high-sensitivity).**
  A too-aggressive succession filter *hides* facts that are still true
  (false-supersede), which is worse than the ghost it fixes. Rows 6–7
  of the eval are the guard; the LLM classify prompt must be
  conservative (only same-claim-slot value/state changes, not additive
  facts). Recommend erring toward under-detection.
- **Three filter seams.** If subticket 2 updates
  `apply_generic_filters` but misses `exploration.py:154-162` or the
  hydration path, superseded units leak back through the missed seam.
  The integration test must exercise a query that triggers exploration
  injection.
- **Reversibility.** High. Succession is a link + a nullable column; a
  bad verdict is undone by clearing `superseded_by_unit_id` and the
  link. No content is lost (append-only). The migration is additive
  (nullable column, widened CHECK) and safe to roll forward; document
  the down-migration.
- **`prune_stale_evidence` interaction (fork-dependent).** If Q1
  resolves to *reuse `status='stale'`* rather than a dedicated column,
  succession units become eligible for the stale-pruning job
  (`sql_models.py:2760-2763`), risking deletion of historical facts —
  a direct violation of §6. The recommended dedicated-column approach
  (Q1) avoids this entirely.
- **LLM over-trigger cost.** Riding `ClassifyRelationships` adds no
  calls; the risk is prompt-quality, not throughput.

## 10. Subtickets

Ordered, dependency-aware; subticket 2 depends on 1's column + link.

1. **`ghost-memory-succession-detect` — detection + schema.**
   Add the `supersedes` link_type + `MemoryUnit.superseded_by_unit_id`
   column (+ migration), extend `ClassifyRelationships` to emit
   `supersede` (zero marginal LLM cost), add the engine branch that
   writes the link + sets the column append-only, add `supersedes` to
   `DEFAULT_HISTORY_LINK_TYPES`. Verify: unit test (detection →
   link+column) + `get_unit_history` surfaces the chain; eval rows 1–2,
   5–7.
2. **`ghost-memory-succession-retrieval` — succession-aware retrieval.**
   Add the default `superseded_by_unit_id IS NULL` filter in
   `apply_generic_filters`, thread `include_superseded` through the
   strategy filters, honor the state in the exploration injector,
   confirm the audit bypasses (`include_superseded=True`,
   `apply_pre_filter=False`) still reach superseded units. Verify:
   integration test (default excludes, bypass includes); eval rows 3–4.

## 11. Open questions

**Q1 (design fork — how is the superseded state persisted?).** Three
options: **(A)** reuse `status='stale'` — cheapest (the retrieval
filter already exists via `include_stale`), but conflates
succession with reflection-staleness and makes superseded facts
eligible for `prune_stale_evidence` deletion (§9), violating retention;
**(B, recommended)** a dedicated nullable `superseded_by_unit_id: UUID`
column on `MemoryUnit` mirroring `Note.superseded_by`
(`sql_models.py:334`) — crisp, indexable, records *which* unit
superseded it, keeps succession distinct from staleness, and the
retrieval filter clones the `is_deprioritized` pattern exactly
(`strategies.py:128-130`); **(C)** link-only, filter via
`NOT EXISTS (supersedes link)` — purest append-only but a correlated
subquery on every retrieval query, added at three seams.
**Recommendation: B** (column as the retrieval-filter index +
`supersedes` link as the graph/provenance source of truth, both written
atomically in the engine's existing single-pass update).

**Q2 (detection — extend contradiction classify vs. standalone pass?).**
Recommendation: **extend `ClassifyRelationships`** (add a `supersede`
relation) — zero marginal LLM cost, reuses candidate retrieval and the
temporal-authority resolver. A standalone detector is the fallback only
if the combined prompt degrades contradiction precision (watch eval
row 7). Confirm the operator accepts folding succession into the
contradiction call.

**Q3 (does succession touch confidence at all?).** Recommendation:
**no confidence change** on the superseded unit — succession is a
temporal state, not evidence-of-wrongness. Contrast with `weaken`
(−alpha) / `contradict` (−2·alpha) at `engine.py:338-349`. If the
operator wants superseded facts to also decay in confidence, that is a
one-line addition but changes the audit-query ranking.

**Q4 (retroactive back-fill of the existing bank).** Out of scope here
(§5). Confirm this is acceptable — the existing London/Paris ghosts
already in a live vault stay ghosts until re-ingested or a follow-up
back-fill ticket lands.

**Q5 (surface a `superseded` marker in search DTOs?).** Optional. If
agents should *see* that a returned historical unit is superseded (for
audit queries), a `superseded: bool` field on the search DTO helps, but
triggers the MCP per-tool description budget rules (CLAUDE.md).
Recommendation: defer to a follow-up unless an eval row needs it.
