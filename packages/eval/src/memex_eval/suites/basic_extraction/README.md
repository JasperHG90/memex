# Basic Extraction Suite

## What this tests

Three notes about Project Alpha (kickoff + Phase 1 update) and Project Beta (overview)
at Acme Corp are ingested into a temporary vault. The suite verifies:

- Fact extraction produces at least one memory unit per note
- Keyword and semantic retrieval surface the expected docs in top-K
- Recall@5 / MRR for the Alpha-related queries

## Why

This is the smoke-test for the extraction → indexing → retrieval pipeline.
Regressions here block all other meaningful evaluation; if `basic_extraction`
fails, every more-specialized suite is suspect.

## Components under test

- `memory/extraction/` — DSPy `ExtractSemanticFacts` signature
- `memory/retrieval/` — keyword + semantic + RRF + cross-encoder
- `memory/entity_resolver.py` — name-variant resolution

## Primary metrics

- `suite.pass_rate` — fraction of deterministic checks passing
- `metric.recall_at_5.mean` — Recall@5 averaged over Gold-Unit-IDs scenarios
- `metric.mrr.mean` — Mean Reciprocal Rank of the first relevant unit

## Default answer backend

`api` — direct `RemoteMemexAPI` calls (no agent in the loop). Override
via `Scenario.answer_mode='claude-code'` or `'hermes'` to test the same
content under those agents.
