# memex-eval

Quality benchmarks for the Memex memory system. Measures extraction, retrieval, contradiction detection, entity resolution, reflection, and temporal reasoning using synthetic ground-truth data and LLM-as-a-judge evaluation.

## Setup

The package is a workspace member — install via `uv sync` from the repo root.

Requires a running Memex server (default: `http://localhost:8001/api/v1/`).

For LLM-judged checks, set `GOOGLE_API_KEY` in your environment (uses Gemini via dspy).

## Usage

### Internal benchmark

Ingests synthetic documents with planted facts into a dedicated vault, then runs queries and verifies results.

```bash
# Full benchmark (deterministic checks only)
memex-eval run --no-llm-judge

# Full benchmark with LLM-as-a-judge
memex-eval run

# Single scenario group
memex-eval run --group contradiction

# Custom server URL
memex-eval run --server http://localhost:9000/api/v1/

# Export results to JSON
memex-eval run --output results.json

# Verbose logging
memex-eval run -v
```

Or via justfile:

```bash
just benchmark-internal       # full benchmark
just benchmark-internal-fast  # deterministic only (no LLM judge)
```

### External benchmarks

```bash
# LongMemEval (500 questions across 5 categories)
memex-eval longmemeval --dataset-path ./data/longmemeval/

# LoCoMo (50 multi-session conversations)
memex-eval locomo --dataset-path ./data/locomo/

# Limit questions for quick smoke test
memex-eval longmemeval --dataset-path ./data/longmemeval/ --limit 20
```

## Scenario groups

| Group | What it tests | Docs | Checks |
|---|---|---|---|
| `basic_extraction` | Fact extraction, keyword/semantic search, entity linking | 3 | 6 |
| `contradiction` | Conflicting facts detected, supersession ranking | 2 | 5 |
| `entity_resolution` | Name variants resolve to same entity, graph co-occurrence | 2 | 4 |
| `reflection` | Mental models synthesized with correct evidence | 0 | 1 |
| `temporal` | Time filtering, recency-aware ranking | 2 | 3 |

## Check types

- **`keyword_in_results`** — deterministic: expected keywords appear in top-K results
- **`entity_exists`** — deterministic: named entity exists in the knowledge graph
- **`result_ordering`** — deterministic: results appear in expected rank order
- **`llm_judge`** — LLM-judged: Gemini evaluates whether the result correctly answers the query given ground truth (skipped with `--no-llm-judge`)

## Architecture

```
memex_eval/
  cli.py              # Typer CLI entry point
  judge.py            # dspy LLM-as-a-judge (Gemini)
  metrics.py          # CheckResult, GroupResult, BenchmarkResult
  report.py           # Rich terminal tables + JSON export
  internal/
    scenarios.py      # Synthetic docs + ground-truth definitions
    checks.py         # Check dispatcher (deterministic + LLM)
    runner.py         # Orchestration: ingest → wait → check → report
  external/
    longmemeval.py    # LongMemEval benchmark runner
    locomo.py         # LoCoMo benchmark runner
```

## Adding scenarios

1. Define a `SyntheticDoc` in `scenarios.py` with content containing specific checkable facts
2. Define `GroundTruthCheck` entries with queries and expected results
3. Group them in a `ScenarioGroup` and add to `ALL_GROUPS`
4. Run `memex-eval run --group <name>` to test
