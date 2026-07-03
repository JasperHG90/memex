# Evaluation Suite Guide

How to author, run, and track Memex evaluation suites with optional MLflow integration for longitudinal comparison.

> **Scope.** This guide covers the **internal** evaluation framework (`memex-eval suite …`). The external benchmarks (LoCoMo, LongMemEval) keep their existing CLIs and are not covered here.

## Mental model

A **Suite** is the unit of evaluation. It bundles:

| Component | What it is | Lives in |
|---|---|---|
| **Sources** | Markdown notes (+ optional binary assets) ingested into a temporary vault | `sources/*.md` and `sources/assets/<note-key>/` |
| **Scenarios** | Typed Python objects describing one verifiable assertion each, **executed in list order** | `__init__.py` (the `SCENARIOS` list) |
| **Inline notes** | Per-scenario markdown notes ingested only when that scenario runs (e.g. contradiction follow-ups) | `Scenario.inline_notes=[InlineNote(...)]` |
| **Metadata** | Suite name/version/tags/primary_metrics/components/knobs | `__init__.py` (the `METADATA` literal) |
| **README** | Scientific description (what + why + what's tested) | `README.md` (plain markdown) |

> **Scenario execution order.** The runner iterates `SCENARIOS` in definition order. Later scenarios may depend on side effects of earlier ones (recorded outcomes, deprioritization, KV writes, inline-note contradictions); this is intentional and contractually pinned by tests.

Every suite run produces a `RunResult` (full per-scenario detail) and, when MLflow is installed and a tracking URI is configured, an MLflow run under experiment `memex-suite-<name>-v<schema_version>` with stable params (suite version, git SHA, knob values, sources hash), stable metrics (`metric.<key>.{mean,std,min,max}`, `latency_ms.{p50,p95}`, `pass_rate`), and artifacts (`run_result.json`, frozen `README.md` + `sources/` snapshot, `config_snapshot.json`, optional `run_notes.md`).

> **MLflow is optional.** Install via `uv add memex-eval[mlflow]` for the MLflow integration; otherwise `--output PATH` writes the full `RunResult` JSON. Without MLflow, longitudinal comparison happens at whatever level your team's run-tracking is — diff two `RunResult` JSON dumps, plot from the file, etc.

---

## Quick start — running suites

### Prerequisites

- Memex server running locally (default `http://localhost:8000/api/v1/`)
- `GOOGLE_API_KEY` in your env if any suite uses LLM-judge outcomes
- For MLflow tracking: `uv add memex-eval[mlflow]` + `MLFLOW_TRACKING_URI` env (or pass `--mlflow-uri`)

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

### Sweep a knob across N values

Sweep one (or more) `MemexConfig` knob and record an MLflow parent run with one nested child per value:

```bash
memex-eval suite sweep acme_corp \
  --param server.memory.retrieval.reranking_mw_alpha=0.0,0.1,0.3,0.5 \
  --mlflow-uri file://./mlruns
```

Each point spawns its own `granian memex_core.server:app` subprocess with the override env var, polls `/api/v1/health`, runs the suite, and SIGTERMs the server. Local-only (env-var overrides only apply at server startup; remote `--server` is hard-rejected).

See `### memex-eval suite sweep <name>` below for the full flag list.

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
  [--notes TEXT | --notes-file PATH]
  [--no-llm-judge]
  [--judge-model MODEL]
  [--verbose]
```

| Flag | Default | Description |
|---|---|---|
| `--all` | — | Run every discoverable suite serially (mutually exclusive with `<name>`) |
| `--server` | `http://localhost:8001/api/v1/` (or `MEMEX_EVAL_DEFAULT_SERVER`) | Target Memex API URL |
| `--mlflow-uri` | `MLFLOW_TRACKING_URI` env, else `file://./mlruns` | MLflow tracking server / file URI |
| `--mlflow-experiment` | `memex-suite-<name>-v<schema_version>` | Experiment name override |
| `--mlflow-run-name` | auto | Human-readable run name (e.g. `mw0.5-decay0.1`) |
| `--override` | none | Repeatable. `<dotted.config.path>=<value>`; logged as `override.*` MLflow params. Validated against `MemexConfig` schema at parse time |
| `--replicates` | `1` | Run each scenario N times; surfaces `metric.<key>.std`. `>1` is recommended for LLMJudge-bearing suites |
| `--seed` | run-id-derived | Seeds Python `random` + `numpy.random` (NOT the LLM judge — see §3.6) |
| `--output` | none | Also dump full `RunResult` JSON to a path |
| `--notes` | none | Free-form description of what changed in the code; uploaded as `run_notes.md` artifact + truncated `notes` MLflow tag for UI filtering |
| `--notes-file` | none | Read `--notes` body from a file (mutually exclusive with `--notes`) |
| `--no-llm-judge` | off | Skip LLMJudge / UsefulAtK scenarios; deterministic-only run (faster, free) |
| `--judge-model` | `gemini/gemini-3-flash-preview` (or `EVAL_JUDGE_MODEL` env) | Override LLM judge model (litellm format) |
| `--verbose` / `-v` | off | DEBUG-level logging |

Exit code is non-zero if any scenario fails or errors.

### `memex-eval suite sweep <name>`

Run a suite N times across a knob grid; spawn a fresh `granian memex_core.server:app` subprocess per point with the override env vars injected. The MLflow parent run holds sweep-level metadata (`sweep.id`, `sweep.knobs`, `sweep.points_planned`) and N child runs nest under it.

```
memex-eval suite sweep <name>
  --param KEY=V1,V2,V3                 (repeatable; cross-products into a Cartesian grid)
  [--server URL]                       (host portion is checked; port is overridden per point)
  [--mlflow-uri URL]
  [--mlflow-experiment NAME]
  [--sweep-label NAME]                 (parent run name; default sweep-<id>)
  [--judge-model MODEL]
  [--replicates N]
  [--seed INT]
  [--scenario ID]                      (repeatable; forwarded to per-point run_suite)
  [--group NAME]                       (repeatable; forwarded to per-point run_suite)
  [--output PATH]                      (write SweepResult JSON aggregate)
  [--log-dir PATH]                     (server stdout/stderr capture; default /tmp/memex-eval-sweep)
  [--verbose]
```

| Flag | Default | Description |
|---|---|---|
| `--param` | required | Repeatable. `KEY=V1,V2,V3`. `KEY` is a dotted `MemexConfig` path (validated against `model_fields` at parse time; `SecretStr` paths are rejected). Multiple `--param` flags cross-product into a grid (e.g. `--param a=1,2 --param b=x,y` → 4 points). |
| `--server` | `http://localhost:8001/api/v1/` | Local-only. Sweep is hard-rejected against non-`localhost`/`127.0.0.1`/`::1` hosts because env-var overrides only apply at server startup. |
| `--sweep-label` | `sweep-<id>` | Sets the parent MLflow run name. Children get `<label>-point-NN`. |
| `--mlflow-experiment` | `memex-sweep-<suite>-<knob_token>-<YYYYMM>` | Override the auto-generated experiment (parent **and** children land in the same one — required for the MLflow Compare view to group them). |
| `--max-points` | `50` | Safety cap on Cartesian grid size; sweeps exceeding it are rejected before any spawn. Bump explicitly when intentional. The harness also strips inherited `MEMEX_*` env vars from each spawned server, so a developer's shell can't silently contaminate sweep results. |
| `--output` | none | Write `SweepResult` JSON (sweep id, parent run id, per-point summaries with override values + status + duration + shutdown method) to a path. |
| `--log-dir` | `/tmp/memex-eval-sweep` | Per-point spawned-server stdout+stderr is tee'd here; surfaced when a point fails. |

**Lifecycle per sweep point:**

1. Allocate a free port in `[18000, 19000)` (random + retry; falls back to OS-assigned ephemeral on bind contention).
2. Spawn `granian --interface asgi --host 127.0.0.1 --port <port> memex_core.server:app` with the parent's environment + `MEMEX_*` overrides layered on top. `start_new_session=True` so signals reach the worker.
3. Poll `/api/v1/health` every 250ms with a 60s timeout. Process exit during startup raises `RuntimeError` with the last 40 log lines.
4. Open an MLflow child run nested under the sweep parent (`mlflow.parentRunId` tag + `nested=True`).
5. Drive `run_suite(server_url=<spawn url>, config_overrides=<point>, ...)`.
6. Send `SIGTERM` to the spawned process group; wait up to 30s for graceful shutdown (the server's lifespan task cancels the scheduler and releases `pg_try_advisory_lock`); escalate to `SIGKILL` if the grace expires. The shutdown method is recorded on the child run.

**MLflow parent / child structure:**

```
memex-sweep-acme_corp-server_memory_retrieval_reranking_mw_alpha-202605
└── sweep-<sweep_id>                     (parent run)
    │  tags: sweep.id / sweep.suite / sweep.knobs / sweep.points_planned
    │        sweep.children_failed / sweep.children_total
    │        sweep.partial_failure (set when any child errored)
    ├── sweep-<sweep_id>-point-00        (child; mlflow.parentRunId=<parent>)
    │   params: override.server.memory.retrieval.reranking_mw_alpha=0.0
    │   metrics: suite.pass_rate / metric.recall_at_10.mean / latency_ms.p95 / ...
    ├── sweep-<sweep_id>-point-01        (override.<...>=0.1)
    └── sweep-<sweep_id>-point-02        (override.<...>=0.3)
```

In the MLflow Compare view: select all children, plot any metric (e.g. `metric.recall_at_10.mean`) on Y vs. `params.override.server.memory.retrieval.reranking_mw_alpha` on X — the curve tells you whether the knob actually moves the metric.

**Constraints:**

- **Local-only.** Env-var overrides only take effect at server startup, so re-knobbing a long-running remote server mid-flight is impossible. The validator hard-rejects non-localhost `--server`.
- **Serial within one sweep.** Two parallel sweep harnesses against the same Postgres would contend on `pg_try_advisory_lock` (the leader-election lock). Run sweeps one at a time; for genuinely independent sweeps, use separate Postgres instances.
- **Knob allowlist.** `--param` paths must traverse `MemexConfig.model_fields`. Typos fail before any spawn (clean error pointing at the offending segment). `SecretStr` fields are rejected — sweeping a secret would exfiltrate it via env + MLflow params.
- **Numeric leaves only.** Sweep values pass through env vars; the leaf field type must be `int` / `float` / `bool` / `str`. Pydantic-model leaves (the inner config sub-trees) are not sweepable.

**Exit code** is non-zero iff any child run failed.

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
| Exploration mode | `server.memory.retrieval.exploration_mode` (`epsilon_greedy` \| `thompson` \| `off`) |
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

## Authoring surface — three ways to write scenarios

Suites can be authored two ways. Both produce identical runtime behavior;
pick whichever ergonomics you prefer.

### Surface A — declarative `Scenario(...)` lists (legacy, still supported)

`SUITE = Suite(metadata=METADATA, scenarios=[Scenario(...), ...])`. Every
existing suite (`acme_corp`, `project_nexus`, `ai_research_lab`) uses
this. Best when scenarios are uniformly declarative — you pick a typed
`ExpectedOutcome` per scenario and the framework runs the rest.

### Surface B — decorator API (`memex_eval.suite.decorator.Suite`)

```python
from memex_eval.suite.decorator import Suite, BaseScenario, ScenarioContext
from memex_eval.suite import KeywordsPresent, SuiteMetadata, SuiteSources

suite = Suite(metadata=METADATA, sources=SUITE_SOURCES)

# 1) Method call: pure declarative — no decorator, no body.
suite.register(
    id='alpha_lookup',
    query='Who leads Project Alpha?',
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
    group='retrieval',
)

# 2) Decorator: function body REPLACES `expected`.
@suite.scenario(id='alpha_custom', query='Who leads Project Alpha?', group='retrieval')
def evaluate(ctx: ScenarioContext) -> None:
    """Body is the evaluator. Mutate ctx.metrics in place. AssertionError → fail."""
    assert 'Sarah Chen' in ctx.answer.answer_text
    ctx.metrics['pass'] = 1.0

# 3) Class-based: full lifecycle override.
@suite.register_class
class AlphaWithSetup(BaseScenario):
    id = 'alpha_with_setup'
    query = 'Who leads Project Alpha?'
    expected = KeywordsPresent(type='keywords_present', keywords=['Sarah Chen'])
    group = 'retrieval'

    async def setup(self, ctx):
        # Runs AFTER declared setup_actions / inline_notes.
        # Use for extra side-effects (e.g. flip a feature flag).
        ...

    async def act(self, ctx):
        # Runs AFTER backend.answer; ctx.answer is the backend's AgentAnswer.
        # Override to mutate or replace ctx.answer (e.g. multi-step retrieval).
        ...

    async def evaluate(self, ctx):
        # Default: runs self.expected.score() into ctx.metrics.
        # Three patterns:
        #   - skip super → fully imperative (declarative `expected` ignored)
        #   - call super, then add → declarative + extra metrics
        #   - just declarative → don't override at all
        await super().evaluate(ctx)
        ctx.metrics['extra_invariant'] = 1.0 if ... else 0.0

    async def teardown(self, ctx):
        # Runs BEFORE the runner's setup_action teardowns + inline-note delete.
        # Use for extra cleanup that must run regardless of pass/fail.
        ...

SUITE = suite  # the loader detects either form and calls .build() if needed
```

> **Lifecycle method semantics.** ``setup`` and ``teardown`` run *around*
> the runner's existing teardown machinery — they're additive, not
> replacements. Default impls are no-ops. ``act`` runs after the backend
> produced ``ctx.answer``; default is no-op (keep the answer unchanged).
> ``evaluate`` is the only one with a non-trivial default — it scores
> ``self.expected`` into ``ctx.metrics`` — and it's the only one where
> calling vs skipping ``super()`` matters semantically.

> **Decorator + `expected=` are mutually exclusive.** ``@suite.scenario(...)``
> takes no ``expected=`` kwarg — the function body IS the expected. If
> you need typed declarative scoring + extra checks, use the class-based
> form with ``await super().evaluate(ctx)`` first then your additions.

> **`ScenarioContext` is intentionally narrow.** Hold complex per-test
> state on ``self`` of the BaseScenario subclass, not on ``ctx``. The
> ``ctx`` dict is for the request/response boundary (query, answer,
> metrics, plumbing the runner already builds).

### Group filter — `--group <name>`

Scenarios can declare `group='retrieval' | 'extraction' | …` (free-form
string). Run a subset:

```bash
memex-eval suite run my_suite --group retrieval
memex-eval suite run my_suite --group retrieval --group extraction
```

`--group` is repeatable. Combines with `--scenario` via **intersection**:
the run executes only scenarios that pass *both* filters. Prerequisite
scenarios (`depends_on_prior_scenarios`) of any included scenario still
execute regardless of their own group — correctness wins.

Closure semantics: dependency closure is computed on each filter axis
*independently before intersection*.
- `--scenario alpha`: includes `{alpha} ∪ closure_of_deps({alpha})`.
- `--group retrieval`: includes `{every scenario in 'retrieval'} ∪ closure_of_deps(...)`.
- Both flags: intersection of the two closures.

Worked example: `--scenario alpha_top5_recall --group retrieval`.
- If `alpha_top5_recall` is in group `'retrieval'`, the result is
  `{alpha_top5_recall}` plus its prerequisite deps, intersected with
  the retrieval-group closure (which already contains them).
- If `alpha_top5_recall` is in a *different* group (e.g. `'extraction'`)
  and its deps are also outside `'retrieval'`, the intersection is
  empty — the scenario is silently skipped.
- If `alpha_top5_recall` is in a different group BUT one of its deps
  (say `setup_x`) IS in `'retrieval'`, the intersection contains *only*
  the dep (`{setup_x}`). The dep runs **without its consumer**, which
  is partial coverage with no useful verdict — the user almost
  certainly didn't want this. Use one filter or the other, not both,
  whenever the requested scenario isn't in the requested group.

If you actually want union ("scenario foo plus everything in group
bar"), pass `--scenario foo --scenario other1 --scenario other2 …`
with explicit IDs, or pre-tag every scenario you want with the same
group label.

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
    InlineNote,    # only needed if any scenario uses inline_notes
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

**Inline notes** — per-scenario notes that aren't part of the shared sources. Use this when one scenario needs ingested content that other scenarios shouldn't see (canonical case: ingest a contradiction-followup that targets a claim in a shared source note).

```python
Scenario(
    id='alpha_lead_contradiction_detected',
    description='Inline note contradicting Sarah-Chen-as-lead surfaces the contradiction.',
    query='Who leads Project Alpha?',
    top_k=5,
    inline_notes=[
        InlineNote(
            note_key='alpha_lead_succession',
            title='Project Alpha lead change',
            content=(
                'Sarah Chen has stepped down as Project Alpha lead. '
                'Marcus Rivera is the new lead effective 2026-05-01.'
            ),
            tags=['acme-corp', 'project-alpha', 'leadership'],
        ),
    ],
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['Marcus Rivera', 'Sarah Chen'],
    ),
),
```

The runner ingests inline notes into the suite vault under the prefixed `note_key` `inline-<scenario_id>-<inline_key>` (so they don't collide across sibling scenarios) and waits for extraction before running the query. Outcomes can reference the inline note via either the short key (`alpha_lead_succession`) or the prefixed form (`inline-<scenario_id>-alpha_lead_succession`); both are wired into `note_key_to_unit_ids`. Validation accepts either.

> **State leak across scenarios.** Inline notes persist in the suite vault for the rest of the run (cleanup happens at suite-end). The prefixed `note_key` prevents identity collisions, but extracted units AND any contradictions/supersessions they trigger are visible to later scenarios. Scenarios that follow a contradiction-bearing inline note should either (a) be the contradiction-checking scenario itself, (b) run earlier in the list, or (c) live in a separate suite.

> **Replicates + inline notes.** With `--replicates >1`, inline notes are ingested once on the first replicate and cached for replicates 2..N. If your scenario's `setup_actions` mutate those units (deprioritize / record_outcome), the second replicate sees the post-mutation state, not a fresh baseline. Use `--replicates=1` for inline-note scenarios where per-replicate isolation matters, or design the scenario so the mutation is the thing being measured.

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

For binary assets (e.g. a PNG referenced in the note), drop them into `sources/assets/<note-key>/` — they'll auto-attach to that note only. Bare files at `sources/assets/*` (no per-note subdir) are NOT auto-attached; the loader will emit a warning. For explicit per-asset filenames in `NoteCreateDTO.files`, set `assets:` in the note's YAML frontmatter.

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
├── __init__.py                       # exports SUITE: Suite (the canonical definition)
├── README.md                         # plain markdown — scientific description
└── sources/
    ├── high-graph-pressure.md        # filename stem = note_key (lowercase, hyphens/underscores ok)
    ├── low-mw-score.md
    ├── 2023-historical-baseline.md   # digit-leading stems are fine
    └── assets/                       # optional binary attachments
        └── high-graph-pressure/      # subdir name MUST equal the note_key it binds to
            └── pressure-diagram.png  # auto-attached to high-graph-pressure.md only
```

> **Per-note asset binding.** Files under `sources/assets/<note-key>/*` auto-attach to that note only. Files placed directly under `sources/assets/*` (no per-note subdir) are NOT auto-attached and the loader emits a warning naming the unattached files. To bind one asset to one note via frontmatter instead, set `assets:` in the note's YAML frontmatter — explicit binding always wins.

> **Filename rules.** Source note stems must match `^[a-z0-9][a-z0-9_-]*$` (lowercase, digits ok, hyphens/underscores ok). Files starting with `_` are intentionally skipped (use this for shared partials / fixtures). Files that don't match the regex (e.g. `README.md`, `Notes.md`) are skipped with a loader warning so a stray README doesn't take down the whole suite.

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
deltas across runs configured with different weight values.

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

A drop in `metric.recall_at_10.mean` between two runs configured with
different weights means the new weight set is over-deprioritizing. A
drop in `pass_rate` means a deterministic ranking expectation broke.
Compare runs side-by-side in the MLflow UI.
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

Targeted scenarios that surface measurable deltas across runs at
different FSFM weight settings.
"""
from pathlib import Path

from memex_eval.suite import (
    Suite,
    SuiteMetadata,
    SuiteSources,
    Scenario,
    InlineNote,    # only needed if any scenario uses inline_notes
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

#### 2.6 Smoke run

```bash
memex-eval suite run fsfm_weights \
  --mlflow-uri file://./mlruns
```

For comparing knob values, restart the server with each setting and re-run the suite — distinguish the runs via `--notes` and `--mlflow-run-name`.

#### 2.7 Tests

The framework's e2e test (`test_runner_e2e.py`) auto-discovers your suite. Add suite-specific unit tests in `packages/eval/tests/suite/test_<your_suite_name>.py` if you have non-trivial scenario logic worth pinning.

---

## 3. Nuts and bolts

> For complete CLI flags and per-command options see **CLI reference — every command and flag** above. This section covers the deeper mechanics (MLflow schema, redaction, reproducibility).

### 3.2 Knob overrides — `--override`

`--override <dotted.path>=<value>` is logged to MLflow as an `override.*` param **only**. The CLI does not restart the server with the new value — start the server with the desired knob set in env or YAML config, then run the suite. The override flag exists so the value lands in the run's params for diff-against-other-runs.

```bash
# Server already running with MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=0.5 in env
memex-eval suite run basic_extraction \
  --override server.memory.retrieval.reranking_mw_alpha=0.5
```

### 3.3 MLflow conventions

#### Experiment naming

| Use case | Experiment name |
|---|---|
| Default longitudinal tracking of a suite | `memex-suite-<name>-v<schema_version>` |
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
- `notes=<first-line preview>` — present only when `--notes` was passed; truncated to 240 chars with a `… (see run_notes.md artifact)` suffix when content was lost

#### Artifacts (queryable but not directly diff-able in UI)

- `run_result.json` — full `RunResult.model_dump_json()` including every per-scenario outcome
- `README.md` — frozen suite description at run time
- `sources/` — frozen copy of every source note + asset (so a 6-month-old run is reproducible from artifacts alone)
- `config_snapshot.json` — full `MemexConfig` dump from `GET /api/v1/system/config` with secrets redacted (see §3.7)
- `run_notes.md` — present only when `--notes` was passed; full body of the user-supplied change description (see §4.6)

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

## 4. Extending the framework

Everything in the framework that's a "closed set" of types — outcomes, setup actions, answer backends, suites — is registry-driven. Custom code registers itself once at import time; the core never has to learn about it. Three registries, one entry-point group.

### 4.1 Registering a custom outcome

Use `@register_outcome('<type-name>')` on a subclass of `ExpectedOutcomeBase`. The class must declare `type: Literal['<type-name>']` to match the discriminator. `score()` consumes a uniform `AgentAnswer` (whatever the active backend produced) and returns a `dict[str, float]`.

The example below asserts that a unit's `confidence` (a real field on `MemoryUnitDTO`) went up by at least `min_delta` after a setup action snapshotted the baseline. The same pattern applies to any per-unit numeric you can pull off `MemoryUnitDTO`.

```python
from typing import Literal
from memex_eval.suite import ExpectedOutcomeBase, register_outcome

@register_outcome('confidence_delta')
class ConfidenceDelta(ExpectedOutcomeBase):
    """Assert that a unit's confidence went up by ≥ min_delta after setup."""

    type: Literal['confidence_delta']
    target_keywords: list[str]
    min_delta: float

    def score(self, answer, scenario, *, context=None, **_kw):
        ctx = context or {}
        # The runner auto-prefixes setup-action context keys with the
        # action's registered name. ``snapshot_confidence`` publishes
        # ``baseline``, which becomes ``snapshot_confidence.baseline``.
        before = float(ctx.get('snapshot_confidence.baseline', 0.0))
        target = next(
            (u for u in answer.units
             if all(kw.lower() in (u.text or '').lower() for kw in self.target_keywords)),
            None,
        )
        if target is None:
            return {'confidence_delta': 0.0, 'pass': 0.0}
        after = float(getattr(target, 'confidence', 0.0) or 0.0)
        delta = after - before
        return {
            'confidence_delta': delta,
            'pass': 1.0 if delta >= self.min_delta else 0.0,
        }

    def metric_keys(self, top_k=None):
        return ['confidence_delta', 'pass']
```

Use it in any suite — by direct instance or by JSON dict (the validator coerces dicts via the registry):

```python
Scenario(
    id='positive_outcome_lifts_confidence',
    description='Recording a successful outcome should lift confidence on the matched unit.',
    query='Project Alpha lead',
    setup_actions=[
        SetupAction(kind='snapshot_confidence', search_query='Project Alpha lead'),
        SetupAction(kind='record_outcome', search_query='Project Alpha lead', success=True),
    ],
    expected=ConfidenceDelta(
        type='confidence_delta',
        target_keywords=['Sarah Chen'],
        min_delta=0.05,
    ),
)
```

`register_outcome` is strict: it raises if `<type-name>` is already registered, so plugins can't silently shadow built-ins. Use `replace_outcome('<type-name>')` for intentional overrides and `unregister_outcome('<type-name>')` to remove an entry (mainly for tests). Outcome names match `^[a-z][a-z0-9_]*$`. Register custom outcomes in any module that runs before `load_suite()` — typically the top of your suite's `__init__.py`.

### 4.2 Registering a custom setup action

Setup actions are registered side-effects that run before each scenario's query. Each action may publish a context dict that the outcome reads back via `score(context=...)` — this is the substrate for delta-style assertions.

```python
import abc
from memex_eval.suite import SetupActionHandler, register_setup_action

@register_setup_action('snapshot_confidence')
class SnapshotConfidence(SetupActionHandler):
    """Capture the confidence of units matching search_query before any
    other setup runs, so a downstream outcome can score the delta.

    Set ``required = True`` to abort the scenario with status='error' if
    the snapshot fails — protects delta outcomes from scoring against a
    phantom-zero baseline.
    """

    required = True

    async def run(self, api, vault_id, params):
        query = params.get('search_query')
        if not query:
            return None
        units = await api.search(query=query, limit=5, vault_ids=[vault_id])
        if not units:
            return None
        # Bare keys here — the runner auto-prefixes them with the handler
        # name, so the outcome reads ``snapshot_confidence.baseline`` and
        # ``snapshot_confidence.unit_id``. No collision risk between
        # handlers in the same scenario.
        return {
            'baseline': float(getattr(units[0], 'confidence', 0.0) or 0.0),
            'unit_id': str(units[0].id),
        }
```

`SetupAction` allows arbitrary extra fields (`extra='allow'` on the model), so custom actions can carry whatever params they need:

```python
SetupAction.model_validate({
    'kind': 'snapshot_confidence',
    'search_query': 'Project Alpha lead',
    'window_s': 30,            # custom param — passed through to params dict
})
```

If the action doesn't need to publish anything, return `None`. If it raises, the runner catches the exception, records `{'kind': ..., 'error': ...}` in `context['_setup_failures']`, and continues — *unless* the handler sets `required = True`, in which case the scenario short-circuits to `status='error'` and **no further actions in the same scenario are executed** (so a failed snapshot can't be followed by a record-outcome that contaminates the vault). Delta-style outcomes should mark their snapshot handlers as `required` to refuse scoring against a missing baseline.

Like outcomes, `register_setup_action` is strict on collisions; use `replace_setup_action('<name>')` for explicit overrides and `unregister_setup_action('<name>')` to remove an entry (mainly for tests). Names match `^[a-z][a-z0-9_]*$`.

### 4.3 Registering a custom answer backend

`@register_backend('<name>')` on a subclass of `AnswerBackend`. Backends produce a uniform `AgentAnswer` so outcomes don't care whether the answer came from the API, Claude Code, Hermes, or some other agent runtime.

```python
from memex_eval.suite import AgentAnswer, AnswerBackend, register_backend

@register_backend('my-agent')
class MyAgentBackend(AnswerBackend):
    async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
        # Drive your agent however you want, then map its output:
        text, tool_calls, retrieved_unit_ids = await my_agent_runtime(scenario.query)
        return AgentAnswer(
            answer_text=text,
            tool_calls=tool_calls,
            retrieved_unit_ids=retrieved_unit_ids,
            backend_name=self.name,
        )
```

Pick the backend per-suite via `SuiteMetadata.default_answer_mode='my-agent'`, or per-scenario via `Scenario.answer_mode='my-agent'`.

`register_backend` is strict on collisions; use `replace_backend('<name>')` for explicit overrides and `unregister_backend('<name>')` for tests. Backend names match `^[a-z][a-z0-9_-]*$` (the hyphen variant exists so `claude-code` keeps validating).

### 4.4 Shipping suites as a separate pip package (entry-point plugin)

Suites can live outside this repo. Drop `SUITE: Suite` in any module of your package and declare it as an entry point:

```toml
# pyproject.toml of your suite package (e.g. memex-eval-suites-acme)
[project.entry-points."memex_eval.suites"]
acme_retrieval = "acme_eval_suites.acme_retrieval"
acme_compliance = "acme_eval_suites.acme_compliance"
```

After `pip install memex-eval-suites-acme`:

```bash
memex-eval suite list                  # acme_retrieval and acme_compliance show up
memex-eval suite run acme_retrieval    # runs your plugin suite
memex-eval suite history acme_retrieval --metric metric.recall_at_10.mean
```

Resolution order is: built-in subpackage → entry-point plugin → filesystem path. Built-in names take priority (a plugin can't shadow `basic_extraction`). Plugins should also register any custom outcomes / setup actions / backends in the imported module's top-level body, so a single import wires everything up.

### 4.5 Ad-hoc filesystem suites (no install required)

For one-off experiments, point the CLI at any directory exporting `SUITE: Suite`:

```bash
memex-eval suite run /home/jasper/experiments/my_one_off_suite
memex-eval suite validate ./local_suite
```

The loader switches on the argument shape: contains `/` or starts with `.` → filesystem load; bare name → built-in lookup → entry-point plugin lookup.

### 4.6 Recording change context — `--notes`

A run's results are only interpretable in context: which version of memex, which knobs, what changed in the code since the last comparable run. The framework auto-captures the easy half (`git.sha`, `memex.version`, `suite.sources_hash`, full config snapshot, etc.) but the human "what did I just change and why" is on you.

Pass `--notes "<text>"` (or `--notes-file <path>` for longer write-ups) on `suite run`:

```bash
memex-eval suite run basic_extraction \
  --notes "Bumped reranking_mw_alpha 0.0→0.3 to test FSFM weight sensitivity. PR #143."
```

What gets logged:

- `RunResult.notes` — the full text on the run-result object (and the `run_result.json` artifact).
- MLflow artifact `run_notes.md` — the full text as a separate file, viewable in the MLflow UI without diving into JSON (when MLflow is configured).
- MLflow tag `notes` — the first line, truncated to 240 chars, with a `… (see run_notes.md artifact)` suffix when content was lost. Use this for filtering in the MLflow UI. A search like `tags.notes LIKE "%mw_alpha%"` finds every run that touched that knob — note that `LIKE` only works with the SQL tracking backend (Postgres/MySQL/SQLite); the file-store backend supports only exact `=` / `!=` matches on tags, so put the most-searchable keyword on the first line.

Comparing two runs months apart, the notes are usually how you remember what each one was actually testing.

Convention: lead with what changed, then *why*, then a PR or issue link. The first line is the searchable tag; everything after it is for the future-you who's trying to understand why a metric moved.

### 4.7 Isolating registry mutation in tests — `isolated_registries()`

Outcome / setup-action / backend registries are process-globals. Tests that register custom entries (or plugin packages that swap in alternative built-ins) should run inside `isolated_registries()` so they don't leak across runs:

```python
from typing import Literal
from memex_eval.suite import isolated_registries, register_outcome, ExpectedOutcomeBase

def test_my_outcome():
    with isolated_registries():
        @register_outcome('my_test_outcome')
        class MyTest(ExpectedOutcomeBase):
            type: Literal['my_test_outcome']
            def score(self, answer, scenario, **_kw):
                return {'pass': 1.0}
            def metric_keys(self, top_k=None):
                return ['pass']
        # ...test body...
    # registry restored on exit
```

The framework's own test suite uses an autouse pytest fixture that does the same; reach for `isolated_registries()` in any external test file or notebook that registers framework extensions.

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
# Restart your server with each value, then run the suite. Distinguish the
# runs via --notes and --mlflow-run-name; compare them in the MLflow UI.
for v in 0.0 0.1 0.2 0.3 0.5 0.7; do
  MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA=$v \
    memex restart   # or your usual server-restart command
  memex-eval suite run basic_extraction \
    --override server.memory.retrieval.reranking_mw_alpha=$v \
    --notes "mw_alpha=$v" \
    --mlflow-run-name "mw_alpha-$v"
done

# Open MLflow → filter experiment → compare runs → plot metric.recall_at_10.mean vs override.*
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
| `MLflowNotInstalled` at import time | mlflow extra not installed | `uv add memex-eval[mlflow]` |
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
