# retrieval-staleness-score-spike: root-cause uniform `staleness: "stale"` and null `score` on `memory_search`

## 1. Title

Investigate two anomalies observed on a live `memex_memory_search`
response — every unit tagged `staleness: "stale"`, and every unit's
`score` returned `null` — and produce a written finding plus a
go/no-go recommendation per anomaly. **This is a spike. The
deliverable is knowledge and a recommendation, not a behavior
change.** No fix to the staleness formula, the reranker, or
serialization is in scope here.

## 2. Size / Effort

**S–M — investigation spike, read-only.** Effort is dominated by
reading the retrieval and MCP-serialization paths and reasoning about
the staleness thresholds against realistic vault dates, not by writing
code. The only code this ticket may produce is at most one
*characterization test* that pins the current (arguably wrong)
behavior so a later fix ticket has a red baseline — and even that is
optional (see §8). Definition of Done is the findings section (§11 /
written artifact) plus a go/no-go per observation. If the answer to
either observation is "working as designed," the correct output is a
one-line "close, no fix" with cited evidence — that still completes
the ticket.

## 3. Triggered by

A real `memex_memory_search` call this session returned ~13 units
spanning different notes and dates, and:

1. **Every unit carried `staleness: "stale"`** — uniform across the
   whole result set. Question: correct decay, or a *saturated* signal
   that reads stale for essentially any real vault, making the field
   non-discriminative at the point of use?
2. **Every unit carried `score: null`**, while `memex_note_search`
   returns real numeric scores. Question: intentional suppression,
   serialization drop, or a bug where the rerank score is never
   propagated to the response?

## 4. Context (today's state, cited)

### Observation 1 — staleness computation

Staleness is computed at the **MCP boundary**, not in core retrieval,
by a pure function:

- `packages/mcp/src/memex_mcp/server.py:1355` — `compute_staleness(*,
  event_date, confidence, superseded_by, links, now=None) ->
  Staleness`.
- Priority order (docstring at `server.py:1365`): CONTESTED >
  confidence-based STALE > time-based FRESH/AGING/STALE.
- `server.py:1409` — `confidence < 0.5` → `STALE`.
- `server.py:1412-1417` — when a usable date exists, **`age_days > 30`
  → `STALE`** (this 30-day cliff is the prime saturation suspect).
- `server.py:1419-1420` — `7 <= age_days <= 30` → `AGING`;
  `server.py:1422-1425` — `< 7` days → `FRESH` (conf ≥ 0.7) else
  `AGING`.
- `server.py:1521-1527` — the date fed in is
  `staleness_anchor = res.mentioned_at or res.occurred_start`, NOT the
  raw `event_date` (see docstring rationale at `server.py:1367-1381`).
  For world facts `mentioned_at` is backfilled from `event_date` by
  `build_memory_unit_dto`, so it is normally populated.
- `packages/mcp/src/memex_mcp/models.py:20-26` — `Staleness` enum
  (`fresh`/`aging`/`stale`/`contested`); `models.py:126` — the
  response field `staleness: Staleness | None`.

**Preliminary read:** the 30-day cliff means any unit whose anchor
date is more than a month old classifies as `STALE` regardless of
confidence or relevance. A vault whose content is mostly older than 30
days will therefore read *uniformly* stale — the field is technically
correct per its own thresholds yet carries near-zero discriminative
information at the point of use. This looks like a **calibration**
question (thresholds are absolute and short), not a code bug. The
spike must confirm this against realistic `mentioned_at`/`event_date`
values and decide calibration-defect vs. working-as-designed.

### Observation 2 — null score on the memory-search path

The MCP model defaults score to null and reads it straight off the
DTO:

- `packages/mcp/src/memex_mcp/models.py:117` — `score: float | None =
  None` (default null on `McpMemoryUnitBase`).
- `packages/mcp/src/memex_mcp/server.py:1482` — `_build_memory_unit_model`
  sets `'score': res.score` (reads the DTO's score verbatim).
- `packages/core/src/memex_core/server/common.py:363` —
  `build_memory_unit_dto(...)` sets `score=getattr(unit, 'score',
  None)`. `MemoryUnit` (the SQLModel) has **no `score` column** — the
  attribute exists only if retrieval assigns it dynamically.

The two search paths diverge on whether they assign that attribute:

- **`note_search` / document path DOES set it** —
  `packages/core/src/memex_core/memory/retrieval/document_search.py:296-297`
  `result.score = score` after reranking (and `:309` for overflow,
  `:961` for normalization).
- **`memory_search` path does NOT** —
  `packages/core/src/memex_core/memory/retrieval/engine.py:_rerank_results`
  computes per-unit `boosted` scores (`engine.py:1982-1992`), sorts by
  them (`engine.py:2003`), then returns **`[item[0] for item in
  scored_results]`** (`engine.py:2004`) — the score is used only for
  ordering and discarded. The main search flow calls this at
  `engine.py:831-836` and never re-attaches a score afterward
  (`engine.py:838-859` and onward operate on unscored units).

**Preliminary read:** the rerank score exists internally
(`boosted`/`scores`) but is never written back onto the unit, so
`getattr(unit, 'score', None)` is `None` all the way to the response.
The `note_search` asymmetry (`result.score = score`) strongly suggests
the `memory_search` drop is **unintentional** — a real defect — rather
than deliberate suppression. The spike must confirm there is no
intervening assignment and decide bug vs. by-design.

### Gates (verified this session)

- `just test` → `uv run pytest tests` — the **root `./tests/`** suite
  only (~56 tests). `packages/core/tests` and `packages/mcp/tests` are
  **not** collected by this gate. An existing unit test for the pure
  function lives at `packages/mcp/tests/test_staleness.py` but does NOT
  run under the loop gate.
- `just prek` → `uv run prek run -a`.

## 5. Non-goals / out of scope

- Do **not** implement a fix for either observation. Do not change the
  staleness thresholds/formula (`compute_staleness`), the reranker or
  its boost composition, or the DTO/serialization.
- Do not touch ranking or ordering behavior — memory-search ordering
  is driven by `boosted` regardless of whether `score` is surfaced;
  leave it alone.
- Do not add score propagation to `_rerank_results` (that is the
  candidate follow-up fix, not this ticket).
- Do not recalibrate or parameterize the 30-day / 7-day thresholds.
- Do not add a new HTTP field, MCP field, or config knob.

## 6. Requirements & restrictions

**Must achieve:**

- R1. A written finding for **Observation 1** that states the actual
  staleness formula and its inputs (cite `server.py:1355` and the
  branch lines), and answers empirically whether, for typical
  `mentioned_at`/`event_date`/`occurred_start` values in a real vault,
  the classification collapses to all-`stale`. Name the specific
  driver (the absolute 30-day cliff and/or the confidence floor).
- R2. A written finding for **Observation 2** that traces `score` from
  the reranker (`engine.py:_rerank_results`) through
  `build_memory_unit_dto` (`common.py:363`) to the MCP model
  (`server.py:1482`, `models.py:117`), explains the asymmetry against
  `document_search.py:296-297`, and states whether the rerank score
  exists internally but is dropped.
- R3. A **go/no-go per observation**: for each, either
  "working-as-designed → close, no fix" or "defect → recommended
  follow-up fix ticket" with a one-paragraph scope for that follow-up
  (files to touch, the shape of the fix). Do not implement the
  follow-up.
- R4. Every claim in the finding cites a `path:line` the investigator
  actually opened. No guessed anchors.

**Restrictions (repo principles, cited):**

- Read-only toward product code — this is a spike. The only permitted
  write besides the finding is an optional characterization test (§8).
- `.claude/rules/python-testing.md` (`all-code-needs-tests`,
  `tests-are-real-code`): if any code is written it is a test, it must
  live where the gate runs it, and it must pass lint + type-check. A
  characterization test asserts the *current* (arguably wrong)
  behavior; it must NOT be a `skip`/`xfail`.
- `.claude/rules/pre-existing-issues.md`: if the investigation surfaces
  an adjacent defect, record it in the finding — do not silently work
  around it, and do not scope-creep a fix into this spike.
- `.claude/rules/adversarial-reviews.md`: run an adversarial review of
  the finding (is the root-cause causal, are the anchors real, is the
  go/no-go defensible) before declaring done.
- `.claude/rules/slop-scan-for-docs.md`: the written finding is a
  markdown doc — every backticked identifier and cited path must
  resolve; run the P0 hallucination check on it.
- `docs`-vs-`.temp` placement of the artifact is an operator fork — see
  §11 Q4.

## 7. Code surface (files to READ; anchors to re-open and cite)

Read-only. No product-code edits. The only file this ticket may
*write* (besides the ticket's own finding section) is the optional
test in §8.

- `packages/mcp/src/memex_mcp/server.py:1355-1433` — `compute_staleness`;
  the branch cliffs at `:1409`, `:1412-1425`. **Read.**
- `packages/mcp/src/memex_mcp/server.py:1436-1548` —
  `_build_memory_unit_model`; `score` read at `:1482`, staleness anchor
  + call at `:1521-1527`. **Read.**
- `packages/mcp/src/memex_mcp/models.py:20-26,117,126` — `Staleness`
  enum, `score` default null, `staleness` field. **Read.**
- `packages/core/src/memex_core/server/common.py:310-365` —
  `build_memory_unit_dto`; `score=getattr(unit,'score',None)` at `:363`,
  `mentioned_at`/`event_date` backfill logic. **Read.**
- `packages/core/src/memex_core/memory/retrieval/engine.py:823-844` —
  search flow rerank call + no post-rerank score attach. **Read.**
- `packages/core/src/memex_core/memory/retrieval/engine.py:1808-2012` —
  `_rerank_results`; `boosted` composed at `:1982-1992`, sorted `:2003`,
  score-less return at `:2004`. **Read.**
- `packages/core/src/memex_core/memory/retrieval/document_search.py:274-311`
  — the contrast: `result.score = score` at `:296-297`. **Read.**
- `packages/common/src/memex_common/schemas.py:583-587` — `MemoryUnitDTO.score`
  field (default None). **Read.**
- `packages/mcp/tests/test_staleness.py:1-55` — existing pure-function
  unit test (NOT run by the loop gate); reference for the
  characterization test's construction of `compute_staleness` inputs.
  **Read.**
- `tests/` — **the only place a new test may be written** so `just
  test` collects it (see §8).

## 8. Tests & validation gates

**Gates:** `just test` (`uv run pytest tests`, root suite only) and
`just prek`. Both must pass at close, whether or not a test is added
(a spike that writes only a finding still must leave the gates green).

**Optional characterization test (only if the finding concludes
"defect" for either observation):**

- File: **`tests/test_retrieval_staleness_score_spike.py`** (root
  `./tests/` so `just test` collects it — the loop gate does NOT run
  `packages/mcp/tests`).
- It CHARACTERIZES current behavior; it does not assert the desired
  behavior. Two independent, pure, offline cases:
  - **Staleness saturation:** import `compute_staleness` from
    `memex_mcp.server` (as `packages/mcp/tests/test_staleness.py:6`
    does). Assert that for a spread of anchor dates all older than 30
    days (e.g. 31, 90, 365 days) with high confidence (≥0.7), the
    result is uniformly `Staleness.STALE` — pinning that the absolute
    cliff makes the field non-discriminative for an aged corpus. Use
    an injected `now` for determinism (`.claude/rules/python-testing.md`
    → no wall-clock).
  - **Score drop:** import `_build_memory_unit_model` from
    `memex_mcp.server`; feed it a minimal DTO-like object whose
    `.score is None` (mirroring what the memory rerank path leaves) and
    assert the produced model's `.score is None` while its `.staleness`
    is populated — pinning that the memory path surfaces a null score.
    (Reference the `note_search` contrast in a comment, do not import
    the DB path.)
- Both cases must pass `just prek` (ruff + mypy): fully typed, no
  `# type: ignore`, no `skip`/`xfail`.
- If the finding concludes "working-as-designed" for both, **write no
  test** — a green characterization test of intended behavior adds
  noise. State that decision in the finding.

**No reproducing test is required to *open* this spike** (it is an
investigation, not a bug-fix ticket). The characterization test, if
written, is the reproduction that the recommended follow-up fix ticket
would later turn red→green.

## 9. Risk assessment

- **Blast radius: minimal.** Read-only investigation. The only write is
  a finding doc and, optionally, one additive test file — neither
  changes runtime behavior. Nothing here alters ranking, staleness, or
  serialization.
- **Reversibility: trivial.** Delete the test / revert the finding.
- **Likeliest failure modes:**
  1. *Scope creep into a fix* — the strong pull is to "just add
     `unit.score = boosted`" in `_rerank_results`. That is explicitly
     out of scope (§5); it is the follow-up ticket. Guard against it.
  2. *Mis-attributing Observation 1 as a code bug* — the thresholds may
     be a deliberate product choice. The finding must distinguish
     "non-discriminative in practice" (calibration) from "computed
     incorrectly" (bug) and not assert a fix the operator hasn't
     approved.
  3. *Anchoring the characterization test on the wrong date field* —
     staleness keys on `mentioned_at`/`occurred_start`
     (`server.py:1521`), not raw `event_date`. A test that feeds
     `event_date` would mischaracterize the path.
  4. *Writing the test where the gate can't see it* — placing it under
     `packages/mcp/tests/` means `just test` never runs it. Root
     `./tests/` only.

## 10. Subtickets (ordered investigation steps)

1. **Confirm the staleness formula and its saturation.** Re-open
   `server.py:1355-1433` and `:1521-1527`; enumerate the branch cliffs.
   Reason about realistic `mentioned_at`/`occurred_start` distributions
   for a month-plus-old vault and determine whether the result
   collapses to all-`stale`. Write the Observation-1 finding with
   cited anchors. → verify: finding states the driver (30-day cliff
   and/or confidence floor) with evidence.
2. **Trace the score path end to end.** Re-open `engine.py:_rerank_results`
   (`:1982-2004`), the search flow (`:823-844`),
   `common.py:363`, `server.py:1482`, `models.py:117`, and the
   `document_search.py:296-297` contrast. Confirm no assignment sets
   `unit.score` on the memory path. Write the Observation-2 finding. →
   verify: finding names the exact line where the score is discarded
   and the exact line where the sibling path keeps it.
3. **Decide go/no-go per observation.** For each: working-as-designed →
   "close, no fix"; defect → one-paragraph follow-up fix scope (files,
   shape). → verify: §11 answers Q1 and Q2 with a recommendation.
4. **(Conditional) write the characterization test** per §8 only if
   step 3 concludes "defect" for an observation. → verify: `just test`
   and `just prek` green; the test pins *current* behavior.
5. **Adversarial review + slop scan of the finding**, then hand back. →
   verify: reviewer confirms causal root-cause, real anchors,
   defensible go/no-go; P0 hallucination check clean.

## 11. Open questions (forks for the operator)

- **Q1 — RESOLVED (operator, 2026-07-23): WORKING AS DESIGNED.** The
  30-day absolute staleness cliff (`server.py:1416`) is intentional — a
  feature, not a bug. Uniform `stale` on a vault whose content is mostly
  older than a month is the intended behavior: staleness is a binary
  recency flag, not an intra-corpus ranking signal (post-reranker boosts
  handle ordering). **No follow-up fix ticket for staleness.** The
  finding records Observation 1 as WAI; no staleness characterization
  test is written (see Q3).
- **Q2 — Observation 2 verdict: bug or deliberate suppression?**
  Evidence points to an unintentional drop (`engine.py:2004` discards
  `boosted`; `document_search.py:297` keeps its score).
  *Recommendation:* **defect → follow-up fix ticket** scoped to attach
  the composed rerank score onto the unit in `_rerank_results` (mirror
  `document_search.py:296-297`) so `build_memory_unit_dto` surfaces it.
  Fork the operator must settle: is a memory-path composite `boosted`
  score *meaningfully comparable* to the sigmoid-normalized
  `document_search` score, or would surfacing it mislead callers into
  cross-path comparison? If the latter, the fix may be to surface the
  normalized `ce_score` rather than `boosted`. Flag, do not decide.
- **Q3 — Does the characterization test get written at all?** Governed
  by Q1/Q2 verdicts (§8). If both are WAI, no test. *Recommendation:*
  write the score-drop case (Q2 leans defect); gate the staleness case
  on the Q1 verdict.
- **Q4 — Where does the written finding live?** Inline in this ticket's
  delivered finding, under `.temp/`, or under `docs/`.
  *Recommendation:* deliver the finding as the loop's completion
  artifact and, if a durable record is wanted, a short note under
  `.temp/` (transient) rather than `docs/` (which would invite the slop
  gate and imply user-facing intent for an internal spike). Operator's
  call.
- **Q5 — Is the null score already breaking a downstream consumer**
  (e.g. an agent that filters/sorts memory-search results by `score`,
  or `memex_get_notes_metadata` post-processing)? Not investigated in
  the primary trace. *Recommendation:* a quick grep for consumers of
  `McpFact.score` / DTO `.score` on the memory path; if a consumer
  silently treats `None` as 0, that raises Q2's priority. Note findings;
  do not fix.

---

**Eval marker:** `.loop/config.json` sets `require_eval: true` — the
loop will refuse pickup until this ticket's eval marker exists.
Co-author the eval with the `create-eval` skill before implementation
(the "eval is the spec" step). For a spike, the eval should assert the
*finding's* completeness and the go/no-go decisions, not a behavior
change: e.g. "the finding names the score-discard line and the
staleness cliff, and records a go/no-go per observation," plus "no
product code under `packages/` was modified" and "any added test lives
under root `./tests/` and passes `just test`."
