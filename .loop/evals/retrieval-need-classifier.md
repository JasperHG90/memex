eval: retrieval-need-classifier

**Definition of Done:** a fast, **fail-open**, non-LLM retrieval-need classifier
(`memory/retrieval/classifier.py`) gates `RetrievalEngine.retrieve` (`engine.py:563`):
when `RetrievalConfig.classifier_enabled=True` and the heuristic classifies a query as
needing no memory, `retrieve` returns `([], None)` before any embedding/strategy cost;
otherwise the full pipeline runs unchanged. Global default is OFF (shipped path
byte-identical to today). A per-request `force_retrieval` override and an MCP
`memex_memory_search` param bypass the gate. Every decision is audited (log record +
`RETRIEVAL_CLASSIFIER_DECISION_TOTAL` counter + debug dict). **Phase 1 of RFC #230 ONLY**
— no reasoning/planning phase, no restructuring phase, no strategy-family hint.

Acceptance is tied to the **#206 atomic-ops retrieval eval (planned separately, per the
reworked `atomic-ops-retrieval-eval-suite` ticket)** — an **Arize Phoenix trace-based**
eval measuring recall against actual retrieval traces, NOT a `packages/eval` / `memex_eval`
suite: the classifier, when enabled, MUST NOT reduce recall on scenarios that DO need
memory. Until that eval lands, rows 1, 2 and 5 below are the standing proxy for that
criterion — plain pytest loop guardrails in root `tests/`, not eval-package usage — and are
hard at 100%.

Scope decision (team lead, 2026-07-23): classifier phase only; goal-reasoning/planning
phase stays out until #206 E2 shows it pays its latency. Classifier is a heuristic, not
an LLM call (RFC #230 Phase 1; avoids the 200-500ms + circuit-breaker coupling of
`run_dspy_operation`).

Scoring policy: deterministic assertions on the search result + emitted audit signal at
a hard 100% bar. Rows 1, 2, 5 are guardrails (default unchanged; recall preserved;
fail-open) and must pass 100%.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Default path byte-identical | `memory_search("query")` with `classifier_enabled` at its default (`False`) over a seeded vault | Returned unit set and serialized shape identical to the pre-change baseline; the gate never runs | Deterministic: assert result equals captured pre-change baseline AND the classifier-decision counter did not increment | 100% |
| **[GUARDRAIL]** Recall preserved when ON for a needs-memory query | `classifier_enabled=True`; a query referencing seeded vault content (proxy for a #206 E2 needs-memory scenario) | Same unit set as the classifier-OFF run — the gate does NOT drop it | Deterministic: assert unit-id set equals the classifier-OFF unit-id set for the same query | 100% |
| Short-circuit fires for a clear no-memory query | `classifier_enabled=True`; a query the heuristic classes as no-retrieval-needed (e.g. a bare greeting) | `[]` returned WITHOUT running embedding or strategies | Deterministic: assert result is empty AND (call-count spy) the embedding/strategy step was not invoked | 100% |
| Per-request override wins over a skip verdict | `classifier_enabled=True`, `force_retrieval=True`, on a query that would otherwise be skipped | Full pipeline runs; results returned | Deterministic: assert non-empty result AND the strategy step was invoked | 100% |
| **[GUARDRAIL]** Fail-open on ambiguity | `classifier_enabled=True`; an ambiguous query that is NOT a high-precision skip signal | Retrieval runs (not short-circuited) | Deterministic: assert the strategy step was invoked AND decision label is `retrieve` | 100% |
| Decision is audited | Any gated `memory_search` with `classifier_enabled=True` | The decision (`retrieve`/`skip`) + reason is emitted | Deterministic: assert `RETRIEVAL_CLASSIFIER_DECISION_TOTAL` increments with the expected `decision`/`reason` labels (or the debug dict carries the decision when `request.debug=True`) | 100% |
| **[GUARDRAIL]** Heuristic is non-LLM / no circuit-breaker coupling | Unit-level: invoke the classifier directly | Classifier is pure/synchronous — no call to `run_dspy_operation`, no DB/embedding I/O | Deterministic: assert (call-count spy) `run_dspy_operation` is never called on the classifier path | 100% |
| Ambiguity battery resolves to retrieve | Unit-level: parametrized queries referencing past events / entities / preferences / "we/last/remember"-shaped language | Every case → `need_retrieval=True` | Deterministic: parametrized assert `decision == retrieve` for each | 100% |
