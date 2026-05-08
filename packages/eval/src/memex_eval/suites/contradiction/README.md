# Contradiction Suite

Tests contradiction detection and supersession ranking. Two notes
describe Project Nexus's tech stack at different points in time
(January 2025 → July 2025 migration). The newer note supersedes
specific claims from the older one.

## Components under test

- `memory/contradiction/` — fact-vs-fact contradiction detection
- `memory/retrieval/` — supersession-aware ranking
- `services/outcomes.py` — confidence-driven re-rank

## Primary metrics

- `suite.pass_rate` — deterministic pass rate
- `metric.graded_score.mean` — LLM-judge migration summary score

## Knobs

- `server.memory.retrieval.confidence_alpha` — tune supersession strength
