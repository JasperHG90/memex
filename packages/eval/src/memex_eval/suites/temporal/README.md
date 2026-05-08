# Temporal Suite

Tests recency-aware ranking. Two notes about Acme Corp's engineering
team headcount — one from 2023 (20 engineers) and one from 2025 (45
engineers) — verify that the newer fact ranks higher under default
recency boost.

## Components under test

- `retrieval/engine.py` — `recency_boost` factor on cross-encoder rerank
- `retrieval/temporal_filter` — time-based filtering

## Knobs

- `server.memory.retrieval.reranking_recency_alpha`
- `server.memory.retrieval.reranking_temporal_alpha`
