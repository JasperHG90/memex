# Agent Integration Suite

Tests how well an LLM agent — Claude Code via MCP or Hermes via the
memex-hermes-plugin — uses memex's tool surface to answer questions
about a vault.

**Suite version: 2.0.0** (consolidated synthesis layer; was 1.0.0 = 3-scenario smoke).

## Scope

Two layers, 26 scenarios across 10 groups:

1. **Smoke** (3 scenarios, group=`smoke`) — verifies the agent loads the
   plugin, finds a basic fact, calls a search tool. Inherited from 1.0.0.
2. **Tool-surface synthesis** (20 active + 1 xfail tripwire) —
   measures synthesis quality across `triage`, `temporal`, `entity`,
   `survey`, `faithfulness`, `navigation`, `feedback`, `kv`,
   `lifecycle`. The xfail scenario under `lifecycle` is a tripwire
   for a plugin gap that exists in MCP but not in the Hermes tool list:
   - `memex_delete_assets`

   It's declared `expected_failure_modes=['hermes']`. Under
   `--answer-mode claude-code` (MCP has it) it should pass normally.
   When the Hermes plugin ships it, it flips from `xfail` to `xpass`
   → "the constraint embedded here is wrong / now stale" → time to
   remove the expected_failure_mode and reshape the scenario.

## Why this matters

Internal suites test memex's API directly. This suite tests the
**agent-facing surface**. Tool description regressions, plugin
provider regressions, prompt-template regressions, and synthesis
regressions all surface here.

## Backends

- `--answer-mode hermes` (**default**) — Hermes agent + memex-hermes-plugin
  in-process. Plugin auto-symlinked into a temp `HERMES_HOME`.
- `--answer-mode claude-code` — spawns `claude` CLI as a subagent with
  `.mcp.json` pointing at the eval vault.
- `--answer-mode api` — direct REST; no agent. `agent_calls_memex_search`
  and every `ToolCall*` outcome cannot pass under this mode. Useful
  only for plumbing sanity checks.

Custom backends register via `@register_backend(...)`; see
`packages/eval/src/memex_eval/suite/agents.py` for the protocol.

## Setup

```bash
uv sync --extra hermes --group hermes-integration
```

### Required env vars

- `GOOGLE_API_KEY` — for the LLM judge (`gemini/gemini-3-flash-preview`)
- One of (depending on agent backend + model):
  - `OLLAMA_API_KEY` — Hermes mode with default `glm-5.1:cloud`
  - `ANTHROPIC_API_KEY` — Claude Code mode, OR Hermes mode with `anthropic/*`
  - `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `HERMES_API_KEY` — per model prefix

### Optional

- `EVAL_JUDGE_MODEL` — override judge (e.g. `anthropic/claude-haiku-4-5-20251001`)
- `HERMES_MODEL` — override the Hermes agent model

## Replicates

- **Iteration:** `--replicates 5` (minimum for the binary pass-rate
  signal to carry past noise).
- **Release-gate:** `--replicates 10`.

8 scenarios carry `replicates_override=1` — the 7 mutating scenarios
(4 agent-driven writers: `feedback_records_success`,
`feedback_deprioritize_obsolete`, `kv_writes_preference`,
`lifecycle_append_meeting`; plus the 3 xfail tripwires under `review`
and `lifecycle` whose target tools mutate when present in MCP) plus
`kv_retrieves_convention`, which reads state stamped by
`kv_writes_preference` and must collapse to N=1 to stay aligned.

The first replicate's write changes vault state; subsequent replicates
would see the post-write state. The suite splits the headline rate
accordingly:

- `suite.pass_rate_all` — every scenario.
- `suite.pass_rate_non_mutating` — excludes the N=1 mutating scenarios.
  Headline rate; CI bands tightest here.

If a `scenario.<id>.pass_rate` key is missing in MLflow, the scenario
was fully skipped or every replicate errored — check `count.errored`.

## Run modes

- `--from-snapshot auto` — **supported**. Mutations don't poison the
  cache; cache is read-only-imported into a fresh vault every run.
- `--keep-vault <label>` — a fresh run that persists the vault for later
  reuse. Mutating scenarios and xfail tripwires run normally (the vault
  is being created, not reused).
- `--reuse-vault <label>` — mutating scenarios and xfail tripwires skip
  with `skip_reason='mutating_under_reuse_vault'`. To verify plugin-gap
  closure (xfail → xpass), run WITHOUT `--reuse-vault`.

## Running

```bash
# Single model (Hermes + glm-5.1:cloud default)
uv run memex-eval suite run agent_integration --replicates 5 --from-snapshot auto

# Two-model comparison
HERMES_MODEL=glm-5.1:cloud \
  uv run memex-eval suite run agent_integration --replicates 5

HERMES_MODEL=anthropic/claude-haiku-4-5-20251001 \
  uv run memex-eval suite run agent_integration --replicates 5

# Claude Code via MCP
uv run memex-eval suite run agent_integration \
  --answer-mode claude-code --replicates 5
```

## Reading results

For model ranking, prefer the continuous
**`metric.graded_score.mean`** over the binary `suite.pass_rate_*`.
Binary pass-rate at 0.5 threshold conflates "almost right" with
"almost wrong"; the graded score keeps the gradient. CI bands at 5
replicates × 20 active scenarios are still ~±20pp on per-scenario
binary outcomes — pair with a baseline-model comparison for
confident calls.

## Cost (Hermes + GLM-5.1, 5 replicates)

- Agent: ~$1.80 (3M tokens × $0.60/MTok GLM-5.1 cloud).
- Judge: ~$0.04 (140k tokens × ~$0.30/MTok Gemini Flash 3).
- **Single-model total: ~$1.85.**
- Two-model comparison: ~$3.70.

Override `EVAL_JUDGE_MODEL=anthropic/claude-haiku-4-5-20251001` to use
Anthropic judge instead — adds ~$0.70–1.00.

## Primary metrics

- `suite.pass_rate_non_mutating` (headline)
- `suite.pass_rate_all`
- `metric.graded_score.mean` (use this for model ranking)
- `cost.total_usd`

## Components under test

- `packages/hermes-plugin/src/memex_hermes_plugin/` — Hermes tools + provider
- `packages/mcp/` — MCP tool registration + descriptions
- `packages/core/src/memex_core/server/` — request handlers
