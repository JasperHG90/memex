# Project Nexus Suite

Tests contradiction detection, supersession ranking, and the lint pipeline
on a body of fictional Project Nexus engineering documentation.

Two shared source notes describe Project Nexus's tech stack at different
points in time (January 2025 → July 2025 migration); the newer note
supersedes specific claims from the older one. A second pair of sources
(`api-version-alpha` / `api-version-beta`) introduce contradicting API
versioning policies that exercise the surprise-gated LLM lint path.

The last two scenarios additionally use **inline notes** — per-scenario
markdown ingested only when that scenario runs — to layer a third
update (Q4 2025 CI/CD switch to CircleCI; Python 3.13 upgrade) on top
of the shared sources without polluting the earlier scenarios. This is
the canonical reference example of the inline-notes feature; copy-paste
the pattern when you want a scenario-local contradiction follow-up
that shouldn't bleed into sibling scenarios.

## Components under test

- `memory/contradiction/` — fact-vs-fact contradiction detection
- `memory/retrieval/` — supersession-aware ranking
- `services/outcomes.py` — confidence-driven re-rank
- `services/lint.py` — V1 maintenance-proposals rule pipeline
- `services/lint_llm.py` — surprise-gated LLM lint that flags semantic contradictions

## Primary metrics

- `suite.pass_rate` — deterministic pass rate
- `metric.graded_score.mean` — LLM-judge migration summary score

## Knobs

- `server.memory.retrieval.confidence_alpha` — tune supersession strength
- `server.lint.surprise_threshold` — surprise-gate cutoff for LLM lint

## Notes on ordering

Scenarios run in `SCENARIOS = [...]` order. The lint scenarios sit
between the supersession scenarios and the inline-note scenarios so the
suite-level state mutations from `consolidation_tick` do not interleave
with later inline-note ingest. The inline-note scenarios sit at the END
of the list deliberately: an inline note ingested by scenario X persists
in the suite vault for the rest of the run (vault cleanup is suite-end),
so an early CircleCI-or-3.13 inline note would contaminate the
pre-existing GitHub-Actions / Python-3.12 assertions.

The lint scenarios mark `expected_failure_modes=['claude-code', 'hermes']`
because the lint outcomes inspect server-side findings, which agent
backends cannot reproduce from text-only tool output.

## Recent baseline

Single replicate, fresh DB + freshly-built snapshot, 2026-05-16:

| Pass rate | Failed | Cost |
|---|---|---|
| **1.0000** (11/11) | — | $0.0005 (judge only — api backend, no agent cost) |
