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

### External benchmarks (LoCoMo)

The LoCoMo benchmark evaluates Memex's ability to answer questions about multi-session conversations. It uses a three-phase pipeline: export, answer, judge.

```bash
# Phase 1: Export questions from the LoCoMo dataset to JSONL
memex-eval locomo export --dataset-path ./data/locomo/ --output questions.jsonl

# Phase 2: Answer questions using an agent CLI (e.g. Claude Code)
memex-eval locomo answer --questions questions.jsonl --output answers.jsonl

# Phase 3: Judge answers with LLM-as-a-judge and produce a report
memex-eval locomo judge --questions questions.jsonl --answers answers.jsonl --output results.json

# Phase 4: Generate evaluation report with plots
memex-eval locomo-report --results results.json --answers answers.jsonl --traces-dir traces/ --output-dir report/

# Limit questions for quick smoke test
memex-eval locomo export --dataset-path ./data/locomo/ --limit 20
```

The report command generates a Markdown report with seaborn distribution plots, mermaid retrieval path diagrams, per-question details, and efficiency analysis. See the [full evaluation report](../../docs/reference/evaluation-report.md) for the latest results.

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

## LoCoMo benchmark methodology

### Dataset

[LoCoMo](https://arxiv.org/abs/2402.17753) (Long Conversation Memory) is an academic benchmark for evaluating long-term memory in conversational AI. Each sample contains multi-session dialogues between two people (19 sessions spanning several months), along with ground-truth QA pairs across five question categories:

| Category | Tests | Example |
|---|---|---|
| **Single-Hop** | Direct fact recall from a single conversation turn | "What is Caroline's relationship status?" |
| **Multi-Hop** | Reasoning across multiple turns to derive a date or fact | "When did Caroline go biking with friends?" |
| **Temporal** | Time-aware recall — what happened when, in what order | "What does Caroline's necklace symbolize?" |
| **Open Domain** | Inference requiring world knowledge combined with stored facts | "Would Caroline likely have Dr. Seuss books?" |
| **Adversarial** | Questions with deliberately swapped subjects or false premises | "What instrument does Caroline play?" (actually Melanie's) |

### Pipeline

The benchmark runs in three decoupled phases, each resumable:

1. **Export** (`locomo_export.py`): Loads the LoCoMo dataset and writes questions to JSONL. For adversarial questions, the `adversarial_answer` field is used as the expected answer rather than the standard `answer`.

2. **Answer** (`locomo_answer.py`): Feeds each question to an agent CLI (currently Claude Code) with access to Memex MCP tools. The agent operates against a pre-ingested vault containing all conversation sessions. Each question runs in an isolated temp directory with a minimal `CLAUDE.md` instructing the agent to search the vault. Captures: answer text, tool call sequence, token usage, duration, and cost.

3. **Judge** (`locomo_judge.py`): An LLM judge (Gemini 3 Flash via dspy) grades each answer on a 5-point scale (0.0 / 0.25 / 0.5 / 0.75 / 1.0) by comparing the model response against the expected answer. The judge also analyzes tool call patterns (memory search, note search, two-speed verification).

### Scoring

- **1.0 (Perfect)**: Answer fully matches the expected answer, or correctly identifies and corrects an adversarial premise
- **0.75 (Mostly correct)**: Core answer is right with minor omissions
- **0.5 (Partial)**: Some correct information but significant gaps
- **0.25 (Minimal)**: Only tangentially relevant
- **0.0 (Wrong)**: Incorrect or missing answer

### Known limitations

- **Image-dependent questions**: Some LoCoMo questions reference images shared in conversations (book covers, photos of signs, pottery). The Memex pipeline extracts text from conversations but does not process shared images, so questions whose answers depend solely on image content cannot be answered from stored memories alone.
- **Adversarial scoring**: Adversarial questions deliberately swap subjects (e.g., asking about Melanie's instruments when they are Caroline's). The judge must recognize that correcting the false premise is a correct response, not an error.

## Results

> Single run on LoCoMo conversation 0. Scores include manual review corrections where the automated judge was inconsistent.

### Configuration

- **Model (answering)**: Claude Opus 4 via Claude Code CLI
- **Model (judging)**: Gemini 3 Flash
- **Dataset**: LoCoMo conversation 0 (19 sessions, 60 QA pairs, 3 excluded as image-dependent)
- **Vault**: Pre-ingested with full extraction + reflection pipeline

### Scores by category

| Category | Count | Mean Score | Perfect | Wrong |
|---|---|---|---|---|
| Single-Hop | 10 | 0.900 | 9 | 1 |
| Multi-Hop | 14 | 1.000 | 14 | 0 |
| Open Domain | 3 | 1.000 | 3 | 0 |
| Temporal | 18 | 1.000 | 18 | 0 |
| **Non-adversarial** | **45** | **0.978** | **44** | **1** |
| Adversarial (unweighted) | 12 | 0.667 | 8 | 4 |

Adversarial scores are reported separately and excluded from the weighted overall score. See the [full evaluation report](../../docs/reference/evaluation-report.md) for the rationale, retrieval efficiency analysis, per-question details, and distribution plots.

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
    locomo_common.py      # Shared constants, dataset loading, JSONL helpers
    locomo_export.py      # Phase 1: export questions to JSONL
    locomo_answer.py      # Phase 2: answer via agent CLI (Claude Code)
    locomo_judge.py       # Phase 3: grade answers + produce report
    locomo_efficiency.py  # Efficiency analysis: latency, tokens, tool usage
    locomo_report.py      # Phase 4: generate Markdown report with plots
```

## Adding scenarios (internal)

1. Define a `SyntheticDoc` in `scenarios.py` with content containing specific checkable facts
2. Define `GroundTruthCheck` entries with queries and expected results
3. Group them in a `ScenarioGroup` and add to `ALL_GROUPS`
4. Run `memex-eval run --group <name>` to test
