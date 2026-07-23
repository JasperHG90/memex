eval: atomic-ops-retrieval-eval-suite

**Reworked (2026-07-23):** the approach changed from the `packages/eval` suite framework
to **Arize Phoenix + actual OTel traces** (operator direction, which explicitly overrides
`.claude/rules/eval-suites.md` for this ticket). The prior suite-package-layout rows are
DELETED as invalid.

**Definition of Done:** Memex's retrieval chokepoint emits an OpenInference **retriever**
span (query as `input.value`, ranked returned unit/note IDs as
`retrieval.documents.{i}.*`) without changing retrieval results or ranking and without
breaking installs that lack the tracing extra; a **trace-based** atomic-ops eval computes
deterministic, note-key-anchored `precision@k` / `recall@k` from the captured spans
(NO LLM judge); the deterministic span-contract + scorer + extractor tests are loop-gated
under root `./tests/`, while the live-Phoenix round-trip is on-demand; and no heavyweight
Phoenix dependency lands in any default install.

Scoring policy: every row is a deterministic assertion (in-memory span inspection, pure
scorer/parse math, path/marker/dep inspection, or a reproducible live-Phoenix run) at a
hard 100% bar. Rows 1, 2, 3, 7, 8, and 9 are the load-bearing guardrails (span kind +
query, ranked doc ids, results-unchanged, note-key anchoring, loop-gate placement, no
heavyweight default dep) — a violation of any defeats the slice, so they pass 100%.

Fork-dependent rows are marked, written against the planner's recommended resolution;
re-pin if the operator decides otherwise:
- Q1 (span at `MemoryEngine.recall`) → rows 1–3 assume the span is emitted at the recall
  chokepoint (covers memory-search + survey sub-queries).
- Q2 (attributes: id+note_id+rank, content omitted) → row 2 asserts id+rank present; does
  NOT require `document.content` or a non-null `document.score`.
- Q3 (Phoenix client in an opt-in group) → row 9 asserts no `arize-phoenix` in any default
  dependency set; the client, if added, is opt-in.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL — span kind + query]** A memory search emits a retriever span carrying the query *(Q1)* | Drive the recall chokepoint in-process (seeded vault) with `InMemorySpanExporter` attached; query `Q` | Exactly one retriever span exists whose OpenInference span-kind attribute == `RETRIEVER` and whose `input.value` == `Q` | Deterministic (in-memory span inspection): assert kind == `RETRIEVER` AND `input.value == Q` | 100% |
| **[GUARDRAIL — ranked doc ids]** The span records the returned unit IDs in rank order *(Q1/Q2)* | Same recall call returning units `[u0, u1, u2]` in that ranked order | Span carries `retrieval.documents.0/1/2.document.id == u0/u1/u2` (in order) plus each document's `note_id` and `rank`; `document.content` NOT required; null `document.score` tolerated | Deterministic: assert the parsed ranked `document.id` list equals the recall result order AND note_id+rank are present per document | 100% |
| **[GUARDRAIL — no behavior change]** Emitting the span does not alter retrieval results or ranking | Run the same query twice: once with the exporter attached, once with tracing deps absent (no-op path) | The returned `results` (identity and order) are identical in both runs; the span path adds no reordering, filtering, or mutation | Deterministic: assert `results` equal between spanned and no-span runs | 100% |
| Span emission never raises into the request path | Recall call where the span exporter/attribute set is forced to fail | The search still returns its normal `results`; the failure is swallowed/logged, not propagated | Deterministic: assert results returned AND no exception surfaced to the caller | 100% |
| Tracing deps absent → span emission is a silent no-op | Recall call in an environment without the tracing deps (mirror `tracing.py:98-104`) | No span emitted, no import error, results returned normally | Deterministic: assert no exception AND results returned AND no span recorded | 100% |
| `precision@k` / `recall@k` math is correct (note-key anchored) | Pure scorer on `(ranked_note_keys, gold_note_keys, k)`: perfect, half-noise, miss, empty-retrieved | perfect → precision=recall=1.0; 2 relevant of top-4 → precision=0.5; zero relevant in top-k → precision=recall=0.0; empty retrieved → 0.0 (no ZeroDivision) | Deterministic (pure unit test) | 100% |
| **[GUARDRAIL — note-key anchoring]** The scorer resolves ranked unit IDs → note_keys, never pins random unit UUIDs | Ranked span `document.id`s + a manifest `{note_id: note_key}` + `{unit_id: note_id}`; gold set is note_keys | Metrics are computed against note_keys; an empty/missing gold resolution raises loudly (not silent 0.0) | Deterministic (unit test): assert metrics match hand-computed note-key values AND the empty-gold case raises | 100% |
| The span-attribute extractor parses flattened `retrieval.documents.{i}.*` correctly | A captured span-attributes dict fixture (out-of-order keys, gaps) | Returns the ranked `(unit_id, note_id, rank)` list in ascending rank order; tolerant of missing `score` | Deterministic (pure unit test) | 100% |
| **[GUARDRAIL — loop-gate placement]** The deterministic span-contract, scorer, and extractor tests are collected by `just test` | The test files for rows 1–8 | They live under root `./tests/` (so `uv run pytest tests` collects them) and carry NO `@pytest.mark.integration`; the live-Phoenix round-trip test is separate and integration-marked | Deterministic (path + marker inspection): assert loop-gated files under `tests/` with no integration marker AND the live test carries the marker | 100% |
| **[GUARDRAIL — no heavyweight default dep]** No `arize-phoenix` server lands in a default install; span work adds no dep *(Q3)* | The ticket diff's dependency changes | `arize-phoenix` (full server) appears in NO default/install dependency set; any Phoenix client is in an opt-in group; the product-code span change adds no new runtime dependency; root `dev` group gains only otel-sdk + openinference-semconv (R5) | Deterministic (dependency-manifest inspection) | 100% |
| Live Phoenix round-trip scores and is reproducible *(on-demand; Q5/Q7)* | `uv run pytest -m integration packages/eval/tests/phoenix_atomic_ops` (traced server + Phoenix up, seeded eval vault) | Real queries fired with known session ids; spans fetched from Phoenix; per-query precision@k/recall@k clear their pinned thresholds within the documented tolerance; two runs agree; a degraded retriever can drop precision below 1.0 (gate is not trivially satisfiable) | Deterministic integration run: assert thresholds met AND runs agree AND sub-1.0 precision is reachable | 100% |
