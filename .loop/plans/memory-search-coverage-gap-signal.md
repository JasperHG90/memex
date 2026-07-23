# memory-search-coverage-gap-signal: opt-in `detect_gaps` coverage scoring on memory search (RFC #204 G1)

## 1. Title

Add an opt-in `detect_gaps: bool = False` parameter to memory search
that, after retrieval, runs ONE LLM call to assess how well the returned
results cover the query intent, produces a float `coverage_score`, and —
when the score falls below a threshold (default `0.3`) — emits an
auditable `knowledge_gap` event and surfaces the signal to the calling
agent. This is RFC #204's **G1 slice only**: a diagnostic signal ("memory
does not answer this — escalate or say so"), NOT a trigger for any
automated action.

## 2. Size / Effort

**M.** The LLM-scoring core is genuinely small — one DSPy signature plus
one `run_dspy_operation` call, mirroring the existing
`SearchService.summarize_search_results`
(`packages/core/src/memex_core/services/search.py:123-136`). The size
driver is NOT the scoring; it is **surfacing the score to the agent
across a search path that has no response envelope anywhere** (bare tuple
in-process, bare NDJSON stream over HTTP, bare `list[...]` at the MCP
tool). Delivering a search-level value to the MCP caller therefore
touches the HTTP endpoint, the HTTP client, and the MCP tool in addition
to the core scorer — each change small and gated behind the opt-in flag,
but spread across packages. §11 Q1 is the fork that sets the true size:
the recommended cut is M; a server-side-only cut is genuinely S but does
not give the agent the signal.

## 3. Triggered by

RFC #204 ("On-Demand Memory Creation"), G1 *Knowledge Gap Signal*
(`.temp/issues/rfc-204.md`; `gh issue view 204`). Triage decision
(operator, this session): implement **G1 ONLY**. G2 (auto-research
pipeline) and G3 (`memory_create_from_research` tool) are explicitly
deferred and out of scope. From the agent seat this prevents the failure
mode of presenting a weak retrieval as a confident answer.

## 4. Context (today's state, cited)

### The retrieval → surface path, and why it has no envelope

- **Core search entry.** `SearchService.search(...)`
  (`packages/core/src/memex_core/services/search.py:58-121`) builds a
  `RetrievalRequest` and returns
  `tuple[list[MemoryUnit], Any]` — units plus a resonance task. It already
  holds an LLM handle (`self.lm`, `search.py:41,48`) and already runs a
  post-search LLM call in `summarize_search_results`
  (`search.py:123-136`) via `run_dspy_operation`. That method is the exact
  shape a coverage call takes. `SearchService.__init__`
  (`search.py:37-51`) does **not** receive an `AuditService` — relevant to
  where the event is emitted (§11 Q2).
- **Facade.** `MemexAPI.search(...)`
  (`packages/core/src/memex_core/api.py:1633`) delegates to
  `self._search.search(...)` and returns the same
  `tuple[list[MemoryUnit], Any]`.
- **HTTP endpoint.** `search_memories`
  (`packages/core/src/memex_core/server/retrieval.py:29-109`) calls
  `api.search(...)` (`:50-76`), serializes units to `MemoryUnitDTO`
  (`:85`), and returns **`ndjson_response(dtos)`** (`:107`) — a
  `StreamingResponse` of one JSON object per unit
  (`server/common.py:378`). There is an explicit **LIMITATION comment**
  (`retrieval.py:88-91`): the `degraded` signal "rides on the per-unit
  DTOs, so a fallback that returns ZERO units cannot carry it … An empty
  degraded response is indistinguishable from a genuine no-match." **A
  search-level `coverage_score` inherits this problem exactly** — and a
  knowledge gap is precisely the zero/few-results case, so per-unit
  piggybacking is disqualified for the core case.
- **HTTP client.** `RemoteMemexAPI.search(...)`
  (`packages/common/src/memex_common/client.py:474`) deserializes the
  NDJSON stream into a bare `list[MemoryUnitDTO]`. This is what the MCP
  server calls.
- **MCP tool.** `memex_memory_search`
  (`packages/mcp/src/memex_mcp/server.py:1565`), return type
  `list[McpFact | McpEvent | McpObservation]` (`server.py:1710`), calls
  `api.search(...)` (`:1756-1773`). **No response envelope** — but the
  tool already SYNTHESIZES search-level system-hint elements: a
  "No results found" hint on empty results (`server.py:1775-1786`) and a
  "⚠️ Partial results" `McpFact` tagged `['system-hint','degraded']` when
  any unit is `degraded` (`server.py:1809-1824`). This is the established
  precedent for surfacing a search-level signal on a bare list.

**Net:** there is **no search-response envelope DTO** anywhere
(`packages/common/src/memex_common/schemas.py` has the per-unit
`MemoryUnitDTO` at `:531` but no wrapper). Surfacing a search-level float
to the agent needs a new channel across the HTTP boundary — this is the
ticket's load-bearing constraint (§11 Q1).

### The pieces the change reuses

- **LLM execution.** `run_dspy_operation(lm, predictor, input_kwargs,
  operation_name=..., timeout=...)`
  (`packages/core/src/memex_core/llm.py:56-63`) is the one wrapper for all
  DSPy calls; it routes through the circuit breaker
  (`circuit_breaker.py:42`) and records
  `LLM_CALLS_TOTAL`/`LLM_CALL_DURATION_SECONDS`
  (`llm.py:123-134`). It **re-raises `CircuitBreakerOpen`**
  (`llm.py:83-86`) — the coverage call must tolerate that (return
  `coverage_score=None`, never fail the search).
- **DSPy signature pattern.** A float-scoring signature example is
  `ClassifyRelationships`/`TriageNewUnits`
  (`packages/core/src/memex_core/memory/contradiction/signatures.py:46,62`);
  the in-search precedent is `SearchSummarySignature`
  (`packages/core/src/memex_core/memory/retrieval/prompts.py`, imported at
  `search.py:125`).
- **Auditable events (EXISTS).** `audit_event(audit_service, action,
  resource_type=None, resource_id=None, **details)`
  (`packages/core/src/memex_core/services/audit.py:169-188`) — no-op when
  `audit_service is None`; fire-and-forget; writes an `AuditLog` row
  (`sql_models.py:1595`, `__tablename__ = 'audit_logs'`) and bumps
  `MEMEX_AUDIT_LOG_TOTAL`. This is the hook for a durable `knowledge_gap`
  event, satisfying the RFC's "gap events are auditable" decision.
- **Metrics.** Prometheus pattern at
  `packages/core/src/memex_core/metrics.py` — `Counter`/`Histogram` module
  objects (`:182`, `:212`); a 0–1 score histogram already exists
  (`OUTCOME_COVERAGE_RATIO`, `:288`) as a template.
- **Config / threshold home.** `RetrievalConfig`
  (`packages/common/src/memex_common/config.py:842`) already holds `0.3`
  thresholds (`similarity_threshold`, `:860`; `superseded_threshold`,
  `:892`), nested as `config.memory.retrieval`. The
  `coverage_gap_threshold` default belongs here.
- **Bool param coercion.** MCP bool params use
  `Annotated[bool, BeforeValidator(_coerce_bool), Field(...)]`
  (`_coerce_bool` at `server.py:152`; examples `apply_pre_filter`
  `server.py:1687-1702`).
- **Two `RetrievalRequest` models.** The wire/protocol model
  (`memex_common.schemas.RetrievalRequest`, used by the HTTP endpoint at
  `retrieval.py:35`) is distinct from the internal SQLModel
  (`memex_core.memory.retrieval.models.RetrievalRequest:21`). A
  `detect_gaps` field sent by the client goes on the **wire** model; the
  endpoint reads `request.detect_gaps` (the internal model need not change
  under the recommended design, since coverage is computed at the endpoint
  after `api.search` returns — see §11 Q2).

### Gates (verified this session)

- `just test` → `uv run pytest tests` — the **root `./tests/`** suite
  ONLY. `packages/core/tests` and `packages/mcp/tests` are **NOT**
  collected by this gate. Any loop-gating test MUST live in root
  `./tests/`.
- `just prek` → `uv run prek run -a` (ruff + mypy, strict).
- `.loop/config.json` sets `require_eval: true` and one enabled review
  pass (`adversarial`).

## 5. Non-goals / out of scope

- **No G2, no G3.** No `gap_filling_service.py`, no web-search / source
  discovery, no targeted extraction, no re-scoring loop, no
  `memory_create_from_research` MCP tool, no `source_type` column. The
  score triggers **no** automated action — it is a returned diagnostic
  only.
- Do NOT change the **default** (`detect_gaps=False`) behavior of memory
  search in any layer. The default path must stay byte-identical and add
  **zero** latency and **zero** LLM calls (opt-in is the whole point;
  RFC key decision #1).
- Do NOT alter ranking, fusion, reranking, MMR, token-budget packing, or
  any existing retrieval behavior. Coverage is read-only over the results
  retrieval already produced.
- Do NOT change `memex_note_search` / the document path, or `api.retrieve`
  / `MemoryEngine.recall` internals.
- Do NOT block or slow the search on the coverage call failing: circuit
  breaker open, LLM timeout, or a malformed score → `coverage_score=None`,
  no event, search returns normally.
- Do NOT invent a general search-response envelope for the **default**
  path (that would touch every consumer). Any envelope introduced is
  scoped to the `detect_gaps=True` path only (§11 Q1).

## 6. Requirements & restrictions

**Must achieve:**

- **R1.** A DSPy coverage signature: inputs `(query, result_texts)`,
  outputs a `coverage_score` in `[0, 1]` and a short `gap_description`
  (used only when below threshold). Defined alongside the existing
  retrieval signatures (`memory/retrieval/prompts.py`) following the
  `contradiction/signatures.py:46` float-score pattern. Clamp/validate the
  model's score into `[0, 1]`; a non-parseable score → `None`.
- **R2.** A single coverage assessment method on `SearchService` (e.g.
  `assess_coverage(query, texts) -> CoverageResult`) that calls
  `run_dspy_operation(..., operation_name='coverage')` exactly as
  `summarize_search_results` does (`search.py:127-134`), exposed via
  `MemexAPI` (mirror how `summarize_search_results`/other search methods
  are surfaced). It runs at most ONE LLM call and only when invoked.
- **R3.** Opt-in wiring end to end: `detect_gaps` on the wire
  `RetrievalRequest` (`memex_common.schemas`), read by the HTTP endpoint
  (`server/retrieval.py`), sent by `RemoteMemexAPI.search`
  (`client.py:474`), and exposed as a `detect_gaps` MCP tool param
  (`server.py:1565`) declared with `BeforeValidator(_coerce_bool)` like
  the sibling bool params (`server.py:1687-1702`). Default `False`
  everywhere.
- **R4.** When `detect_gaps=True` AND `coverage_score < threshold`
  (default `config.memory.retrieval.coverage_gap_threshold = 0.3`):
  - emit `audit_event(audit_service, 'knowledge_gap',
    resource_type='search', query=..., coverage_score=..., threshold=...,
    result_count=...)` (durable audit trail; RFC decision #3);
  - bump a Prometheus counter and observe the score in a histogram
    (`metrics.py`);
  - surface the signal to the agent (§11 Q1 decides the mechanism).
- **R5.** The `coverage_score` (and, below threshold, a gap hint) is
  visible to the MCP caller when `detect_gaps=True`. The score must
  survive the **zero-results** case (a gap's most important case), which
  rules out per-unit DTO piggybacking (see §4 LIMITATION at
  `retrieval.py:88-91`).
- **R6.** Fail-open resilience: any coverage-call failure
  (`CircuitBreakerOpen`, `asyncio.TimeoutError`, parse error) leaves the
  search result exactly as if `detect_gaps=False` was passed, logs at
  WARNING, and sets `coverage_score=None`.

**Restrictions (repo principles, cited):**

- **Simplicity / surgical** (`CLAUDE.md` §2, §3): minimum code; every
  changed line traces to this ticket; no speculative G2/G3 hooks. Reuse
  `run_dspy_operation`, `audit_event`, and the `_coerce_bool`/system-hint
  patterns rather than new machinery.
- **All code needs tests; tests are real code**
  (`.claude/rules/python-testing.md`): ship tests; assert observable
  behavior; no `skip`/`xfail`/`# type: ignore` to green a gate. The LLM
  call is mocked (paid boundary) — do NOT hit a real model in the default
  gate; the coverage test is offline and unmarked.
- **Mark & exclude slow tests** (`.claude/rules/python-testing.md`): any
  test needing a real LLM carries `@pytest.mark.llm` and stays out of the
  default run; the gating tests are offline.
- **Do not silence gates** (`.claude/rules/prek-code-quality.md`): `just
  prek` must pass by fixing causes.
- **Pre-existing issues** (`.claude/rules/pre-existing-issues.md`): if the
  wire/internal `RetrievalRequest` split or the NDJSON limitation surfaces
  an adjacent defect, record it — do not silently work around it.
- **Adversarial review** (`.claude/rules/adversarial-reviews.md`): run the
  adversarial pass before declaring done.
- **Slop scan** (`.claude/rules/slop-scan-for-docs.md`): every edited MCP
  description string / prose surface passes the P0 + sentence checks;
  every backticked identifier resolves.
- **Agent-surface budgets** (`CLAUDE.md` agent-surface constraint): the
  `memex_memory_search` description gains at most a terse `detect_gaps`
  clause; stay within the per-tool char cap enforced by
  `packages/mcp/tests/test_description_budgets.py`.

## 7. Code surface (files to touch; anchors to re-open)

Core scorer:
- `packages/core/src/memex_core/memory/retrieval/prompts.py` — **add** the
  coverage DSPy signature (sibling to `SearchSummarySignature`).
- `packages/core/src/memex_core/services/search.py:123-136` — **add**
  `assess_coverage(...)` mirroring `summarize_search_results`.
- `packages/core/src/memex_core/api.py:1633` (and the facade method-surface
  near it) — **expose** the coverage method on `MemexAPI`.
- `packages/common/src/memex_common/config.py:842` (`RetrievalConfig`) —
  **add** `coverage_gap_threshold: float = 0.3` (+ optional
  `coverage_timeout` / model knob; keep minimal).

Opt-in wiring + surfacing (mechanism per §11 Q1):
- `packages/common/src/memex_common/schemas.py` — **add** `detect_gaps:
  bool = False` to the wire `RetrievalRequest`; **add** the response
  envelope DTO IF Q1 = envelope-on-flag.
- `packages/core/src/memex_core/server/retrieval.py:29-109` — **branch**
  on `request.detect_gaps`: after `api.search` returns, compute coverage,
  `audit_event('knowledge_gap', …)` + metric below threshold, and return
  the envelope (Q1) instead of NDJSON on the flagged path. Default path
  returns `ndjson_response(dtos)` unchanged.
- `packages/common/src/memex_common/client.py:474` — **parse** the
  envelope on the `detect_gaps=True` path; bare-list default unchanged.
- `packages/mcp/src/memex_mcp/server.py:1565` — **add** `detect_gaps`
  param (`_coerce_bool`, `server.py:152`); **surface** `coverage_score`
  and, below threshold, inject a gap system-hint `McpFact` reusing the
  degraded-warning precedent (`server.py:1809-1824`).
- `packages/mcp/src/memex_mcp/server.py:1553-1560` — **extend** the tool
  description with a terse `detect_gaps` clause (budget-capped).

Metrics:
- `packages/core/src/memex_core/metrics.py:182,288` — **add**
  `KNOWLEDGE_GAP_TOTAL` counter and a `COVERAGE_SCORE` histogram
  (template: `OUTCOME_COVERAGE_RATIO` at `:288`).

Tests (root `./tests/` — the only place the gate collects):
- **`tests/test_memory_search_coverage_gap.py`** (NEW) — see §8.

## 8. Tests & validation gates

**Gates:** `just test` (`uv run pytest tests`, root suite only) and `just
prek`. Both green at close.

**Eval marker (REQUIRED — WRITTEN & VALIDATED):**
`.loop/evals/memory-search-coverage-gap-signal.md` exists and passes
`loopctl eval` (`valid`). It pins 8 deterministic scenarios at 100%: three
guardrails (default path byte-identical + zero coverage LLM calls;
fail-open on coverage failure; zero-results still carries the signal),
plus the envelope-on-flag surfacing rows (Q1=A), below/above-threshold
event + hint behavior, and the gap metric. Q1=A / Q2=endpoint are recorded
in the marker as the accepted recommendation pending final user
confirmation.

**New offline test `tests/test_memory_search_coverage_gap.py`** (no
Docker, no real LLM — mock the DSPy predictor / `run_dspy_operation`),
asserting:

- **(a) GUARDRAIL — default path untouched.** With `detect_gaps` omitted
  / `False`, no coverage predictor is invoked (call-count spy == 0) and
  the returned payload is byte-identical to today's. Latency/LLM
  invariant.
- **(b) Below threshold emits + surfaces.** With `detect_gaps=True` and a
  mocked coverage score `< 0.3`: exactly one `knowledge_gap`
  `audit_event` fires with `query`/`coverage_score`/`threshold`/
  `result_count`; the counter increments; the caller receives the score
  (and, at the MCP layer, the injected gap system-hint `McpFact`).
- **(c) Above threshold is quiet.** Mocked score `>= 0.3`: no
  `knowledge_gap` event, no injected hint; the score is still returned.
- **(d) GUARDRAIL — zero results still carries the signal.** A search
  returning **zero units** with `detect_gaps=True` still yields a
  `coverage_score` / gap signal to the caller — the case per-unit
  piggybacking (`retrieval.py:88-91`) cannot serve. Pins R5.
- **(e) GUARDRAIL — fail-open.** Coverage call raising
  `CircuitBreakerOpen` / `asyncio.TimeoutError` → search returns its units
  unchanged, `coverage_score=None`, no event, WARNING logged. Pins R6.

Harness note: prefer invoking the MCP tool's underlying `.fn` with a
stub `api`/`ctx` (the server supports internal `.fn` callers) and mocking
`run_dspy_operation` — self-contained and offline. The `packages/mcp` and
`packages/core` conftests are NOT collected by `just test`, so the file
must be root-level and self-sufficient. A separate `@pytest.mark.llm` test
exercising the real signature MAY be added but must stay out of the
default run.

## 9. Risk assessment

- **Blast radius.** Concentrated on the opt-in path. The one shared
  surface at risk is the **default** search payload in three layers
  (endpoint NDJSON, client parse, MCP list) — guarded by test (a)
  byte-identity across all three. The wire `RetrievalRequest` gains one
  optional field (additive, defaulted). Metrics/audit are additive.
- **Reversibility.** High — delete the param, the signature, the scorer,
  the envelope branch, and the metric; the default path is defined to be
  unchanged.
- **Likeliest failure modes.**
  1. *The default path is not actually byte-identical* — e.g. the endpoint
     branch subtly reorders or the client parse changes on the default
     path. Caught by (a); keep the `detect_gaps=False` code path literally
     the pre-change code.
  2. *Coverage failure sinks the search* — the point of R6. A missing
     try/except around the coverage call turns an opt-in diagnostic into a
     search outage. Caught by (e).
  3. *Zero-results signal lost* — implementing surfacing via per-unit
     piggyback repeats the `degraded` limitation and drops the signal
     exactly when it matters most. Caught by (d); Q1's recommended
     envelope avoids it structurally.
  4. *Latency on the default path* — accidentally computing coverage (or
     even building the predictor) when the flag is off. Caught by (a)'s
     call-count spy.
  5. *Test lands where the gate can't see it* (`packages/**/tests`) — must
     be root `./tests/`.
  6. *Description budget overflow* — the `detect_gaps` clause pushes
     `memex_memory_search` over its char cap
     (`test_description_budgets.py`). Keep it terse.

## 10. Subtickets (ordered)

1. **Resolve §11 Q1** (surfacing mechanism) and **Q2** (where coverage is
   computed / how the audit service is reached) — these gate the endpoint
   and client shape. Operator/implementer before code.
2. **Config + metrics + signature.** Add `coverage_gap_threshold`
   (`config.py:842`), `KNOWLEDGE_GAP_TOTAL` + `COVERAGE_SCORE`
   (`metrics.py`), and the coverage DSPy signature
   (`retrieval/prompts.py`). → verify: `just prek` green; config default
   reads `0.3`.
3. **Core scorer.** `SearchService.assess_coverage`
   (`search.py:123-136` sibling) + `MemexAPI` exposure (`api.py:1633`),
   with R6 fail-open. → verify: unit test drives it with a mocked
   predictor and with a raising predictor.
4. **Opt-in wiring + surfacing** per Q1: wire `detect_gaps` through
   `schemas.RetrievalRequest` → endpoint branch (`retrieval.py`, emit
   `audit_event` + metric) → `client.py:474` → MCP tool param + hint
   injection (`server.py:1565`, `:1809` precedent). → verify: tests (b),
   (c), (d).
5. **Guardrail tests + description.** Land (a) and (e); extend the tool
   description (budget-capped). → verify: `just test` + `just prek` green;
   `test_description_budgets.py` passes.
6. **Slop scan + adversarial review** of the edited prose and the diff. →
   verify: reviewer confirms default path unchanged, fail-open holds,
   zero-results signal survives; P0 slop checks clean.

## 11. Open questions (forks for the operator)

- **Q1 — How does the `coverage_score` reach the agent, given no search
  envelope?** Options: **(A) envelope-on-flag** — when `detect_gaps=True`,
  the endpoint returns a JSON object `{coverage_score, gap_description,
  units:[...]}` instead of the NDJSON stream; the client parses it on that
  path only; the MCP tool reads the score and injects a gap system-hint
  `McpFact` below threshold. Default path stays byte-identical NDJSON.
  **(B) server-side-only** — compute coverage, `audit_event` + metric +
  structlog, and do NOT return it to the caller. **(C) full unconditional
  envelope** — replace the bare-list contract everywhere.
  *Recommendation:* **(A)**. It delivers the agent-facing signal the
  triage brief calls for, keeps the default path and its latency
  untouched, survives zero results (unlike per-unit piggyback), and
  localizes blast radius to the opt-in path. (B) is genuinely Size S but
  fails the stated goal (the agent never sees the score). (C) is a
  cross-cutting refactor out of proportion to G1. This fork sets the
  ticket's size (§2).
- **Q2 — Where is coverage computed, and how does it reach an
  `AuditService`?** `SearchService` has `self.lm` but no `AuditService`
  (`search.py:37-51`). Options: compute at the **HTTP endpoint** after
  `api.search` returns (the endpoint can call a new `api.assess_coverage`
  and already has request context + `background_tasks`; the `AuditService`
  is reachable there via the app/api wiring), OR inject `AuditService` into
  `SearchService` and emit inside core. *Recommendation:* compute + emit at
  the **endpoint** — it keeps `SearchService.search`'s signature and the
  internal `RetrievalRequest` unchanged, needs no new constructor
  dependency, and matches how the endpoint already threads
  cross-cutting concerns (degraded set, `background_tasks`,
  `resonance_task`). Confirm the endpoint's access to the `AuditService`
  while wiring (grep how other routes obtain it).
- **Q3 — Coverage input: unit texts, or texts + scores/titles?** The
  minimal, model-friendly input is the ranked result texts plus the query.
  *Recommendation:* pass the query and the top-N result texts (cap N to
  bound tokens/latency, e.g. the returned `limit`); do not feed internal
  scores. Surface N as a config knob only if the operator wants it.
- **Q4 — Threshold semantics: strict `<` vs `<=`, and is `0.3` right?**
  RFC says "below 0.3." *Recommendation:* strict `<
  coverage_gap_threshold`, default `0.3`, configurable via
  `RetrievalConfig`. Flag for the operator to tune once real scores are
  observed (the metric histogram exists precisely to calibrate this).
- **Q5 — Does the CLI `memex memory search` also expose `detect_gaps`?**
  The RFC/triage centers the MCP tool. *Recommendation:* MCP + HTTP + core
  only for this ticket; leave the CLI flag to a trivial follow-up if
  wanted. Keeps the surface small. Flag, do not build.

---

**Eval marker:** `require_eval: true` — author
`.loop/evals/memory-search-coverage-gap-signal.md` with the `create-eval`
skill before implementation. This change carries guardrails (default path
byte-identical + zero LLM calls; fail-open on coverage failure;
zero-results still carries the signal); prose alone leaves those wobbly,
so the eval is mandatory, not optional.
