# Evaluation Suite Guide

How to author, run, sweep, and track Memex evaluation suites with full MLflow reproducibility.

> **Scope.** This guide covers the **internal** evaluation framework (`memex-eval suite …`). The external benchmarks (LoCoMo, LongMemEval) keep their existing CLIs and are not covered here.

## Mental model

A **Suite** is the unit of evaluation. It bundles:

| Component | What it is | Lives in |
|---|---|---|
| **Sources** | Markdown notes (+ optional binary assets) ingested into a temporary vault | `sources/*.md` and `sources/assets/` |
| **Scenarios** | Typed Python objects describing one verifiable assertion each | `__init__.py` (the `SCENARIOS` list) |
| **Metadata** | Suite name/version/tags/primary_metrics/components/knobs | `__init__.py` (the `METADATA` literal) |
| **README** | Scientific description (what + why + what's tested) | `README.md` (plain markdown) |

Every suite run produces:

- One **MLflow run** under experiment `memex-suite-<name>-v<schema_version>`
- Stable params (suite version, git SHA, knob values, sources hash)
- Stable metrics (`metric.<key>.{mean,std,min,max}`, `latency_ms.{p50,p95}`, `pass_rate`)
- Artifacts (`run_result.json`, frozen `README.md` + `sources/` snapshot, `config_snapshot.json`)

A **sweep** is N runs of the same suite at different knob values, all under one MLflow experiment with a parent/child structure.

---

## Quick start — running suites

### Prerequisites

- Memex server running locally (default `http://localhost:8000/api/v1/`)
- `GOOGLE_API_KEY` in your env if any suite uses LLM-judge outcomes
- Postgres reachable (testcontainer or docker-compose) if you'll be sweeping

### One-line run

```bash
# Run the default reference suite against a local server, log to ./mlruns
memex-eval suite run basic_extraction
```

That's it. With no flags, the runner uses sensible defaults (`--server http://localhost:8000/api/v1/`, `--mlflow-uri file://./mlruns`, `--mlflow-experiment memex-suite-basic_extraction-v1`, `--replicates 1`).

### Inspect what's available before running

```bash
memex-eval suite list                        # all discoverable suites with version + tags + primary metrics
memex-eval suite show basic_extraction       # render the suite's README + scenarios summary
memex-eval suite validate basic_extraction   # check the suite loads cleanly without running it
```

### Run with a different server / MLflow target

```bash
memex-eval suite run basic_extraction \
  --server http://staging-memex.internal/api/v1/ \
  --mlflow-uri http://mlflow.internal:5000 \
  --mlflow-experiment ad-hoc-investigation
```

### Run every suite serially

```bash
memex-eval suite run --all
```

### Run a single suite with a knob temporarily flipped on the server

You're running the server yourself; you've already started it with `MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=0.5 memex serve`; you just want the run logged with `override.*` params for diff-friendliness in MLflow:

```bash
memex-eval suite run basic_extraction \
  --override server.memory.retrieval.reranking_mw_alpha=0.5
```

For automatic server lifecycle (spawn-with-overrides-then-clean-up), use `sweep` instead.

### Sweep a knob across N values

```bash
memex-eval suite sweep basic_extraction \
  --param server.memory.retrieval.reranking_mw_alpha=0.0,0.1,0.3,0.5,0.7
```

The harness spawns one server subprocess per sweep point (with the override env var set), runs the suite against it, gracefully shuts it down, and proceeds to the next point. All N runs land as MLflow nested children under one parent run for trivial side-by-side comparison.

> **Sweep is local-only.** `--server <remote-url>` for sweep is rejected because env-var overrides only take effect at process startup, not on a long-running server.

### Open MLflow to inspect

```bash
mlflow ui --backend-store-uri file://./mlruns
# → http://localhost:5000
```

In the UI: select your experiment, see the run list, click any run for params/metrics/artifacts, or check ≥2 runs and click **Compare** for side-by-side.

### Just-recipe shortcuts

The project ships justfile recipes for the most common cases:

```bash
just suite-list                              # alias for `memex-eval suite list`
just suite-run basic_extraction              # alias for `memex-eval suite run basic_extraction`
just suite-sweep basic_extraction \
  server.memory.retrieval.reranking_mw_alpha=0.0,0.3,0.5
```

---

## CLI reference — every command and flag

All commands are subcommands of `memex-eval suite`. Run any command with `--help` for the live flag list.

### `memex-eval suite list`

List every discoverable suite.

```
memex-eval suite list [--json]
```

| Flag | Default | Description |
|---|---|---|
| `--json` | off | Machine-readable output instead of the rich table |

**Output (default)**: a table with columns `name`, `version`, `schema_version`, `tags`, `primary_metrics`, `requires_llm_judge`, `requires_postgres`.

### `memex-eval suite show <name>`

Render a suite's README and scenarios summary for human inspection.

```
memex-eval suite show <name> [--scenarios-only] [--metadata-only]
```

| Flag | Default | Description |
|---|---|---|
| `--scenarios-only` | off | Skip README; print just the scenario summary |
| `--metadata-only` | off | Print just the YAML-shaped metadata |

### `memex-eval suite validate <name>`

Validate a suite's structural integrity without running it. Use this in CI and after authoring a new suite.

```
memex-eval suite validate <name> [--all] [--strict]
```

| Flag | Default | Description |
|---|---|---|
| `--all` | off | Validate every discoverable suite |
| `--strict` | off | Fail on lint warnings (e.g. duplicate scenario IDs across suites) |

Exit code is non-zero on any validation failure.

### `memex-eval suite run <name>`

Run a suite once.

```
memex-eval suite run [<name>|--all]
  [--server URL]
  [--mlflow-uri URL]
  [--mlflow-experiment NAME]
  [--mlflow-run-name NAME]
  [--override KEY=VALUE]   (repeatable)
  [--replicates N]
  [--seed INT]
  [--output PATH]
  [--no-llm-judge]
  [--judge-model MODEL]
  [--verbose]
```

| Flag | Default | Description |
|---|---|---|
| `--all` | — | Run every discoverable suite serially (mutually exclusive with `<name>`) |
| `--server` | `http://localhost:8000/api/v1/` (or `MEMEX_EVAL_DEFAULT_SERVER`) | Target Memex API URL |
| `--mlflow-uri` | `MLFLOW_TRACKING_URI` env, else `file://./mlruns` | MLflow tracking server / file URI |
| `--mlflow-experiment` | `memex-suite-<name>-v<schema_version>` | Experiment name override |
| `--mlflow-run-name` | auto | Human-readable run name (e.g. `mw0.5-decay0.1`) |
| `--override` | none | Repeatable. `<dotted.config.path>=<value>`; logged as `override.*` MLflow params. Validated against `MemexConfig` schema at parse time |
| `--replicates` | `1` | Run each scenario N times; surfaces `metric.<key>.std`. `>1` is recommended for LLMJudge-bearing suites |
| `--seed` | run-id-derived | Seeds Python `random` + `numpy.random` (NOT the LLM judge — see §3.6) |
| `--output` | none | Also dump full `RunResult` JSON to a path |
| `--no-llm-judge` | off | Skip LLMJudge / UsefulAtK scenarios; deterministic-only run (faster, free) |
| `--judge-model` | `gemini/gemini-3-flash-preview` (or `EVAL_JUDGE_MODEL` env) | Override LLM judge model (litellm format) |
| `--verbose` / `-v` | off | DEBUG-level logging |

Exit code is non-zero if any scenario fails or errors.

### `memex-eval suite sweep <name>`

Run a suite N times across knob values, with full server-lifecycle management. Logs all N runs as MLflow nested children of one parent run.

```
memex-eval suite sweep <name>
  --param KEY=v1,v2,v3,...   (repeatable; values comma-separated)
  [--mlflow-uri URL]
  [--mlflow-experiment NAME]
  [--server URL]               (must be localhost; non-local is rejected)
  [--server-startup-timeout SEC]
  [--graceful-shutdown-seconds SEC]
  [other run flags]
```

| Flag | Default | Description |
|---|---|---|
| `--param` | required | Repeatable. Format: `<dotted.config.path>=<v1>,<v2>,...`. Each value spawns a sweep point. Multiple `--param` flags produce a Cartesian product (cap: ≤20 total points). Validated against `MemexConfig`; SecretStr fields rejected |
| `--server` | `http://localhost:<random-free-port>/api/v1/` | Must be localhost; non-local URLs hard-fail with `SweepNotSupportedRemote` |
| `--server-startup-timeout` | `60` (seconds) | Per-point timeout for `/health` to return 200 after subprocess spawn |
| `--graceful-shutdown-seconds` | `30` | SIGTERM grace window before SIGKILL on each point's server |

The first sweep run takes longer (~60s server-startup) than the second; subsequent points reuse cached imports where possible. Per-point cost: server cold-start + ingest + extraction + scenario loop. Budget ~3-5 minutes per point for a typical 5-scenario suite with 5 source notes.

### `memex-eval suite history <name>`

Tabulate a metric across MLflow runs filtered by git commit range. The "follow it over time" view.

```
memex-eval suite history <name>
  --metric KEY                 (e.g. metric.recall_at_10.mean)
  --since-git-rev SHA          (e.g. 9f6df449 or HEAD~30)
  [--mlflow-uri URL]
  [--mlflow-experiment NAME]
  [--limit N]
  [--json]
```

| Flag | Default | Description |
|---|---|---|
| `--metric` | required | Metric key to tabulate (must be a logged MLflow metric) |
| `--since-git-rev` | required | Git commit, branch, or `HEAD~N`; the resolved range is `<since>..HEAD` |
| `--limit` | `100` | Max runs to include |
| `--json` | off | Machine-readable output instead of markdown table |

Output (default): a markdown table with `git_sha (short)`, `start_time`, `<metric>`, `suite.version`. Sorted by `start_time` ascending so trends are visible top-to-bottom.

### Global env-var fallbacks

Any CLI default can be overridden via env:

| Env var | Effect |
|---|---|
| `MEMEX_EVAL_DEFAULT_SERVER` | Default for `--server` |
| `MLFLOW_TRACKING_URI` | Default for `--mlflow-uri` |
| `MEMEX_EVAL_MLFLOW_EXPERIMENT` | Default for `--mlflow-experiment` |
| `EVAL_JUDGE_MODEL` | Default for `--judge-model` |
| `GOOGLE_API_KEY` | Required by the LLM judge |
| `MEMEX_RUN_HEAVY_INTEGRATION` | Set to `1` to enable `test_server_control.py` and other slow integration tests |

### Knob-override path schema (the `--override` and `--param` formats)

The dotted path follows `MemexConfig.model_fields` traversal. Examples:

| Knob | Dotted path |
|---|---|
| MW boost strength | `server.memory.retrieval.reranking_mw_alpha` |
| Recency boost | `server.memory.retrieval.reranking_recency_alpha` |
| FSFM graph weight | `server.memory.retrieval.deprioritize.weights.graph` |
| FSFM mw weight | `server.memory.retrieval.deprioritize.weights.mw` |
| Exploration epsilon | `server.memory.retrieval.exploration_epsilon` |
| Confidence boost alpha | `server.memory.retrieval.confidence_alpha` |
| Decay boost alpha | `server.memory.retrieval.reranking_decay_alpha` |
| Propose threshold | `server.memory.reflection.propose_threshold` |

The CLI translates `<dotted.path>=<value>` to a pydantic-settings env var by uppercasing and using `__` as the nesting delimiter:

```
server.memory.retrieval.reranking_mw_alpha=0.5
↓
MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=0.5
```

Validation runs at parse time so typos fail before any server is spawned.

---

## 1. Adding a scenario to an existing suite

### When to do this

You have a suite (e.g. `basic_extraction`) that already tests Memex's behavior in some area, and you want to assert one more thing about it. Examples: a new query for the same source corpus; a new gold-unit-IDs check; a regression-pin for a bug fix.

### What you'll touch

- **One file**: the suite's `__init__.py` (`packages/eval/src/memex_eval/suites/<name>/__init__.py`)
- **Optionally**: a new markdown source note under `sources/` if your scenario needs new content
- **Bump** the `suite_version` field in `METADATA` (any sources change requires it; CI enforces)

### Step-by-step

#### 1.1 Open the suite module

```bash
$EDITOR packages/eval/src/memex_eval/suites/basic_extraction/__init__.py
```

You'll see something like:

```python
from memex_eval.suite import (
    Suite, SuiteMetadata, SuiteSources, Scenario,
    KeywordsPresent, GoldUnitIds, LLMJudge,
)
from pathlib import Path

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='basic_extraction',
    schema_version='1',
    suite_version='1.0.0',
    description='Tests fact extraction, keyword and semantic retrieval, ...',
    tags=['extraction', 'retrieval', 'entities'],
    primary_metrics=['pass_rate', 'metric.recall_at_10.mean', 'metric.mrr.mean'],
    components_under_test=[...],
    knobs=['server.memory.retrieval.reranking_mw_alpha', ...],
    requires_llm_judge=True,
)

SCENARIOS = [
    Scenario(
        id='alpha_lead_lookup',
        description='Sarah Chen is named as Project Alpha lead.',
        query='Who is the lead for Project Alpha?',
        top_k=5,
        expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
    ),
    # ... existing scenarios ...
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
```

#### 1.2 Pick the right `ExpectedOutcome` for your assertion

| You want to assert… | Use |
|---|---|
| Specific keywords appear in top-K results | `KeywordsPresent(keywords=[...])` |
| Specific keywords are absent (e.g. deprioritized) | `KeywordsAbsent(keywords=[...])` |
| A named entity exists in the graph | `EntityResolves(expected_names=[...])` |
| Two entities co-occur via the entity graph | `EntityCooccurs(expected_neighbors=[...])` |
| Specific notes appear in top-K (Recall@k / MRR / NDCG) | `GoldUnitIds(note_keys=[...], metrics_to_compute=['recall_at_k', 'mrr'])` |
| A specific rank ordering of keywords | `RankingOrder(expected_keyword_order=[...])` |
| A unit must NOT appear under default flags | `ExcludedByDefault(forbidden_keywords=[...])` |
| LLM-as-judge against a rubric | `LLMJudge(rubric='…', threshold=0.75)` |
| LLM-judged "useful at K" (returns useful_at_5 ratio) | `UsefulAtK(rubric='…', k=5)` |
| A lint rule fires | `LintFindingPresent(expected_rule_name='surprise_gate_llm')` |
| Multiple of the above bundled | `CompositeOutcome(children=[...])` |

#### 1.3 Append your scenario to the `SCENARIOS` list

Two examples — a deterministic check and a graded retrieval check.

**Deterministic** (no LLM calls):

```python
Scenario(
    id='alpha_tech_stack_python',                 # snake_case, unique within suite
    description='Project Alpha tech stack mentions Python 3.12.',
    query='What language does Project Alpha use?',
    top_k=5,
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['Python 3.12'],
    ),
),
```

**Retrieval Recall@k / MRR** (references source notes by `note_key`, which is the markdown file's stem):

```python
Scenario(
    id='alpha_top5_covers_both_notes',
    description='Top-5 retrievals for "Project Alpha" cover both kickoff and update notes.',
    query='Project Alpha at Acme',
    top_k=5,
    expected=GoldUnitIds(
        type='gold_unit_ids',
        note_keys=['project-alpha-kickoff', 'project-alpha-update'],
        metrics_to_compute=['recall_at_k', 'mrr'],
    ),
),
```

The runner resolves `note_keys` to actual unit IDs after ingestion via `GET /api/v1/notes/{note_id}/memory_units`. **If a referenced note produces zero memory units after extraction, the scenario reports `status='error'` (not `'fail'`)** so a silent recall=0 cannot poison your numbers.

#### 1.4 If your scenario needs new source content, drop a markdown file under `sources/`

```bash
cat > packages/eval/src/memex_eval/suites/basic_extraction/sources/project-alpha-budget.md <<'EOF'
---
vault_name: null
tags: [project, budget, acme-corp]
description: Budget addendum for Project Alpha.
---

# Project Alpha — Budget Addendum

Approved budget: $2.4M FY2025. Owner: Sarah Chen.
...
EOF
```

The frontmatter block (between `---`) is parsed into `SourceNote` metadata; everything below is the markdown body. The filename stem (`project-alpha-budget`) becomes the `note_key`. Do **not** create duplicate stems within one suite — the loader will reject it.

For binary assets (e.g. a PNG referenced in the note), drop them into `sources/assets/` and reference them in `SourceNote.assets` if needed (advanced — see §3).

#### 1.5 Bump `suite_version`

Any change to `sources/` or `SCENARIOS` is a meaningful surface change. Bump the version:

```python
METADATA = SuiteMetadata(
    ...,
    suite_version='1.1.0',  # was 1.0.0
    ...
)
```

CI computes `sources_content_hash` (sha256 over sorted source-note + asset bytes) and **fails if the hash changes without a `suite_version` bump**. Bumping signals to MLflow that runs from this version onwards may not be directly comparable to prior runs.

> **Schema-version vs suite-version.** `schema_version` is the framework's metadata schema (changes rarely; bumps create a new MLflow experiment `memex-suite-<name>-v<schema_version>`). `suite_version` is the suite's content version (changes often; logged as a param within the same experiment).

#### 1.6 Validate, run, inspect

```bash
# Validate the suite loads + scenario IDs are well-formed + note_keys resolve
memex-eval suite validate basic_extraction

# Run against a local server (default http://localhost:8000/api/v1/)
memex-eval suite run basic_extraction \
  --mlflow-uri file://./mlruns \
  --mlflow-experiment memex-suite-basic_extraction-v1
```

Open MLflow UI:

```bash
mlflow ui --backend-store-uri file://./mlruns
```

Filter by `params.suite.version='1.1.0'` to see only your new run. Inspect `metric.recall_at_5.mean`, `metric.mrr.mean`, `pass_rate`. Click into the run → Artifacts → `run_result.json` for per-scenario detail.

#### 1.7 Tests

Add a unit test asserting your `ExpectedOutcome` scores correctly on hand-built actuals (place in `packages/eval/tests/suite/test_outcomes.py`). The runner-level e2e (`test_runner_e2e.py`) auto-discovers your suite if it's well-formed; no separate registration needed.

---

## 2. Adding an entire new suite

### When to do this

You're testing a part of the system not covered by any existing suite. Examples: FSFM weight tuning, entity-resolution edge cases under high-volume vaults, contradiction-detection latency on long-form documents.

### What you'll touch

- **One new directory** under `packages/eval/src/memex_eval/suites/<your_name>/`
- Inside that: `__init__.py`, `README.md`, `sources/` directory with markdown notes
- **No registration step** — the loader walks `packages/eval/src/memex_eval/suites/` and discovers any directory containing a `__init__.py` exporting a `SUITE: Suite` constant

### Directory layout

```
packages/eval/src/memex_eval/suites/fsfm_weights/
├── __init__.py                 # exports SUITE: Suite (the canonical definition)
├── README.md                   # plain markdown — scientific description
└── sources/
    ├── _shared.md              # source notes; filename stem = note_key
    ├── high-graph-pressure.md
    ├── low-mw-score.md
    └── assets/                 # optional, only if you need binary attachments
        └── pressure-diagram.png
```

### Step-by-step

#### 2.1 Create the directory

```bash
mkdir -p packages/eval/src/memex_eval/suites/fsfm_weights/sources
cd packages/eval/src/memex_eval/suites/fsfm_weights
```

#### 2.2 Write `README.md`

Plain markdown; **no YAML frontmatter required**. The runner copies this into the MLflow artifact bundle for every run. Keep it scientific:

```markdown
# FSFM Weights Suite

## What this tests

Targeted regression set for the four FSFM composite weights
(`weight_graph` / `weight_mw` / `weight_temporal` / `weight_entity`) at
`server.memory.retrieval.deprioritize.weights.*`. Source notes are
designed so that each weight's contribution is isolable: scenarios pin
the expected ranking under default weights and surface measurable
deltas under sweeps.

## Why

These four weights drive auto-deprioritization (see
`services/deprioritize_score.py`). Defaults were chosen from literature
precedent; this suite gives empirical signal as we tune them.

## Components under test

- `services/deprioritize_score.py` — FSFM composite scoring
- `memory/retrieval/engine.py:1581-1588` — boost composition

## Primary metrics

- `metric.recall_at_10.mean` — does the top-10 still contain ground-truth
  units after deprioritization?
- `metric.mrr.mean` — does the rank of the first relevant unit shift?
- `pass_rate` — do the deterministic ranking-order scenarios still pass?

## How to interpret

A drop in `metric.recall_at_10.mean` under a sweep point means the new
weight set is over-deprioritizing. A drop in `pass_rate` means a
deterministic ranking expectation broke. Use the parent-vs-children
view in MLflow to compare across sweep points.
```

#### 2.3 Write source notes

Each `sources/*.md` file is one note. Optional YAML frontmatter for per-note metadata:

```markdown
---
vault_name: null            # null → default vault; or 'work', 'personal', etc. for multi-vault
tags: [project, fsfm-test]
description: Notes capturing high graph pressure scenario.
---

# Project Phoenix Crisis

Sarah Chen flagged a major issue with the data pipeline on March 12.
The team agreed Phase 1 was a failure. Marcus Rivera disagreed and
proposed continuing with the original architecture.

By March 15, leadership reversed the decision: Phase 1 is now
considered a partial success. The architecture proceeds.
...
```

Filename stem = `note_key`. Stems must be unique within a suite. Frontmatter keys are optional; if omitted, defaults are used.

> **Idempotency / dedup.** If your scenario needs the SAME content ingested multiple times (e.g. testing duplicate-detection), embed `uuid4()` in the markdown via Python — see "Templated content" in §3.

#### 2.4 Write `__init__.py`

```python
"""FSFM weights regression suite.

Targeted scenarios that surface measurable deltas under the four
FSFM weight knobs. Designed to be sweep-friendly.
"""
from pathlib import Path

from memex_eval.suite import (
    Suite,
    SuiteMetadata,
    SuiteSources,
    Scenario,
    GoldUnitIds,
    KeywordsPresent,
    RankingOrder,
)

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='fsfm_weights',                # MUST match directory name
    schema_version='1',
    suite_version='1.0.0',
    description='FSFM composite weight regression set.',
    tags=['fsfm', 'deprioritize', 'retrieval'],
    primary_metrics=[
        'pass_rate',
        'metric.recall_at_10.mean',
        'metric.mrr.mean',
    ],
    components_under_test=[
        'services.deprioritize_score',
        'retrieval.cross_encoder_rerank',
    ],
    knobs=[
        'server.memory.retrieval.deprioritize.weights.graph',
        'server.memory.retrieval.deprioritize.weights.mw',
        'server.memory.retrieval.deprioritize.weights.temporal',
        'server.memory.retrieval.deprioritize.weights.entity',
    ],
    requires_llm_judge=False,
    requires_postgres=True,
)

SCENARIOS = [
    Scenario(
        id='phoenix_resolution_visible',
        description='After the March 15 reversal, top results emphasize the resolution.',
        query='Did Project Phoenix Phase 1 succeed?',
        top_k=10,
        expected=KeywordsPresent(
            type='keywords_present',
            keywords=['partial success', 'reversed'],
        ),
    ),
    Scenario(
        id='phoenix_top10_recall',
        description='Top-10 retrievals cover both the crisis note and the resolution note.',
        query='Project Phoenix Phase 1',
        top_k=10,
        expected=GoldUnitIds(
            type='gold_unit_ids',
            note_keys=['high-graph-pressure'],
            metrics_to_compute=['recall_at_k', 'mrr'],
        ),
    ),
    # ... more scenarios
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
```

#### 2.5 Validate it loads

```bash
memex-eval suite validate fsfm_weights
```

The validator checks:
- `__init__.py` exports `SUITE: Suite`
- All scenario IDs are unique and snake_case
- Every `GoldUnitIds.note_keys` entry maps to an existing source note
- `requires_llm_judge=True` paired with `LLMJudge`/`UsefulAtK` outcomes (and vice versa)
- Knob paths in `metadata.knobs` resolve against the live `MemexConfig` schema
- README exists; sources directory exists and has at least one note

If validation passes, the suite is auto-discovered by `memex-eval suite list`.

#### 2.6 Smoke run + sweep

```bash
# One-shot run
memex-eval suite run fsfm_weights \
  --mlflow-uri file://./mlruns

# Sweep one weight
memex-eval suite sweep fsfm_weights \
  --param server.memory.retrieval.deprioritize.weights.graph=0.3,0.4,0.5,0.6 \
  --mlflow-uri file://./mlruns \
  --mlflow-experiment memex-sweep-fsfm-graph-202605
```

Sweep output: 1 parent run + N child runs in MLflow, each child carries `sweep.point_index` + `override.<knob>` params for trivial side-by-side comparison.

#### 2.7 Tests

The framework's e2e test (`test_runner_e2e.py`) auto-discovers your suite. Add suite-specific unit tests in `packages/eval/tests/suite/test_<your_suite_name>.py` if you have non-trivial scenario logic worth pinning.

---

## 3. Nuts and bolts

> For complete CLI flags and per-command options see **CLI reference — every command and flag** above. This section covers the deeper mechanics (MLflow schema, sweep contract, redaction, reproducibility).

### 3.2 Knob overrides — `--override`

`--override <dotted.path>=<value>` translates to a `MEMEX_*` environment variable using the project's nested-delimiter convention (`__`):

```bash
# CLI form
--override server.memory.retrieval.reranking_mw_alpha=0.5

# Translates to
MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=0.5
```

Both single-shot `run` and `sweep` use this. **Override paths are validated against the live `MemexConfig` schema at parse time** — typos fail before any server is spawned. Paths that resolve to `SecretStr` fields are **rejected** (you cannot sweep a secret).

For a single-shot run, the user is expected to have already configured the running server with the desired knob value. For sweeps, the harness manages server lifecycle (see §3.4).

### 3.3 MLflow conventions

#### Experiment naming

| Use case | Experiment name |
|---|---|
| Default longitudinal tracking of a suite | `memex-suite-<name>-v<schema_version>` |
| One-off sweep | `memex-sweep-<name>-<knob>-<YYYYMM>` |
| Custom | whatever you pass to `--mlflow-experiment` |

#### Params (logged immutably at run start)

Three sub-budgets (cap: ≤80 total params):

- **Base (≤30)** — system identity:
  - `suite.name`, `suite.version`, `suite.schema_version`, `suite.sources_hash`
  - `git.sha`, `git.branch`, `memex.version`, `release.version`
  - `judge.model`, `judge.model_revision`, `judge.api_base`, `judge.temperature`
  - `embedding.backend_type`, `embedding.model_id`
  - `reranker.backend_type`, `reranker.model_id`
  - `seed`, `replicates`, `vault.name`
- **Knobs (≤30)** — every entry in `SuiteMetadata.knobs` is logged as `knob.<dotted.path>=<resolved value>`
- **Overrides (≤20)** — every `--override` flag is logged as `override.<dotted.path>=<value>` (so MLflow filters can target them)

#### Metrics (numeric, queryable, comparable across runs)

- `suite.pass_rate` — fraction of scenarios passing (denominator excludes skipped)
- For every scenario metric key: `metric.<key>.mean`, `metric.<key>.std`, `metric.<key>.min`, `metric.<key>.max`
- `latency_ms.p50`, `latency_ms.p95`, `latency_ms.mean`
- `count.scenarios`, `count.passed`, `count.failed`, `count.errored`, `count.skipped`

> **Per-scenario detail is NOT logged as MLflow metrics.** Scenario-level outcomes go into the `run_result.json` artifact instead, because (a) scenario-set membership shifts when a suite_version bumps, and (b) per-scenario metric keys would explode the namespace. Read the artifact for cross-run scenario-level analysis.

#### Tags

- `suite.name=<name>`, `schema_version=<v>` — for filtering in MLflow UI
- `suite.tags=<comma-joined>` — values, not boolean presence (e.g. `extraction,retrieval`)
- `components=<comma-joined>` — `components_under_test` joined
- For sweep child runs: `sweep.id`, `sweep.point_index`, `mlflow.parentRunId` (auto-set by `start_run(nested=True)`)

#### Artifacts (queryable but not directly diff-able in UI)

- `run_result.json` — full `RunResult.model_dump_json()` including every per-scenario outcome
- `README.md` — frozen suite description at run time
- `sources/` — frozen copy of every source note + asset (so a 6-month-old run is reproducible from artifacts alone)
- `config_snapshot.json` — full `MemexConfig` dump from `GET /api/v1/system/config` with secrets redacted (see §3.7)

### 3.4 Sweeps

```bash
memex-eval suite sweep basic_extraction \
  --param server.memory.retrieval.reranking_mw_alpha=0.0,0.1,0.3,0.5,0.7 \
  --mlflow-experiment memex-sweep-basic_extraction-mw_alpha-202605
```

What happens:

1. Override path validated against `MemexConfig` schema. Typos fail immediately.
2. **Local server only.** `--server` pointing at a remote/staging URL is rejected with `SweepNotSupportedRemote` because env-var overrides only take effect at process startup, not on a long-running server.
3. Parent MLflow run opened in the target experiment.
4. Per sweep point (serial):
   - Free port allocated.
   - Server subprocess spawned with the override env (`MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=0.5`).
   - `/api/v1/health` polled until 200 (60s timeout).
   - Child MLflow run opened with `start_run(nested=True)` — parent/child relationship registers in the SQL backend; UI tree renders correctly.
   - Suite runs against `localhost:<free_port>`.
   - Server gracefully shut down via SIGTERM; if not exiting in 30s, SIGKILL with a `sweep.shutdown_method=kill` tag on the child run.
5. **Partial-failure tolerance.** If a child raises, the parent does not unwind — the failing child is tagged `sweep.partial_failure=true` and the harness continues.
6. After all points: parent records `sweep.children_total` + `sweep.children_failed` and finalizes.

Concurrent sweep harnesses against the same Postgres are **not supported** (`pg_try_advisory_lock` contention in the scheduler).

### 3.5 The longitudinal view — `suite history`

Tracks a metric across MLflow runs filtered by the git commit range:

```bash
memex-eval suite history basic_extraction \
  --metric metric.recall_at_10.mean \
  --since-git-rev 9f6df449 \
  --mlflow-uri file://./mlruns
```

Implementation:

1. Resolves the git commit range: `git rev-list --reverse <since-git-rev>..HEAD`.
2. `mlflow.search_runs(experiment_names=[memex-suite-<name>-v<schema_version>])`.
3. Filter Python-side by `params['git.sha']` ∈ resolved set.
4. Output a markdown table: `git_sha (short)` × `start_time` × `metric value` × `suite.version`. Pass `--json` for machine-readable output.

This is the "follow it over time" view — a metric's trajectory as features land.

### 3.6 Reproducibility

What's logged (every run):

- `git.sha` + `git.branch` (where the eval framework was running from)
- `memex.version` (pip-installed memex version)
- `suite.version` + `suite.sources_hash` (what content was tested)
- `config_snapshot.json` (every server knob, with secrets redacted)
- `embedding.model_id` + `reranker.model_id` (which models were active)
- `judge.model` + `judge.model_revision` (probed at run start)

To replay a 6-month-old run:

1. `git checkout <git.sha>`
2. Restore `config_snapshot.json` (excluding `<redacted>` secrets — re-supply via env)
3. Re-install the same `memex.version` (`uv add memex==<v>`)
4. Use the artifact's frozen `sources/` snapshot as the new suite source

> **Determinism caveat.** `--seed` only seeds Python `random` and `numpy.random`. The LLM judge (Gemini via dspy) is **non-deterministic** — pinned at `temperature=0.0` to minimize variance, but per-replicate variance under `LLMJudge` outcomes is real and surfaces as `metric.<key>.std`. A non-zero `std` is information, not a bug.

### 3.7 Secret redaction

`config_snapshot.json` is generated by `GET /api/v1/system/config` (admin-auth-gated) and run through `memex_common.redaction.redact` before logging. Two-tier rules:

- **Exact-name set**: `password`, `api_key`, `access_key_id`, `secret_access_key`, `session_token`, `token`, `webhook_secret`, `dsn`, `database_url`
- **Suffix patterns**: `*_password`, `*_api_key`, `*_token`, `*_secret` (the leading underscore excludes plural `*_tokens`)
- **Shape rule** for `ApiKeyConfig`-shaped dicts (siblings `key` + `policy`): `key` is redacted regardless of name

For each redacted leaf at key `k`:
- value → `'<redacted>'`
- a sibling boolean `<k>_set` is added (`true` if originally non-empty, `false` if `None`/empty)

You can tell whether a secret was *configured* without leaking the value. The non-secret tunable knobs (`max_tokens`, `chunk_size_tokens`, `tokenizer`, etc.) **always pass through unredacted**.

### 3.8 Server defaults

`memex-eval suite *` defaults to `http://localhost:8000/api/v1/`. Override with:
- `--server <url>` per command, OR
- `MEMEX_EVAL_DEFAULT_SERVER=<url>` env var

Sweep mode is **local-only**. `--server <remote-url>` for sweeps hard-errors. Use `memex-eval suite run --server <remote>` for single-point runs.

### 3.9 Replicates

```bash
memex-eval suite run basic_extraction --replicates 5
```

Each scenario runs 5× in the same vault. The aggregator emits `metric.<key>.std` so you can distinguish judge-noise from real signal. Defaults to 1 (no replication, no std signal).

When using `--replicates >1` with `LLMJudge` outcomes, expect non-zero std. Use it as a noise floor when comparing two configurations.

### 3.10 Templated content (for idempotency-defeat)

Some scenarios need each ingest to be unique to defeat content-hash dedup (e.g. testing duplicate-handling logic). Because suites are **Python-only**, you can construct source content with `uuid4()`:

```python
# In the suite's __init__.py
from uuid import uuid4
from memex_eval.suite import Suite, SuiteSources, SourceNote

_ROOT = Path(__file__).parent

# Programmatically build source notes with unique content
_GENERATED_NOTES = [
    SourceNote(
        path=_ROOT / 'sources' / f'generated-{i}.md',
        content=f'# Note {i}\n\nUnique tag: {uuid4().hex}\n...',
        tags=['generated'],
        note_key=f'generated-{i}',
    )
    for i in range(10)
]

# Combine with disk-loaded notes
_DISK_SOURCES = SuiteSources.from_directory(_ROOT / 'sources')
SOURCES = SuiteSources(notes=_DISK_SOURCES.notes + _GENERATED_NOTES)

SUITE = Suite(
    metadata=METADATA,
    sources=SOURCES,
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
```

This is also how the `scale` suite (10 generated notes) avoids 10 separate markdown files on disk.

### 3.11 Schema-version evolution

When the framework's `schema_version` bumps (rare — happens when a metric is renamed or removed):

- Old runs (`v1` runs) and new runs (`v2`) live in **different MLflow experiments** (`memex-suite-<name>-v1` vs `memex-suite-<name>-v2`)
- A suite's `__init__.py` declares `schema_version='2'` to opt in to the new framework conventions
- Migrations between versions are documented per-bump with a `MIGRATION.md` file at the framework level

Bumping `suite_version` (frequent — happens any time you change scenarios or sources) keeps everything in the same experiment. Comparable across the bump as long as the metric set stays stable.

### 3.12 Adding a new `ExpectedOutcome` type (advanced)

If none of the built-in outcomes fit, subclass `_ExpectedOutcomeBase` in your suite's `__init__.py` (local) or contribute to `memex_eval.suite.base` (framework-level):

```python
from memex_eval.suite import _ExpectedOutcomeBase, ActualMemorySearch
from typing import Literal

class MyCustomOutcome(_ExpectedOutcomeBase):
    type: Literal['my_custom_outcome']
    expected_thing: str

    async def fetch(self, api, vault_id, scenario):
        units = await api.search(query=scenario.query, vault_ids=[vault_id], limit=scenario.top_k)
        return ActualMemorySearch(units=units)

    def score(self, actual: ActualMemorySearch) -> dict[str, float]:
        # Return a dict of metric keys, e.g.:
        return {'my_custom_metric': 1.0 if any(self.expected_thing in u.text for u in actual.units) else 0.0}

    def metric_keys(self) -> list[str]:
        return ['my_custom_metric']
```

Register it in the `ExpectedOutcomeUnion` discriminated union if framework-level; otherwise just use it directly in your suite. Each `ExpectedOutcome` subclass owns **both** the runtime fetch shape (what to ask Memex for) AND the scoring (what to do with the result) — the runner only orchestrates.

---

## Recipes

### "I changed an FSFM weight default in core. Did anything regress?"

```bash
memex-eval suite run --all \
  --mlflow-experiment baseline-after-fsfm-weight-change \
  --mlflow-uri $MLFLOW_URI

# Compare in MLflow UI: filter by experiment, compare against the prior baseline run.
```

### "I need to find the optimal `mw_alpha` value"

```bash
memex-eval suite sweep basic_extraction \
  --param server.memory.retrieval.reranking_mw_alpha=0.0,0.1,0.2,0.3,0.4,0.5,0.7,1.0 \
  --mlflow-experiment mw_alpha_sweep_$(date +%Y%m%d)

# Open MLflow → Compare child runs → plot metric.recall_at_10.mean vs override.<knob>
```

### "I want to validate a new suite without running it"

```bash
memex-eval suite validate fsfm_weights
```

### "I want to see what changed between two runs"

```bash
# Side-by-side params + metrics in MLflow UI:
mlflow ui --backend-store-uri file://./mlruns
# → select two runs → Compare button
```

### "I'm seeing flaky results from a LLMJudge scenario"

```bash
memex-eval suite run my_suite --replicates 5
# Look at metric.<key>.std — if it's high, judge-noise is your problem,
# not the system under test.
```

### "I want to track the same suite over the last 3 months"

```bash
memex-eval suite history basic_extraction \
  --metric metric.recall_at_10.mean \
  --since-git-rev $(git rev-parse HEAD~90)
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `SuiteNotFound: <name>` | Directory not discoverable | Verify `__init__.py` exports `SUITE: Suite`, name in `METADATA` matches dir name |
| `note_key 'X' has zero resolved units` (status='error') | Extraction failed for that note | Inspect server logs; rerun once; if it persists, the note's content may not produce extractable facts |
| `OverrideValidationError: server.memory.X.Y is not a valid path` | Typo in `--override` | Run `memex-eval suite show <name>` to see the suite's known knobs; or check `MemexConfig.model_fields` |
| `OverrideValidationError: server.X is a SecretStr` | Tried to override a secret | Secrets cannot be swept; pass via env var only |
| `SweepNotSupportedRemote` | `--server` pointed at non-localhost | Sweeps are local-only; use `memex-eval suite run --server <remote>` for single-point runs |
| `MLflowNotInstalled` at import time | mlflow extra not installed | `uv add memex-eval[mlflow]` (or wait for the dep promotion in PR 1c-prep) |
| All metrics at zero, vacuous-looking pass_rate | `--no-llm-judge` set with LLM-only suite | Check the suite's `requires_llm_judge` |
| MLflow run is `KILLED` and a vault `eval-suite-<name>-<id>` lingers | Ctrl-C during a run | Cleanup is best-effort on SIGINT; manually `memex vault delete eval-suite-<name>-<id>` if needed |
| `sources_content_hash changed without suite_version bump` (CI) | You edited a source note but didn't bump version | Bump `suite_version` in `METADATA` |
| Pre-commit hook complains about `uv lock --check` | Dependency change | Run `uv lock` and commit the updated `uv.lock` |

---

## Related reading

- `packages/eval/src/memex_eval/suite/base.py` — the Pydantic data model (start here)
- `packages/eval/src/memex_eval/suite/runner.py` — what happens during a run
- `packages/eval/src/memex_eval/suite/metrics.py` — exact definitions of `recall_at_k`, `mrr`, `ndcg_at_k` (frozen — changes require schema_version bump)
- `packages/eval/src/memex_eval/suite/redaction.py` — secret deny-list rules
- `EVALUATION_BACKLOG.md` — upcoming evaluation work (E1–E6) that builds on this framework
