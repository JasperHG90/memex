# Agent Integration Suite

Tests how well an LLM agent — Claude Code via MCP or Hermes via the
memex-hermes-plugin — uses memex's tool surface to answer questions
about a vault.

**Suite version: 2.0.0** (consolidated synthesis layer; was 1.0.0 = 3-scenario smoke).

## Scope

Two layers, 38 scenarios across 10 groups:

| Group | Scenarios | What it covers |
|---|---|---|
| `smoke` | 3 | Plugin loads, finds a basic fact, calls a search tool |
| `triage` | 3 | Picks relevant from many; multi-hop synthesis |
| `temporal` | 3 | Recency-aware ranking; supersession |
| `entity` | 3 | Name-variant resolution; entity-mention surfacing |
| `survey` | 2 | Comprehensive vault-wide views |
| `faithfulness` | 2 | Citation discipline; honest refusal |
| `navigation` | 2 | Page-index walking on large notes |
| `feedback` | 4 | record_outcome / deprioritize routing; clarification under ambiguity |
| `kv` | 12 | Loose-phrased writes per namespace + hard wake-word triggers + TTL |
| `lifecycle` | 4 | Append-vs-add; xfail tripwire for `memex_delete_assets` (Hermes-gap) |

The xfail scenario under `lifecycle` (`asset_lifecycle_detach`) is a
tripwire for a plugin gap that exists in MCP but not in the Hermes
tool list. It's declared `expected_failure_modes=['hermes']`. Under
`--answer-mode claude-code` (MCP has it) it should pass normally. When
the Hermes plugin ships it, it flips from `xfail` to `xpass` → "the
constraint embedded here is wrong / now stale" → time to remove the
expected_failure_mode and reshape the scenario.

### KV group breakdown — loose vs hard variants

The KV group splits into **two register**: loose-phrased natural queries
(the agent must infer the namespace from scope cues) and hard wake-word
triggers (the agent must execute the matching `memex_kv_*` call
verbatim, bypassing any routing reasoning).

| Scenario id | Register | Query shape | Asserts |
|---|---|---|---|
| `kv_writes_project_preference` | loose | "Remember this for future sessions: we use 7-character indentation in this repo (unusual but deliberate)." | `memex_kv_write` with key matching `^project:.+` |
| `kv_writes_user_preference` | loose | "Remember about me: I prefer Neovim …" | key `^user:.+` |
| `kv_writes_global_convention` | loose | "Remember this company-wide standard: we standardise on Python 3.12 …" | key `^global:.+` |
| `kv_writes_app_setting` | loose | "Remember this for whenever I use Claude Code: dark theme …" | key `^app:.+` |
| `kv_retrieves_convention` | loose | "What's our indentation convention in this repo?" | keywords `[7]` in answer (depends on `kv_writes_project_preference`) |
| `kv_wakeword_store_user` | hard | `Store in KV: user:editor=Neovim` | key matches `^user:editor$` (verbatim) |
| `kv_wakeword_store_project` | hard | `Store in KV: project:eval-suite:lang=Python 3.12` | key `^project:eval-suite:lang$` |
| `kv_wakeword_store_global` | hard | `Store in KV: global:lang_min=Python 3.12` | key `^global:lang_min$` |
| `kv_wakeword_store_app` | hard | `Store in KV: app:claude-code:theme=dark` | key `^app:claude-code:theme$` |
| `kv_wakeword_kv_get` | hard | `KV: get user:editor` | `memex_kv_get` with key `^user:editor$` (depends) |
| `kv_wakeword_kv_search` | hard | `KV: search editor preference` | `memex_kv_search` (depends) |
| `kv_wakeword_store_with_ttl` | hard | "Store in KV: user:current_focus=ticket-456 (expires in 1 hour, ttl_seconds=3600)" | `memex_kv_write` with `ttl_seconds` matching `^[1-9]\d*$` |

**Loose** variants may fail (they exercise the namespace-by-scope-cue
heuristics; sonnet's been ~0.90 on these, GLM ~0.95). **Hard** variants
must pass — the wake words bypass reasoning. If a hard scenario fails,
the agent is ignoring the imperative trigger documented in
`agent_surface.RETRIEVAL_ROUTING`, which is a routing-policy regression.

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

- `GOOGLE_API_KEY` — for the LLM judge (`gemini/gemini-3.1-flash-lite-preview`)
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

19 scenarios carry `replicates_override=1`:
- 16 mutating writers (split across `feedback`, `kv`, `lifecycle` —
  every loose-phrased and hard-wake-word KV write, every
  record_outcome / deprioritize / restore exerciser, every append /
  add lifecycle scenario, and the xfail tripwire whose target tool
  mutates when present in MCP); plus
- 3 read-after-write scenarios that depend on mutating writers
  (`kv_retrieves_convention`, `kv_wakeword_kv_get`,
  `kv_wakeword_kv_search`). Their pass requires the prior write's
  state to be present, so re-running them after a single write would
  cross-contaminate.

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

### Generating the corpus (first run)

Every suite needs the source notes ingested + extracted before scenarios
can be graded. The snapshot cache automates "run extraction once, dump,
import":

```bash
# First run (no cache): full ingest + extraction. ~3–6 minutes.
uv run memex-eval suite run agent_integration --from-snapshot auto

# Subsequent runs: import only. ~5–15 seconds before scenarios start.
uv run memex-eval suite run agent_integration --from-snapshot auto
```

Cache root: `~/.cache/memex-eval/<suite_name>-<sources_hash>/`
(override with `--snapshot-cache-dir` or `MEMEX_EVAL_SNAPSHOT_ROOT`).
Force re-extraction after extraction-affecting code changes:

```bash
uv run memex-eval suite run agent_integration --from-snapshot auto --reingest
```

If the alembic head moves (e.g. after a schema migration), the cached
snapshot is rejected with `SnapshotImportRefused` — clear the matching
cache directory and re-run. The framework-level walkthrough lives in
`docs/how-to/evaluation-suite.md` § "Snapshot import — skip extraction
on reruns".

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
replicates × 22 non-mutating scenarios are still ~±15pp on per-scenario
binary outcomes — pair with a baseline-model comparison for
confident calls.

Recent baseline (single replicate, 38-scenario suite, 2026-05-16,
post procedure-MW rip-out + wake-word/TTL scenarios + fresh DB):

| Backend | pass_rate | Failed scenarios | Cost |
|---|---|---|---|
| `hermes` (`glm-5.1:cloud`) | **1.0000** (37/37 + 1 xfail) | — | $0.01 (judge only; agent tokens uncosted) |
| `claude-code` (`claude-sonnet-4-6`) | **0.8421** (32/38) | `agent_keywords_in_answer`, `entity_mentions_enumeration`, `feedback_clarifies_under_ambiguity`, `kv_retrieves_convention`, `temporal_superseded_handling`, `triage_picks_relevant_from_many` | $4.16 |
| `ollama-claude` (GLM via Claude CLI) | **0.9474** (36/38) | `kv_writes_app_setting`, `kv_writes_user_preference` (both **loose** KV variants — hard wake-word variants all pass) | $27.40 |

Observations:
- All 7 hard wake-word + TTL scenarios pass on hermes and ollama-claude.
  Claude-code passes all 7 too — the wake words are sticky.
- Loose KV variants are the only failures on ollama-claude. Claude-code's
  failures spread across smoke / entity / feedback / kv / temporal /
  triage — single-replicate noise on borderline LLM-judge thresholds,
  not a systemic gap.
- Claude-code drift between adjacent runs (0.94 → 0.84 on the same
  build) is the CI band at single-replicate × 38 scenarios. Use
  `--replicates 5` minimum for ranking calls; pre-wake-word the
  per-scenario CI band was ±20pp.

## Cost (Hermes + GLM-5.1, 5 replicates × 38 scenarios)

- Agent: ~$2.50 (4M tokens × $0.60/MTok GLM-5.1 cloud).
- Judge: ~$0.06 (200k tokens × ~$0.30/MTok Gemini Flash 3).
- **Single-model total: ~$2.55.**
- Two-model comparison: ~$5.10.

Override `EVAL_JUDGE_MODEL=anthropic/claude-haiku-4-5-20251001` to use
Anthropic judge instead — adds ~$0.90–1.30.

## Primary metrics

- `suite.pass_rate_non_mutating` (headline)
- `suite.pass_rate_all`
- `metric.graded_score.mean` (use this for model ranking)
- `cost.total_usd`

## Components under test

- `packages/hermes-plugin/src/memex_hermes_plugin/` — Hermes tools + provider
- `packages/mcp/` — MCP tool registration + descriptions
- `packages/core/src/memex_core/server/` — request handlers
