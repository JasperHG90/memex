# atomic-ops-retrieval-eval-suite: trace-based atomic-ops retrieval eval on Arize Phoenix (RFC #206 E2)

> **⛔ BLOCKING PREREQUISITE FOR THE TIER-1 RETRIEVAL WAVE — LANDS FIRST.**
> The Phoenix-trace-based eval capability does NOT exist yet. This ticket
> builds it, and it MUST be implemented and merged **before any
> retrieval/pipeline ticket in the wave is picked up** (#230, #155, #247,
> #271). Loop ledger ordering: this slug is a hard predecessor of every
> retrieval-touching ticket in the wave — do not `advance`/pick up those
> tickets until this one is `done`. Rationale: those tickets are gated on
> being *measurable*, and the measurement instrument is this ticket's
> deliverable. There is nothing to measure against until it lands.
>
> **Deliverable is a WORKING capability, not a spec.** "Done" means the
> end-to-end path runs: (1) the retrieval chokepoint emits result-bearing
> retriever spans; (2) the experiment/scoring harness reads actual traces
> from Phoenix and computes note-key-anchored precision@k/recall@k; (3) the
> atomic-ops scenario set runs through that harness end-to-end and produces
> numbers. A ticket that adds the scaffolding but cannot produce a score
> from a real trace is NOT done.

> **Reworked (2026-07-23), direction change from the operator via team lead.**
> This ticket NO LONGER uses the `packages/eval` suite framework. Evals run
> against **Arize Phoenix** and **actual OTel traces** from a running Memex
> server. This **explicitly overrides `.claude/rules/eval-suites.md`** for this
> ticket — a diff that does NOT create a suite package / `register_outcome` /
> `seed_paragraphs_from_sources` is CORRECT here, not drift. Reviewers: see §5
> and the override note in §6 before flagging suite-framework absence.
>
> **Confirmed decisions (operator via team lead, 2026-07-23):**
> - **Product-code touch CONFIRMED.** The RETRIEVER-span enrichment at
>   `MemoryEngine.recall` is the necessary first deliverable (retrieval emits
>   no result-bearing span today). Its three guarantees — no-raise into the
>   search path, `results` unchanged, no-op when tracing deps absent — are
>   **hard eval rows** (marker rows 3–5), not soft goals.
> - **Q3 DECIDED: raw `httpx` against Phoenix's API** for the on-demand trace
>   fetch — **zero new dependencies**, matches the repo's existing HTTP
>   standard. `arize-phoenix-client` is the **fallback only** if Phoenix API
>   churn makes the raw calls brittle — not the default.
> - **R5 APPROVED:** pin `opentelemetry-sdk` + `openinference-semantic-conventions`
>   in the root `dev` group (a loop-gated test that passes only incidentally is
>   a real defect).
> - Q1 (recall chokepoint), Q2 (omit content by default), Q4–Q7 accepted as
>   **defaults**, flagged pending user confirmation as usual.

## 1. Title

Make Memex's retrieval pipeline **evaluable from its own traces**: enrich the
`memex_memory_search` retrieval chokepoint with OpenInference **retriever**
span attributes (query text + ranked returned unit/note IDs), then add a
**trace-based atomic-ops eval** that fires real single-fact / keyword-recall
queries at a running traced server and computes deterministic `precision@k` /
`recall@k` from the captured Phoenix traces, anchored on note_keys. This is the
E2 read atomic-ops slice of RFC #206, re-platformed onto Phoenix.

## 2. Size / Effort

**M (Medium).** Larger than the original S because it now spans two layers:
(1) a small but load-bearing **product-code** change in `memex_core` (emit a
retriever span at the recall chokepoint), and (2) a **trace-reading eval
harness** (query driver + Phoenix span fetch + note-key-anchored scorer) that
is net-new, plus its dependency handling. The retrieval-metric math and the
OTel/OpenInference plumbing already exist; the effort is the span contract, the
trace-extraction/scoring code, and splitting deterministic (loop-gated) from
live-Phoenix (on-demand) tiers cleanly.

## 3. Triggered by

RFC #206 (issue 206) E2 "Atomic Operation Benchmark" — read precision/recall
slice — plus a **direction change from the user (via team lead)**: evals must
use Arize Phoenix + actual traces, not the `packages/eval` framework. This eval
must land **before** the retrieval-touching tier-1 RFCs (#230, #155, #247,
#271) so their impact is measurable from traces — the "measurement before
optimization" evidence base for that wave. #230 and future retrieval work gate
on it.

**Supplementary operator constraint (2026-07-23):** the Phoenix-trace eval
capability does not yet exist and MUST be resolved before implementation of the
other wave tickets. This ticket is therefore the wave's **blocking
prerequisite** — it is picked up and closed FIRST; no retrieval/pipeline ticket
in the wave is `advance`d or implemented until this slug is `done`. See the
header banner for the ledger-ordering statement.

## 4. Context (today's state, cited)

### Tracing infrastructure exists; retrieval is NOT traced

- Memex exports OTel spans over OTLP/HTTP to any backend incl. Phoenix.
  Setup: `packages/core/src/memex_core/tracing.py:24` (`setup_tracing`);
  the manual-span helper `trace_span(tracer_name, span_name, attributes)`
  at `tracing.py:94` returns a real span CM or a `nullcontext` no-op when
  tracing deps are absent (`tracing.py:98-104`) — the pattern any new span
  must follow.
- Only **LiteLLM is auto-instrumented** (`tracing.py:58-59`) — DSPy/LLM calls
  (query expansion, rerank LLM) show up automatically. There is **no
  FastAPI/HTTP auto-instrumentation** and **no manual span** in the retrieval
  path. Grep for `trace_span`/`set_attribute` across `memex_core` shows spans
  in extraction (`memory/extraction/engine.py:344`), reflection
  (`memory/reflect/reflection.py:542`), contradiction
  (`memory/contradiction/engine.py:95`), cases, procedural, lint, entities —
  **but NONE in `memory/retrieval/`**. So today a `memex_memory_search` trace
  carries the session grouping + any LiteLLM child spans, but **no span records
  the query text, the returned unit IDs, or their ranks.** Enriching that is
  this ticket's first deliverable.
- Session grouping works: the HTTP middleware binds `X-Session-ID` →
  `using_session` (`server/__init__.py:342-362`, `context.py:52-53`), so
  queries fired with a known session id are findable in Phoenix's Sessions tab
  (docs `docs/how-to/observability/arize-phoenix.md:97-112`).

### The retrieval chokepoint (where the span must be emitted)

- Route: `server/retrieval.py:34` `search_memories` (POST `/memory/search`).
- Service: `services/search.py:53` `retrieve()` / `:58` `search()` — both call
  `self.memory.recall(session, request)`.
- **Chokepoint:** `memory/engine.py:291` `MemoryEngine.recall(session, request)`
  — at `:303` it calls `self.retrieval.retrieve(...)` and at `:304` holds
  `results` (the ranked `list[MemoryUnit]`). This one method has BOTH
  `request.query` and the ranked `results`, and every memory-search AND
  `survey` sub-query passes through it — the natural single place to emit a
  retriever span. (Engine internals: `RetrievalEngine.retrieve` at
  `memory/retrieval/engine.py:563`.)
- **Score caveat:** on the memory-search path, units are ranked by an internal
  `boosted` score that is discarded before return (`engine.py:2004`), so
  `getattr(unit, 'score', None)` is often `None` there (documented in the
  sibling ticket `retrieval-staleness-score-spike.md`). Therefore **rank
  (list position) is the load-bearing per-document attribute** for precision/
  recall; `document.score` is best-effort and may be null until that separate
  fix lands.

### OpenInference retriever conventions are already available

- The `[tracing]` extra (`packages/core/pyproject.toml:78-84`) pulls
  `openinference-instrumentation`, which ships `openinference.semconv.trace`.
  Verified this session: `SpanAttributes.RETRIEVAL_DOCUMENTS ==
  'retrieval.documents'`, `DocumentAttributes.DOCUMENT_ID == 'document.id'`,
  `OpenInferenceSpanKindValues.RETRIEVER.value == 'RETRIEVER'`. **No new
  dependency is needed for the span-enrichment work** — Phoenix renders a span
  with `openinference.span.kind = RETRIEVER`, `input.value = <query>`, and
  `retrieval.documents.{i}.document.id/score/content` natively.

### The gates and the test environment

- `just test` → `uv run pytest tests` (`justfile:65-66`), **root `./tests/`
  only**; `pyproject.toml:78` sets `addopts = "... -m 'not integration'"`, so
  `packages/eval/tests/` is not collected and `integration`-marked tests are
  excluded. Root `tests/conftest.py:121,134,341` gives every root test a
  session-scoped testcontainer Postgres (Docker present in this env).
- **Fragility to fix:** `opentelemetry-sdk` + `openinference-semconv` import in
  the base `just test` env **only incidentally** — they are NOT pinned in the
  root `dev` dependency group (`pyproject.toml:95-118`). A loop-gated test that
  imports them (for `InMemorySpanExporter` + the semconv constants) would pass
  here but break in a clean CI. Precedent for the fix: commit `4783bd8f`
  ("fix(ci): add respx to root dev group so core OIDC unit tests collect").

### Phoenix experimentation already started

- A `.temp/phoenix/.venv` exists in the tree (prior Phoenix spike) — informational; not a dependency of this ticket.

## 5. Non-goals / out of scope

- **Do NOT use the `packages/eval` suite framework.** No suite package under
  `suites/<name>/`, no `register_outcome` / `register_setup_action`, no
  `Suite`/`SUITE`/runner, no `seed_paragraphs_from_sources`, no `GoldUnitIds`.
  This ticket **overrides `.claude/rules/eval-suites.md`** (operator direction);
  the override is stated at the top and in §6 so the reviewer does not flag the
  absence of a suite package as drift.
- **No LLM-judged evaluation.** Do NOT use `arize-phoenix-evals`' LLM relevance
  evaluators or any LLM-as-judge. Scoring is deterministic set math
  (`precision@k` / `recall@k`) over trace-extracted IDs vs a fixed gold set.
- **No heavyweight default dependency.** Do NOT add `arize-phoenix` (the full
  server) to any default/install dependency set. Any Phoenix **client** needed
  to fetch traces goes in an opt-in eval/dev group and is flagged (see §11 Q3).
  The product-code span change adds **no** new dependency.
- **No other E2 atomic operations** (write-correctness, update-consistency,
  merge/reflection-alignment, contradiction-detection) and **no E1/E3**.
- **Entity-lookup atomic op is deferred** (see §11 Q6) — same reasoning as the
  original slice: it needs the NER/extraction pipeline and does not add read
  precision/recall signal.
- **No change to retrieval ranking or scoring behavior.** The recall chokepoint
  change is **observation only** — emit a span; do not alter `results`, order,
  filtering, or the score computation. (The null-score fix is a separate ticket.)
- **No FastAPI/HTTP auto-instrumentation** rollout, no new config knobs beyond
  what `TracingConfig` already has (`config.py:1785`).

## 6. Requirements & restrictions

**Must achieve:**

- R1 (span enrichment — product code). At the recall chokepoint
  (`memory/engine.py:291`), emit a **retriever span** carrying, via
  `openinference.semconv.trace` constants:
  - `OpenInferenceSpanKindValues.RETRIEVER` as the span kind attribute;
  - `input.value` = `request.query`;
  - per returned unit `i` (in rank order): `retrieval.documents.{i}.document.id`
    = the unit UUID, plus its `note_id` and rank recorded so the eval can
    resolve to a note_key (record note_id/rank either under the document's
    `metadata` attribute or dedicated `memex.*` attributes — implementer's
    call, but they MUST be programmatically extractable);
    `document.score` best-effort (may be null on the memory path — §4).
  The emission MUST be **guarded / no-op when tracing deps are absent**
  (mirror `tracing.py:98-104`) and MUST NOT change `results` or ranking.
- R2 (trace-based eval harness — NOT the suite framework). A standalone module
  (location §11 Q4; recommend `packages/eval/src/memex_eval/phoenix_atomic_ops/`
  that imports NOTHING from `memex_eval.suite.*`) providing:
  - a **known corpus + per-query gold sets** keyed on note_keys (a manifest
    mapping note_key → note_id for the seeded vault);
  - a **query driver** that fires each atomic-ops query at a running traced
    server over HTTP with a unique `X-Session-ID` per scenario;
  - a **trace reader + scorer** that fetches the retriever spans for those
    session ids from Phoenix, extracts the ranked `document.id`s, resolves
    unit_id → note_id → note_key against the manifest, and computes
    `precision@k` / `recall@k` vs the gold note_key set.
- R3 (deterministic scorer, note-key anchored). `precision_at_k` /
  `recall_at_k` are pure functions over `(ranked_note_keys, gold_note_keys, k)`.
  **Anchor on note_keys, never on random unit UUIDs** (the anchoring rule from
  `.claude/rules/eval-suites.md` survives the override — it is about baseline
  stability, not the suite framework). An empty/missing gold resolution is a
  loud error, not a silent `0.0`.
- R4 (two tiers, split by gate reachability):
  - **Loop-gated (root `./tests/`, no live Phoenix, no LLM, deterministic):**
    (a) a **span-contract test** using `opentelemetry.sdk`'s
    `InMemorySpanExporter` that drives the recall chokepoint in-process against
    the root testcontainer Postgres with a tiny seeded vault and asserts the
    emitted span's kind, `input.value`, and per-document id/rank ordering, and
    that `results` are unchanged vs a no-span baseline;
    (b) a **scorer** unit test (`precision@k`/`recall@k` math); (c) a
    **span-attribute extractor** unit test that parses a captured
    `retrieval.documents.{i}.*` attribute dict into ranked ids.
  - **On-demand (not loop-gated):** the full Phoenix round-trip — traced server
    + Phoenix up, fire queries, fetch spans, compute metrics, assert thresholds
    + reproducibility. Lives under `packages/eval/tests/...` or a `just` recipe;
    NOT run by `just test`.
- R5 (pin the test-env deps). Add `opentelemetry-sdk` + `openinference-semantic-conventions`
  (or `memex-core[tracing]`) to the root `dev` dependency group
  (`pyproject.toml:95`) so the loop-gated span-contract test collects in a clean
  environment — mirroring commit `4783bd8f`. Do NOT rely on their incidental
  presence.
- R6 (document the override). The eval module's `README.md` and this ticket
  state that the Phoenix/trace approach intentionally bypasses the suite
  framework per operator direction, so the codebase records why
  `.claude/rules/eval-suites.md` does not apply here.

**Restrictions (repo principles, cited):**

- `.claude/rules/eval-suites.md` — **overridden for this ticket** (operator
  direction); note-key anchoring principle retained (R3).
- `.claude/rules/python-testing.md` — every code change ships a test; tests are
  real code (ruff + mypy clean; no `skip`/`xfail`/`type: ignore`); the live-
  Phoenix test carries a marker and is excluded from the default run
  (`mark-and-exclude-slow-tests`); `dont-mock-what-you-can-run` → the span
  contract is tested with a real in-memory exporter against a real recall call,
  not a mocked span.
- `.claude/rules/pre-existing-issues.md` — the null-score-on-memory-path issue
  (§4) is pre-existing; record it, do not fix it here, and do not let the eval
  depend on a non-null `document.score`.
- `.claude/rules/adversarial-reviews.md` — adversarial review before done:
  confirm the span contract holds, the scorer discriminates, and no ranking
  behavior changed.
- `.claude/rules/slop-scan-for-docs.md` — the eval `README.md` runs the P0
  hallucination + economy/slop layers.
- CLAUDE.md style — single quotes, line length 100, ruff, strict mypy, ≥3.12,
  async I/O.

## 7. Code surface (files this ticket creates / touches)

**Product code (memex_core):**

- `packages/core/src/memex_core/memory/engine.py:291-330` — `recall()`: wrap
  the `self.retrieval.retrieve(...)` call (`:303`) in a retriever span emitting
  R1's attributes over `results`. No change to `results`/ordering.
- `packages/core/src/memex_core/tracing.py` — OPTIONAL: add a small
  `retriever_span(...)` / attribute-builder helper beside `trace_span`
  (`:94`) if it keeps `engine.py` clean; must keep the deps-absent no-op guard.
  (If a helper is added, it is the only tracing.py change.)

**Eval harness (net-new, standalone — NOT the suite framework):**

- `packages/eval/src/memex_eval/phoenix_atomic_ops/__init__.py` — NEW. Module
  root (imports nothing from `memex_eval.suite.*`).
- `.../phoenix_atomic_ops/scoring.py` — NEW. Pure `precision_at_k`/`recall_at_k`
  + the note-key resolution.
- `.../phoenix_atomic_ops/trace_extract.py` — NEW. Parse retriever-span
  attributes → ranked `(unit_id, note_id, rank)`; Phoenix span-fetch adapter
  behind a narrow interface (so the loop-gated tests exercise the parser without
  a live Phoenix).
- `.../phoenix_atomic_ops/scenarios.py` — NEW. Atomic-ops queries + gold
  note_key sets + `k` per query; the corpus manifest (note_key → seed content).
- `.../phoenix_atomic_ops/run.py` — NEW. The query driver + on-demand
  end-to-end runner (fire queries with session ids → fetch spans → score).
- `packages/eval/src/memex_eval/phoenix_atomic_ops/README.md` — NEW (R6).

**Tests:**

- `tests/test_atomic_ops_retrieval_span_contract.py` — NEW, **loop-gated** (root
  `./tests/`, NOT `integration`-marked). Span-contract test (R4a) using
  `InMemorySpanExporter` against a seeded vault on the root Postgres.
- `tests/test_atomic_ops_trace_scoring.py` — NEW, **loop-gated**. Scorer math
  (R4b) + span-attribute extractor (R4c) on synthetic fixtures. Offline.
- `packages/eval/tests/phoenix_atomic_ops/test_end_to_end.py` — NEW,
  `@pytest.mark.integration` (on-demand; needs live Phoenix + traced server).

**Deps:**

- `pyproject.toml:95` (root `dev` group) — add `opentelemetry-sdk` +
  `openinference-semantic-conventions` (R5). The Phoenix client for R2/on-demand
  goes in an opt-in group per §11 Q3 (flagged; add via `uv add`, not `pip`).

## 8. Tests & validation gates

**Gates (both must pass at close):**

- `just test` (`uv run pytest tests`, root only). The two loop-gated tests run
  and MUST pass, deterministically, with no live Phoenix and no LLM. Neither
  carries `@pytest.mark.integration`. They require otel-sdk + openinference-
  semconv in the env (R5 pins them).
- `just prek` (`uv run prek run -a`) — ruff + mypy over product code, harness,
  and tests. Fully typed; no `type: ignore`.

**On-demand (not loop-gated):**
`uv run pytest -m integration packages/eval/tests/phoenix_atomic_ops` — proves
the full Phoenix round-trip yields the expected precision/recall and is
reproducible. Because `just test` never collects `packages/eval/tests`, name
this as the on-demand deeper check.

**Named tests → homes (all listed in §7):**
- span contract (kind/input/doc-id-rank/results-unchanged) → `tests/test_atomic_ops_retrieval_span_contract.py`.
- `precision@k`/`recall@k` math + extractor parsing → `tests/test_atomic_ops_trace_scoring.py`.
- end-to-end Phoenix round-trip + reproducibility → `packages/eval/tests/phoenix_atomic_ops/test_end_to_end.py`.

## 9. Risk assessment

- **Blast radius: low but non-zero — this now touches product code.** The recall
  change is observation-only (emit a span), so a bug there could add span
  overhead or, worst case, throw inside the hot search path. Mitigation: the
  emission is wrapped in the deps-absent no-op guard AND must not raise into the
  request path (span emission failures swallowed/logged, never propagated) —
  assert this in the contract test (search still returns results even if the
  exporter is misconfigured). Reversible by removing the span block.
- **Likeliest failure modes:**
  1. **Span emission changes ranking/results** — accidentally consuming a
     generator or reordering while building attributes. Guard: contract test
     asserts `results` order/identity is unchanged vs a no-span baseline.
  2. **Reviewer flags missing suite package as drift** — mitigated by the
     override banner (§5/§6).
  3. **Eval depends on `document.score`** which is null on the memory path
     (§4). Guard: rank-based scoring only; extractor tolerates null score.
  4. **Loop-gated test needs a live Phoenix** — it must not. Guard: R4a uses
     `InMemorySpanExporter`; only the on-demand test touches Phoenix.
  5. **Incidental-dep flakiness** — otel/openinference not pinned. Guard: R5.
  6. **Heavyweight dep sneaks into default install** — pulling `arize-phoenix`.
     Guard: §5 + §11 Q3; client dep in an opt-in group only.
  7. **Non-discriminative gold/k** — every retriever scores 1.0. Guard: the
     on-demand test must show `precision@k < 1.0` is reachable by a degraded
     retriever; pick `k` near |relevant| per query.

## 10. Subtickets (ordered)

1. **Span contract first (test-first).** Write the failing span-contract test
   (`InMemorySpanExporter`, seeded vault, assert kind/input/doc-id-rank +
   results-unchanged). → verify: red for the right reason (no retriever span
   today).
2. **Emit the retriever span** at `engine.py:291` per R1 (guarded, non-raising,
   ordering-preserving). → verify: subticket-1 test green; `just test` green.
3. **Scorer + extractor** (`scoring.py`, `trace_extract.py`) + their loop-gated
   unit tests (R4b/c, note-key anchored, loud on empty gold). → verify: math
   cases pass offline.
4. **Scenarios + corpus manifest** (`scenarios.py`) — atomic-ops queries, gold
   note_key sets, `k` per query. → verify: |relevant| < |corpus|; ≥1 distractor
   plausibly retrievable per query.
5. **Query driver + on-demand runner** (`run.py`) + the integration
   end-to-end test; Phoenix client dep in an opt-in group (§11 Q3). → verify:
   `-m integration` run scores + is reproducible; a degraded retriever can drop
   precision below 1.0.
6. **Pin deps (R5)** + **README (R6)** + slop scan. → verify: clean-env collect;
   P0 hallucination pass clean.
7. **Adversarial review.** → verify: contract holds, no ranking change, scorer
   discriminates, override documented.

## 11. Open questions (forks for the operator; each carries a recommendation)

- **Q1 — Span chokepoint: `MemoryEngine.recall` vs the service layer.**
  *Recommendation:* `recall` (`engine.py:291`) — single point with query +
  ranked results, covers memory-search and survey. Note survey fires multiple
  recalls → multiple retriever spans per request (correct, but the eval must
  select the span for its own single-query session). If the operator prefers
  one span per user request, emit at `services/search.py:53/58` instead and
  accept that survey sub-queries are then unspanned.
- **Q2 — What per-document attributes to record, and content/size/privacy.**
  Recording full unit `content` on every document bloats spans and may leak
  memory content into Phoenix. *Recommendation:* record `document.id` (unit
  UUID) + `note_id` + `rank` (+ best-effort `score`); **omit content** by
  default (or truncate behind an opt-in flag). Confirm the note_id/rank carrier
  (document `metadata` vs `memex.*` attrs) during subticket 2.
- **Q3 — How the harness fetches traces from Phoenix, and the dependency.**
  Options: (a) `arize-phoenix-client` (lightweight, purpose-built) in an opt-in
  eval group; (b) raw `httpx` against Phoenix's GraphQL/REST (no new dep, more
  code); (c) `arize-phoenix` full (REJECTED — heavyweight, §5). *Recommendation:*
  **(a)** `arize-phoenix-client` in a new opt-in `phoenix-eval` group, added via
  `uv add`; fall back to (b) if the client pulls a heavy tree. Flagged per the
  operator's "don't add heavyweight deps without flagging."
- **Q4 — Harness location** (since it must NOT be a suite). *Recommendation:*
  `packages/eval/src/memex_eval/phoenix_atomic_ops/` as a standalone module that
  imports nothing from `memex_eval.suite.*`. Alternative: a top-level `evals/`
  dir if the operator wants it fully outside the eval package.
- **Q5 — Corpus seeding for the live tier: real ingest vs direct DB seed.**
  Real ingest exercises extraction→retrieval end-to-end but makes the *live*
  numbers subject to extraction variance; a direct seed is reproducible but
  bypasses extraction. *Recommendation:* **real ingest into a dedicated eval
  vault** with a committed note_key→note_id manifest; determinism lives in the
  **loop-gated** contract+scorer tier, while the live Phoenix tier is the
  measurement (trend) tier where small extraction variance is acceptable. The
  loop-gated span-contract test uses a tiny fixed seed for exactness.
- **Q6 — Entity-lookup atomic op: still deferred?** *Recommendation:* yes —
  defer (needs NER/extraction, no read-precision signal). Reconsider once the
  read slice is trending in Phoenix.
- **Q7 — Pass thresholds and `k`.** *Recommendation:* set `k` near |relevant|
  per query; pin the loop-gated contract test to structural facts (span shape),
  not a metric floor; pin the on-demand test's thresholds from observed values
  with a documented tolerance band (retrieval is live, so a tight equality would
  flake) — not a loose `≥0.5` that hides regressions.

---

**Eval marker:** `.loop/config.json` sets `require_eval: true`. The marker at
`.loop/evals/atomic-ops-retrieval-eval-suite.md` is reworked to match: it drops
the suite-package-layout rows (now invalid) and asserts the **span contract**
(retriever kind, query as `input.value`, per-document id+rank in order,
results-unchanged, no-raise, deps-absent no-op), the **note-key-anchored
scorer** math, the **trace extractor** parsing, the **loop-gate placement**, the
**dep pinning**, and that **no ranking/scoring behavior changed** and **no
heavyweight/default dep** was added — all deterministic rows. Re-validated with
`loopctl eval`.
