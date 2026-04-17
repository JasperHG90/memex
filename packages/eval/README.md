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

### External benchmark — LongMemEval

[LongMemEval](https://github.com/xiaowu0162/longmemeval) is the canonical external scoreboard. 500 questions across six cognitive-ability categories (single-session-user/assistant/preference, temporal-reasoning, knowledge-update, multi-session) plus `_abs` abstention variants. LLM-as-judge per the paper. Three variants: `oracle` (ground-truth evidence only — pure answering), `s` (~40 sessions, ~115k tokens), `m` (~500 sessions).

Dataset files are not vendored; download from upstream and pass the directory or JSON path.

```bash
# Phase 0: Ingest sessions into a per-run vault
memex-eval longmemeval ingest --dataset-path ./data/longmemeval/ --variant oracle --run-id smoke

# Phase 2: Retrieve + answer per question
memex-eval longmemeval answer --dataset-path ./data/longmemeval/ --variant oracle --run-id smoke --output hypotheses.jsonl

# Phase 3: Judge hypotheses (with optional cache for offline runs)
memex-eval longmemeval judge --dataset-path ./data/longmemeval/ --variant oracle --hypotheses hypotheses.jsonl --output judgments.jsonl

# Phase 4: Aggregate into report
memex-eval longmemeval report --judgments judgments.jsonl --variant oracle --output-dir report/

# End-to-end shortcut
memex-eval longmemeval run --dataset-path ./data/longmemeval/ --variant oracle --questions 20
```

Reports include overall accuracy, per-category breakdown, abstention precision/recall, and a pinned dataset SHA-256 for provenance.

## Scenario groups (internal)

| Group | What it tests | Docs | Checks |
|---|---|---|---|
| `basic_extraction` | Fact extraction, keyword/semantic search, entity linking | 3 | 6 |
| `contradiction` | Conflicting facts detected, supersession ranking | 2 | 5 |
| `entity_resolution` | Name variants resolve to same entity, graph co-occurrence | 2 | 4 |
| `reflection` | Mental models synthesized with correct evidence | 0 | 1 |
| `temporal` | Time filtering, recency-aware ranking | 2 | 3 |

## Check types (internal)

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
    runner.py         # Orchestration: ingest -> wait -> check -> report
  external/
    longmemeval_common.py   # Shared types, dataset loaders, JSONL helpers
    longmemeval_ingest.py   # Phase 0: session-to-note adapter + vault setup
    longmemeval_answer.py   # Phase 2: retrieve + LM answering
    longmemeval_judge.py    # Phase 3: LLM-as-judge with cache support
    longmemeval_report.py   # Phase 4: per-category accuracy + Markdown report
```

## Adding scenarios (internal)

1. Define a `SyntheticDoc` in `scenarios.py` with content containing specific checkable facts
2. Define `GroundTruthCheck` entries with queries and expected results
3. Group them in a `ScenarioGroup` and add to `ALL_GROUPS`
4. Run `memex-eval run --group <name>` to test
