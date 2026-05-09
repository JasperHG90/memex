# Contradiction Suite

Tests contradiction detection and supersession ranking. Two shared
source notes describe Project Nexus's tech stack at different points in
time (January 2025 → July 2025 migration). The newer note supersedes
specific claims from the older one.

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

## Primary metrics

- `suite.pass_rate` — deterministic pass rate
- `metric.graded_score.mean` — LLM-judge migration summary score

## Knobs

- `server.memory.retrieval.confidence_alpha` — tune supersession strength

## Notes on ordering

Scenarios run in `SCENARIOS = [...]` order. The inline-note scenarios
sit at the END of the list deliberately: an inline note ingested by
scenario X persists in the suite vault for the rest of the run (vault
cleanup is suite-end), so an early CircleCI-or-3.13 inline note would
contaminate the pre-existing GitHub-Actions / Python-3.12 assertions.
