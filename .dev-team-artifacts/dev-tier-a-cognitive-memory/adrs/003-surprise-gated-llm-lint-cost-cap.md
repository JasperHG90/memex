# ADR-003: Surprise-Gated LLM Lint With 24h Count Cap and Defer-Not-Drop

## Status

Accepted

## Context

F10 introduces an LLM-based lint pass over newly-extracted memory units (DSPy-driven contradiction and quality checks). Running an LLM on every unit is prohibitively expensive and mostly wasted: most extracted units are mundane and will not surface a useful lint signal. We need a gate that invokes the LLM only when there is likely something interesting to find, and a cost cap that bounds spend per vault even under burst load.

Two failure modes must be avoided: (1) silent over-spend when an upstream component floods the queue, and (2) silent data loss when the cap fires — a unit that misses its lint window today should still be evaluable tomorrow.

## Decision

F10 LLM lint is gated by an anisotropy-corrected surprise score with threshold 0.7. The score uses cheap embedding distances against the per-vault corpus distribution; only high-surprise units pass the gate.

A 24h rolling cost cap is enforced via the `LintLLMQuota` table with hour-bucket UPSERT keyed on `uq_lint_llm_quota_vault_hour`. The cap is stored as an **integer count of LLM calls**, not cents — billing model conversions are deferred to the metrics layer where pricing changes do not require a schema migration. Both DSPy checks (contradiction lint and quality lint) share the same per-vault cap.

When the cap is exceeded, units are **deferred, not dropped**: a `MaintenanceProposal` row is queued with `rule_name='llm_deferred'`, and the consolidation tick re-evaluates them once budget reopens.

Implemented in `packages/core/src/memex_core/memory/lint_llm/`. Specified in RFC-006. Schema in alembic 029.

## Consequences

**Positive:**
- LLM is invoked only on surprising units — embeddings preempt expensive calls for the long tail.
- Hour-bucket UPSERT is concurrent-writer-safe under the single-leader scheduler; multiple workers cannot exceed the cap.
- Defer-not-drop preserves work across cap boundaries — no permanent loss of lint signal.

**Negative:**
- Two-stage decision (surprise + cap) makes the lint pipeline harder to reason about during incident response. Mitigated by structured logs at each gate.
- Count-based cap is approximate in cost terms — a future schema migration is needed if per-call costs become highly variable across model tiers.
- Deferral queue can grow unbounded under sustained over-budget load; monitored via Prometheus.

## Alternatives Considered

- **Cents-based cap** — rejected: couples schema to billing model; provider price changes force migrations.
- **Drop on cap** — rejected: silently loses lint signal; violates the "no quiet data loss" invariant.
- **Per-call cost cap (no surprise gate)** — rejected: still spends LLM cycles on uninteresting units even within budget.
- **Sliding-window Redis counter** — rejected: introduces a new infra dependency; Postgres UPSERT is sufficient given the scheduler is single-leader.
