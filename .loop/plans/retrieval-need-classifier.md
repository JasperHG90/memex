# retrieval-need-classifier: gate the retrieval pipeline behind a fast, fail-open "does this query need memory?" classifier (RFC #230, Phase 1 only)

## 1. Title

Add a **retrieval-need classifier** in front of the TEMPR retrieval
pipeline: before embedding + strategy dispatch, decide whether the
query actually needs memory retrieval, and short-circuit (return an
empty result cheaply) when it demonstrably does not. This is **Phase 1
of RFC #230 and ONLY Phase 1** — the "whether to retrieve" gate. The
pre-retrieval *reasoning/planning* phase (`RetrievalPlan`,
`reasoning.py`) and the post-retrieval *restructuring* phase
(`restructuring.py`) are explicitly deferred (see §5).

## 2. Size / Effort

**M.** One new module (`classifier.py`), one short-circuit hook at a
single choke point in the engine, three plumbed config/flag fields
(global config, per-request field, MCP tool param), and an audit
log/metric. The effort is dominated by (a) getting the fail-open
default and the byte-identical default path exactly right, and (b) the
recall-preservation test surface, not by the classifier logic — which
is deliberately a small heuristic, not an LLM call (see §6 R2 and
§11 Q1).

## 3. Triggered by

RFC #230 (`gh issue view 230`; `/home/vscode/workspace/.temp/issues/rfc-230.md`)
Phase 1: Memex retrieval is *reflex* (query → embed → top-K), running
the full pipeline even for turns that need no memory. Triage decision
(team lead, 2026-07-23): implement the **classifier phase only**; the
goal-reasoning/planning phase stays out until the #206 E2 eval shows it
pays its latency.

## 4. Context (today's state, cited)

### The retrieval call path (three altitudes)

- `packages/core/src/memex_core/api.py:1633` — `MemexAPI.search(...)`
  (flat kwargs) delegates to `self._search.search(...)`.
  `MemexAPI.retrieve(request: RetrievalRequest)` at `api.py:1628` is
  the request-object sibling.
- `packages/core/src/memex_core/services/search.py:58` —
  `SearchService.search(...)` builds a `RetrievalRequest` and opens a
  metastore session; `SearchService.retrieve(request)` at
  `search.py:53` calls `self.memory.recall(session, request)`.
- `packages/core/src/memex_core/memory/engine.py:291` —
  `MemoryEngine.recall(session, request)` → `engine.py:303`
  `await self.retrieval.retrieve(session, request)`.
- `packages/core/src/memex_core/memory/retrieval/engine.py:563` —
  `RetrievalEngine.retrieve(self, session, request) -> tuple[list[MemoryUnit], dict|None]`.
  **This is the choke point.** The first real work is the GUC pin at
  `engine.py:577` (`SELECT set_limit(0.3)`); query expansion at
  `engine.py:581`; **embedding at `engine.py:597`
  (`_get_embeddings_cached`)**; NER at `engine.py:684`; strategy
  dispatch loop at `engine.py:717`. A gate placed at method entry
  (before `engine.py:577`) precedes ALL embedding/NER/strategy cost.

### The request model (where a per-request override rides)

- Internal engine model: `packages/core/src/memex_core/memory/retrieval/models.py:21`
  `class RetrievalRequest(SQLModel)`. Precedent per-request booleans:
  `apply_pre_filter` (`models.py:81` region — the `False`-default
  bypass), `expand_query`, `include_stale`. `VALID_STRATEGIES`
  frozenset at `models.py:1`. Cross-field validator
  `validate_request_fields` at `models.py:168`.
- Wire/protocol model (public HTTP): `packages/common/src/memex_common/schemas.py:269`
  `class RetrievalRequest(BaseModel)`. A per-request opt-out must be
  mirrored on BOTH models (the internal SQLModel is what the engine
  reads; the wire model is what the HTTP API accepts).

### Config (where a global toggle rides)

- `packages/common/src/memex_common/config.py:842`
  `class RetrievalConfig(BaseModel)`. Precedent on/off knob:
  `temporal_extraction_enabled: bool` (`config.py:896`),
  `temporal_concretization_enabled` (`config.py:900`),
  `fact_type_partitioned_rrf` (`config.py:907`). A global
  `classifier_enabled: bool` belongs here alongside these.
- Per-vault overrides today live in
  `packages/common/src/memex_common/vault_policy.py:42`
  `class VaultPolicy(BaseModel)` (governs synthesis surfaces, not
  retrieval yet). Per-vault classifier config is an **open question**
  (§11 Q3), deferred.

### LLM surface + circuit breaker (why the default is NOT an LLM call)

- `packages/core/src/memex_core/llm.py:56`
  `run_dspy_operation(lm, predictor, input_kwargs, ...)` — the only
  cheap structured-LLM surface. It is wired to the circuit breaker:
  `pre_call()` at `llm.py:82` **raises `CircuitBreakerOpen`** when the
  breaker is open. `circuit_breaker.py:42` `class CircuitBreaker`.
  Consequence: an LLM-based classifier would put an LLM call — and its
  200-500ms latency and its breaker-open failure mode — *in front of
  every retrieval*. When the breaker is open, an LLM gate would either
  reject retrieval (recall regression) or need its own fallback. This
  is why the default classifier is a heuristic (§6 R2, §11 Q1).

### Auditability surfaces that exist today

- Retrieval uses stdlib `logging` (NOT structlog):
  `logging.getLogger('memex.core.memory.retrieval.engine')` at
  `engine.py:82`. Existing per-stage profile line to mirror:
  `logger.warning('PROFILE retrieve | expand=... embed=... ...')` at
  `engine.py:1033-1048` (stage timings `t_expand`/`t_embed`/… already
  collected).
- Metrics: `packages/core/src/memex_core/metrics.py` — counters follow
  a `Counter(...)` pattern (`LLM_CALLS_TOTAL` at `metrics.py:182`,
  `CIRCUIT_BREAKER_REJECTIONS_TOTAL` at `metrics.py:203`,
  `MEMEX_AUDIT_LOG_TOTAL` at `metrics.py:212`).
  `RETRIEVAL_DURATION_SECONDS` at `metrics.py:28` is declared but
  unused — a new `RETRIEVAL_CLASSIFIER_DECISION_TOTAL` counter is
  new-but-consistent with the file's pattern.
- The engine already returns a second tuple element (the optional
  `dict` / resonance context) and honours `request.debug`
  (`models.py` `debug` field) — the classifier decision can be
  surfaced there for the debug path.

### Gates

- `just test` → `uv run pytest tests` (`justfile:65-66`) — runs the
  **root `/home/vscode/workspace/tests/` suite ONLY**. Per-package
  `packages/core/tests/` and `packages/mcp/tests/` are **NOT** part of
  this gate. So any test that must block the loop lives in root
  `tests/`.
- `just prek` → `uv run prek run -a` (ruff + mypy, strict).
- Root retrieval E2E tests already present (run under the gate):
  `tests/test_e2e_retrieval_augmentation.py`,
  `tests/test_e2e_search_budget.py`,
  `tests/test_e2e_keyword_search_tsvector.py`.
- Core-only unit tests (NOT under the loop gate):
  `packages/core/tests/unit/memory/retrieval/test_retrieval_engine.py`
  and siblings.

## 5. Non-goals / out of scope

**Hard stop-line — this ticket is RFC #230 Phase 1 (classifier) ONLY.**

- Do **NOT** implement the pre-retrieval *reasoning/planning* phase:
  no `RetrievalPlan`, no `reasoning.py`, no sub-query decomposition, no
  `info_type` / `aggregation_strategy` / `max_results_per_query`
  synthesis. That phase stays out **until the #206 E2 eval shows it
  pays its latency** (team-lead triage decision, 2026-07-23).
- Do **NOT** implement post-retrieval *restructuring* (`restructuring.py`,
  decouple/merge/chain/contrast/compose aggregation). Out of scope.
- Do **NOT** make "which strategy family fits" a required output. The
  classifier answers exactly ONE binary question: *does this query
  need memory retrieval at all?* Optional strategy-family hinting is
  **deferred to the reasoning phase** (§11 Q4). Keep the output lean.
- Do **NOT** turn the classifier on by default. The global default is
  OFF so the shipped default retrieval path is byte-identical to
  today (§6 R4).
- Do **NOT** add an LLM call to the default classifier path (§6 R2).
- Do **NOT** modify TEMPR strategy internals, RRF/MMR fusion, the
  reranker, the staleness/score serialization, or the note_search /
  document path.
- Do **NOT** add per-vault classifier config in this ticket (extend
  `VaultPolicy`) — deferred (§11 Q3).
- Do **NOT** build the #206 atomic-ops retrieval eval here (planned
  separately, per the reworked `atomic-ops-retrieval-eval-suite`
  ticket — an **Arize Phoenix trace-based** eval, NOT a `packages/eval`
  / `memex_eval` suite; the user has directed that retrieval evals run
  against actual Phoenix traces, not the eval framework). This ticket
  ships its own proxy recall-preservation tests (§8), which are
  standing loop guardrails in root `tests/` (plain pytest, no
  eval-package usage), and is written so the Phoenix-trace eval can
  later assert against the classifier's audited decisions.

## 6. Requirements & restrictions

**Must achieve:**

- **R1 — Single choke point.** The gate runs at exactly one place:
  the entry of `RetrievalEngine.retrieve` (`engine.py:563`), BEFORE
  the GUC pin at `engine.py:577` and therefore before expansion,
  embedding (`engine.py:597`), NER (`engine.py:684`) and strategy
  dispatch (`engine.py:717`). When the classifier decides "no
  retrieval needed", `retrieve` returns `([], None)` immediately (the
  same empty shape a zero-result search returns), skipping all
  downstream cost. Do not scatter the gate across `api.search` /
  `SearchService.search` — one choke point that sees the full typed
  `RetrievalRequest`. (RFC #230 "Called before the full retrieval
  pipeline in engine.py".)
- **R2 — Fast, non-LLM default; no circuit-breaker coupling.** The
  default classifier is a **heuristic** (`classifier.py`), pure and
  synchronous, no DB round-trip, no embedding, no `run_dspy_operation`
  call — so it adds no LLM latency and cannot be tripped by an open
  circuit breaker (`llm.py:82`). Recommendation locked by §11 Q1. An
  LLM/learned upgrade path may be *sketched* in a docstring but MUST
  NOT be wired in this ticket.
- **R3 — Fail-open / recall-preserving (the load-bearing safety
  property).** The gate short-circuits **only** on high-precision
  "no-memory-needed" signals. Any ambiguity → retrieve. A false
  positive (skipping retrieval when memory was needed) is a recall
  regression and is the failure this design must avoid; a false
  negative (retrieving when it was unnecessary) merely forgoes the
  saving and is acceptable. Every heuristic rule must be justified as
  high-precision-for-skip.
- **R4 — Byte-identical default path.** With `classifier_enabled`
  defaulting to `False` (global `RetrievalConfig`, `config.py:842`),
  the retrieval path, its results, and its serialized output are
  identical to pre-change. This is a guardrail asserted by test (§8).
- **R5 — Per-request opt-out override.** A per-request field (name TBD
  in §11 Q2; recommend `force_retrieval: bool = False`) on BOTH
  `RetrievalRequest` models (`models.py:21` internal +
  `schemas.py:269` wire) forces the full pipeline regardless of the
  global toggle or the heuristic verdict. Mirror the `apply_pre_filter`
  precedent.
- **R6 — MCP opt-in/opt-out surface.** The `memex_memory_search` tool
  (`packages/mcp/src/memex_mcp/server.py:1551`, handler at
  `server.py:1565`) exposes the override as one new `Annotated`
  param, threaded to the request. Keep the MCP tool description within
  its char budget (per AGENTS.md agent-surface constraints) — add the
  minimum ("set true to force retrieval even if the need-classifier
  would skip"). No universal prose (see the description-budget tests).
- **R7 — Auditable decision.** Every gate evaluation emits (a) a
  structured log record on the engine logger (`engine.py:82`,
  mirroring the PROFILE line at `engine.py:1033`) carrying the
  decision (`retrieve` | `skip`), the reason/signal that fired, and
  whether an override was in effect; and (b) a Prometheus counter
  `RETRIEVAL_CLASSIFIER_DECISION_TOTAL` (labels `decision`, `reason`)
  in `metrics.py` following the existing `Counter(...)` pattern
  (`metrics.py:182`). When `request.debug` is set, the decision is
  also surfaced in the returned debug dict (the `retrieve` second
  tuple element).

**Restrictions (repo principles, cited):**

- `.claude/rules/python-testing.md` (`all-code-needs-tests`,
  `tests-are-real-code`, `mark-and-exclude-slow-tests`): the change
  ships with tests; a loop-blocking assertion lives in root `tests/`
  (only dir `just test` collects); heuristic-only tests may also live
  in `packages/core/tests/unit/`. No `skip`/`xfail`/`# type: ignore`
  to green a gate. Fully typed (mypy strict).
- `.claude/rules/pre-existing-issues.md`: fix, don't route around, any
  adjacent breakage the change surfaces.
- `.claude/rules/adversarial-reviews.md`: run an adversarial review
  (via the loop's `adversarial` pass) before done — scrutinise the
  fail-open property and the byte-identical default especially.
- AGENTS.md agent-surface constraints + `packages/mcp/tests/test_description_budgets.py`
  and `test_no_universal_content_in_descriptions.py`: the new MCP
  param description must stay within budget and carry no universal
  content.
- Code style (AGENTS.md): single quotes, 100 cols, ruff, async I/O,
  Python ≥3.12.

## 7. Code surface (files to touch; anchors)

- **NEW** `packages/core/src/memex_core/memory/retrieval/classifier.py`
  — the heuristic classifier: a pure function/small class returning a
  decision object `(need_retrieval: bool, reason: str)`. No I/O, no
  LLM. Docstring notes the learned-upgrade path but does not wire it.
- `packages/core/src/memex_core/memory/retrieval/engine.py:563-578` —
  insert the gate at `retrieve` entry: read `classifier_enabled` (from
  config on the engine) and the per-request override; on "skip" log +
  count + return `([], None)` before `engine.py:577`. Import the
  classifier + counter.
- `packages/core/src/memex_core/memory/retrieval/models.py:21` — add
  the per-request override field (e.g. `force_retrieval: bool = False`).
- `packages/common/src/memex_common/schemas.py:269` — mirror the
  override field on the wire `RetrievalRequest`.
- `packages/common/src/memex_common/config.py:842` — add
  `classifier_enabled: bool = False` (+ optional heuristic tuning
  fields only if a rule needs one; keep minimal) to `RetrievalConfig`.
- `packages/core/src/memex_core/metrics.py` (near `:182`) — add
  `RETRIEVAL_CLASSIFIER_DECISION_TOTAL = Counter(...)` with labels
  `decision`, `reason`.
- `packages/mcp/src/memex_mcp/server.py:1565-1624` — add one
  `Annotated` param to `memex_memory_search` and thread it into the
  request/kwargs passed down.
- Config threading: wherever `RetrievalEngine` is constructed with its
  `RetrievalConfig` (verify the constructor around `engine.py:430-512`
  reads config) — ensure `classifier_enabled` reaches the engine. If
  the engine does not already hold the `RetrievalConfig`, thread it;
  if threading requires touching a file not listed here, that is the
  `out-of-scope-fix-needed` blocker — surface it, do not silently
  expand.
- **NEW TEST** `tests/test_e2e_retrieval_need_classifier.py` (root
  `tests/` — the only dir `just test` collects) — the loop-blocking
  recall-preservation + guardrail assertions (§8).
- **NEW TEST** `packages/core/tests/unit/memory/retrieval/test_retrieval_classifier.py`
  — pure heuristic unit tests (parametrized skip/retrieve cases + the
  fail-open ambiguity cases). Runs under `pytest packages/core/tests`,
  not the loop gate, but required by `all-code-needs-tests`.

## 8. Tests & validation gates

**Gates:** `just test` (root `tests/` only) and `just prek`. Both green
at close.

**Loop-blocking test — `tests/test_e2e_retrieval_need_classifier.py`:**

1. **[GUARDRAIL] Default path byte-identical.** With
   `classifier_enabled` defaulted `False`, a search over a seeded
   vault returns exactly the same units (and serialized shape) as
   before the change. Assert against the existing
   `test_e2e_retrieval_augmentation.py` expectations / a captured
   baseline.
2. **Recall preserved when classifier ON, for needs-memory queries.**
   With `classifier_enabled=True`, a query that clearly needs memory
   (references vault content) returns the **same unit set** as with
   the classifier OFF — the gate must NOT drop it. This is the proxy
   for the #206 atomic-ops "must not reduce recall on scenarios that
   DO need memory" acceptance criterion (measured separately via the
   Phoenix-trace eval, per `atomic-ops-retrieval-eval-suite`).
3. **Short-circuit fires for a clear no-memory query.** With
   `classifier_enabled=True`, a query the heuristic classifies as
   no-retrieval-needed returns `[]` **without** running strategies
   (assert via a spy/call-count that the embedding or strategy step
   was not invoked, mirroring the call-count technique in the
   inline-metadata eval).
4. **Per-request override wins.** `force_retrieval=True` (or chosen
   name) with `classifier_enabled=True` on a would-be-skipped query
   runs the full pipeline and returns results.
5. **[GUARDRAIL] Fail-open on ambiguity.** An ambiguous query (not a
   high-precision skip signal) retrieves — assert it is NOT
   short-circuited.
6. **Decision is audited.** The classifier decision + reason is
   emitted (assert the counter increments with the right
   `decision`/`reason` labels, or the debug dict carries the decision
   when `request.debug=True`).

**Heuristic unit tests —
`packages/core/tests/unit/memory/retrieval/test_retrieval_classifier.py`:**
parametrized `(query, expected_decision, expected_reason)` covering
each skip rule and, critically, a battery of ambiguity cases that MUST
resolve to `retrieve` (R3). Pure, offline, deterministic.

No new external-system dependency; the E2E tests reuse the existing
testcontainer Postgres fixtures the sibling `test_e2e_*` files use.

## 9. Risk assessment

- **Blast radius:** the gate sits on the hottest read path (every
  `memory_search`). A wrong short-circuit silently returns no memory —
  the highest-consequence failure. Mitigated by R3 (fail-open),
  R4 (default OFF → shipped path unchanged), and tests 1/2/5.
- **Reversibility:** trivial — the default-OFF global toggle means the
  feature is dormant until explicitly enabled; revert is a config flip
  or a clean module removal.
- **Likeliest failure modes:**
  1. *Recall regression* — a heuristic that skips too eagerly. Guard:
     R3 + test 2 + the ambiguity battery.
  2. *Default path drift* — accidentally changing behaviour when the
     flag is off (e.g. constructing the classifier unconditionally
     with a side effect). Guard: test 1, and keep the gate strictly
     behind the `classifier_enabled` branch.
  3. *Scope creep into the reasoning phase* — adding `RetrievalPlan`
     "while we're here". Explicitly forbidden (§5). The classifier
     returns a binary + reason, nothing more.
  4. *Circuit-breaker coupling* — someone reaches for `run_dspy_operation`
     for "smarter" classification, importing the breaker-open failure
     mode into every retrieval. Forbidden by R2.
  5. *Config not reaching the engine* — if `RetrievalEngine` does not
     already hold its `RetrievalConfig`, the toggle is a no-op. Verify
     the constructor threading (§7) and test that ON actually gates.

## 10. Subtickets (ordered)

1. **Classifier module + unit tests.** Write `classifier.py` (pure
   heuristic, `(need_retrieval, reason)`) and its parametrized unit
   test incl. the fail-open ambiguity battery. → verify: unit tests
   pass; every skip rule justified high-precision.
2. **Config + request fields.** Add `classifier_enabled` to
   `RetrievalConfig` (`config.py:842`, default `False`) and the
   per-request override to both `RetrievalRequest` models
   (`models.py:21`, `schemas.py:269`). → verify: mypy green; defaults
   preserve current behaviour.
3. **Engine gate + audit.** Insert the short-circuit at
   `retrieve` entry (`engine.py:563`), the log record (mirror
   `engine.py:1033`), the `RETRIEVAL_CLASSIFIER_DECISION_TOTAL`
   counter (`metrics.py`), and the debug-dict surfacing. Thread config
   into the engine if needed. → verify: gate fires only when enabled;
   returns `([], None)` on skip.
4. **MCP override param.** Add the `Annotated` flag to
   `memex_memory_search` (`server.py:1565`) and thread it. → verify:
   description-budget + no-universal-content MCP tests pass.
5. **Root E2E recall-preservation + guardrail tests.** Write
   `tests/test_e2e_retrieval_need_classifier.py` (§8 cases 1-6). →
   verify: `just test` green.
6. **Gates + adversarial review.** `just test` + `just prek` green;
   adversarial pass scrutinising fail-open and byte-identical default.
   → verify: reviewer signs off.

## 11. Open questions (forks for the operator; each with a recommendation)

- **Q1 — Classifier implementation: heuristic vs cheap LLM vs small
  model?** *Recommendation:* **heuristic, non-LLM, for this ticket.**
  RFC #230 itself specifies Phase 1 as "a fast, lightweight classifier
  (not a full LLM call)... simple heuristic initially, upgradeable to
  a learned classifier." An LLM gate adds 200-500ms per retrieval and
  couples every read to the circuit breaker (`llm.py:82`) — the wrong
  trade for a saving whose whole point is *lower* latency. Ship the
  heuristic; leave a documented upgrade seam. Settle before build.
- **Q2 — Per-request override field name.** *Recommendation:*
  `force_retrieval: bool = False` (reads naturally at the MCP surface:
  "force retrieval even if the classifier would skip"). Alternative:
  `classify_need: bool` (opt-*in*) — rejected because opt-in would
  make callers responsible for enabling the saving, whereas the global
  toggle + fail-open default is the safer rollout. Confirm the name.
- **Q3 — Per-vault configurability (RFC #230 open question 1).**
  *Recommendation:* **defer.** Global `classifier_enabled` +
  per-request override is enough to roll out and to run the #206 E2
  ablation. Extending `VaultPolicy` (`vault_policy.py:42`) is a
  follow-up once E2 quantifies the win. Out of scope here (§5).
- **Q4 — Should the classifier also emit a strategy-family hint?**
  *Recommendation:* **no — defer to the reasoning phase.** The
  team-lead scope is the binary gate only. Emitting `info_type` /
  strategy hints is the `RetrievalPlan`'s job, which is out of scope
  until E2 justifies it. Keep the output `(need_retrieval, reason)`.
- **Q5 — Skip semantics: return `[]` vs a sentinel/flag.**
  *Recommendation:* return `([], None)` — the existing empty-result
  shape — so every consumer already handles it and no caller needs to
  learn a new "skipped" state. The *audit* surfaces the skip (R7); the
  *result contract* stays unchanged. Confirm no consumer distinguishes
  "skipped" from "genuinely empty" in a way that matters (grep
  consumers of `search`/`recall` return values).
- **Q6 — Which heuristic signals count as high-precision skips?** This
  is the crux of R3 and should be co-authored in the eval (§ Eval
  marker). *Recommendation seed:* skip only on clearly memory-free
  turns (pure greetings/acknowledgements, self-contained
  arithmetic/code-gen with no reference to prior context or vault
  entities); treat anything referencing past events, entities,
  preferences, or "we/last/remember"-shaped language as *retrieve*.
  Finalise the rule list as the eval's Input column.

---

**Eval marker (MANDATORY — `.loop/config.json` sets `require_eval: true`).**
The loop will refuse to pick this ticket up until
`.loop/evals/retrieval-need-classifier.md` exists. A companion eval
marker is authored alongside this ticket; co-author/adjust it with the
`create-eval` skill. Acceptance is tied to the **#206 atomic-ops
retrieval eval (planned separately, per the reworked
`atomic-ops-retrieval-eval-suite` ticket)** — an **Arize Phoenix
trace-based** eval that measures recall against actual retrieval
traces, NOT a `packages/eval` / `memex_eval` suite: the classifier,
when enabled, **must not reduce recall on scenarios that DO need
memory**. Until that eval lands, this ticket's own root `tests/`
recall-preservation cases (§8 tests 1, 2, 5) are the standing proxy
for that criterion — plain pytest loop guardrails, not eval-package
usage — and the gate must stay fail-open (R3). The eval marker's Input
column is also where the high-precision skip signal list (Q6) gets
pinned.
