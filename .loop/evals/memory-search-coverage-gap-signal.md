eval: memory-search-coverage-gap-signal

**Definition of Done:** an opt-in `detect_gaps` parameter on memory search runs ONE
post-retrieval LLM coverage call (only when set), returns a `coverage_score`, and — below
the `0.3` threshold — emits an auditable `knowledge_gap` event and surfaces the score plus a
gap hint to the calling agent. The default (`detect_gaps=False`) path is byte-identical, makes
zero coverage LLM calls, and adds zero latency.

**Surfacing decision (Q1 = A, envelope-on-flag; Q2 = compute/emit at the HTTP endpoint) —
accepted recommendation, PENDING FINAL USER CONFIRMATION.** On the `detect_gaps=True` path the
endpoint returns a JSON envelope `{coverage_score, gap_description, units:[...]}` instead of the
NDJSON stream; the client parses it on that path only; the MCP tool reads the score and, below
threshold, injects a gap system-hint `McpFact` (reusing the degraded-warning precedent at
`server.py:1809-1824`). The default path stays a bare NDJSON stream. If the user overturns Q1,
rows 3, 5, and 7 change and this marker is re-authored.

**Scope guard:** G1 only. No G2 (auto-research pipeline), no G3 (`memory_create_from_research`),
no `source_type` column, no automated action triggered by the score.

Scoring policy: deterministic assertions on the search result / tool response and on the
emitted audit event + metrics. Guardrail rows (1, 2, 3) protect invariants and must pass 100%.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| **[GUARDRAIL]** Default path is untouched and makes no coverage LLM call | `memory_search("when did we rotate prod DB creds?")` with `detect_gaps` omitted (default `False`), over a populated vault | Response is byte-identical to the pre-change output (bare list, no `coverage_score`/`gap_description` anywhere); the coverage predictor is invoked **zero** times | Deterministic: assert serialized result equals the pre-change baseline AND a call-count spy on `run_dspy_operation`/the coverage predictor == 0 | 100% |
| **[GUARDRAIL]** Fail-open — a coverage-call failure never sinks the search | `detect_gaps=True` where the coverage call raises `CircuitBreakerOpen` (and, separately, `asyncio.TimeoutError`) | The search returns its retrieved units exactly as with `detect_gaps=False`; `coverage_score` is `None`; **no** `knowledge_gap` event is emitted; a WARNING is logged | Deterministic: assert units unchanged AND `coverage_score is None` AND no `audit_event('knowledge_gap', …)` fired (mock the audit service; `assert calls == []`) | 100% |
| **[GUARDRAIL]** Zero-results still carries the signal (the per-unit channel cannot) | `detect_gaps=True` for a query that retrieves **zero** units, mocked coverage score `0.05` | The caller still receives `coverage_score=0.05` and a gap signal via the envelope (NOT dropped as it would be on a per-unit DTO); a `knowledge_gap` event fires | Deterministic: assert the returned envelope carries `coverage_score` AND the MCP caller receives a gap system-hint despite zero real units | 100% |
| Below-threshold emits exactly one auditable `knowledge_gap` event | `detect_gaps=True`, mocked coverage score `0.12` (< `0.3`), 2 results | Exactly one `audit_event('knowledge_gap', resource_type='search', …)` fires carrying `query`, `coverage_score=0.12`, `threshold=0.3`, and `result_count=2` | Deterministic: assert the audit service received exactly one `knowledge_gap` call with those detail keys/values | 100% |
| Below-threshold surfaces the score AND a gap hint to the MCP caller | `memex_memory_search(query, detect_gaps=True)`, mocked coverage score `0.12` | The tool response exposes `coverage_score=0.12` AND includes an injected `McpFact` tagged `system-hint` whose text names the gap (`gap_description`) | Deterministic: assert `coverage_score` present AND a result element has `'system-hint'` in `tags` and non-empty gap text | 100% |
| Above-threshold is quiet but still returns the score | `detect_gaps=True`, mocked coverage score `0.82` (>= `0.3`) | `coverage_score=0.82` is returned to the caller; **no** `knowledge_gap` event; **no** injected system-hint `McpFact` | Deterministic: assert `coverage_score == 0.82` AND no `knowledge_gap` audit call AND no `system-hint` element added by the coverage path | 100% |
| Envelope-on-flag shape (Q1=A) replaces NDJSON only on the opt-in path | HTTP `POST /memories/search` with `detect_gaps=True`, mocked coverage score `0.4` | The endpoint returns a single JSON object `{coverage_score, gap_description, units:[…]}` (not an NDJSON stream); the client deserializes it into results + a search-level `coverage_score`; the `detect_gaps=False` request to the same endpoint still returns an NDJSON stream | Deterministic: assert the flagged response is one JSON object with those keys AND the default response is still NDJSON (content-type / shape) | 100% |
| Coverage below threshold records the gap metric | `detect_gaps=True`, mocked coverage score `0.12` | `KNOWLEDGE_GAP_TOTAL` counter increments by 1 and `COVERAGE_SCORE` histogram observes `0.12` | Deterministic: assert the Prometheus counter delta == 1 AND the histogram recorded one sample near `0.12` | 100% |
