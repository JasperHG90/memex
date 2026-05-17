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
- `--answer-mode ollama-claude` — same `claude` CLI but launched via
  `ollama launch claude` so the model is served by Ollama (default
  `glm-5.1:cloud`; override with `MEMEX_EVAL_OLLAMA_CLAUDE_MODEL`).
- `--answer-mode api` — direct REST; no agent. `agent_calls_memex_search`
  and every `ToolCall*` outcome cannot pass under this mode. Useful
  only for plumbing sanity checks.

Custom backends register via `@register_backend(...)`; see
`packages/eval/src/memex_eval/suite/agents.py` for the protocol.

### Backend infrastructure (`claude-code` / `ollama-claude`)

The CC subprocess shares two surfaces with the host: the bash hooks in
the `claude-code-plugin` (which call the `memex` CLI for vault
resolution, briefing, session-note persistence), and the MCP server
spun up via `.mcp.json`. Both must point at the **eval** server, not
the operator's personal memex. Three guardrails enforce this:

1. **Env override**: `MEMEX_SERVER_URL` is overridden in `child_env`
   (and `MEMEX_API_KEY` dropped) so the plugin's `memex` CLI hits the
   eval server. Without this the plugin's `memex_resolve_active_vault`
   silently queries the operator's server and concludes "No vault
   set", even though `.mcp.json` correctly binds the MCP layer.
2. **Per-suite stable workspace**: the backend caches one tmpdir per
   `vault_id` so every scenario in a suite run shares the same path,
   so the plugin's path-based `project_id` is stable across
   scenarios. Cross-scenario KV state (e.g. `kv_retrieves_convention`
   depending on `kv_writes_project_preference`) needs this.
3. **MCP default-vault binding**: `MEMEX_VAULT__ACTIVE=<vault_name>`
   is injected into `child_env` so agents that omit `vault_ids`
   (Sonnet routinely does; GLM/Opus thread it through) still hit the
   eval vault via MCP's `config.read_vaults` fallback.

Both `claude-code` and `ollama-claude` go through `ClaudeCodeBackend`,
so all three guardrails apply to both backends.

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

Recent baseline (single replicate, 38-scenario suite, 2026-05-17,
post infrastructure fixes — env-leak override, per-suite stable
project_id, MCP `MEMEX_VAULT__ACTIVE` default; see `## Backends`):

| Backend | Model | pass_rate | Cost |
|---|---|---|---|
| `hermes` | `glm-5.1:cloud` | **1.0000** (37/37 + 1 xfail) | $0.01 |
| `hermes` | `gemma4:31b-cloud` | **0.9737** (36/37 + 1 xfail) | $0.01 |
| `claude-code` | `claude-opus-4-7` | **0.9474** (36/38) | $9.60 |
| `claude-code` | `claude-sonnet-4-6` | **0.8947** (34/38) | $3.71 |
| `ollama-claude` | `glm-5.1:cloud` | **0.8947** (34/38) | $24.13 |
| `ollama-claude` | `gemma4:31b-cloud` | **0.7838** (29/37 + 1 errored) | $19.07 |

Pre-infrastructure-fix (2026-05-16, retained for context — gap between
this and the above row of the same model is the leak the eval framework
carried until 2026-05-17):

| Backend | Model | pass_rate |
|---|---|---|
| `hermes` | `glm-5.1:cloud` | 1.0000 (37/37 + 1 xfail) |
| `claude-code` | `claude-sonnet-4-6` | 0.8421 (32/38) |
| `ollama-claude` | `glm-5.1:cloud` | 0.9474 (36/38) |

### Per-scenario × per-model failure matrix

Only scenarios that failed on at least one backend listed (25 passed
on all 6 configs and are omitted). Legend: `.` pass, `F` fail,
`E` error (timeout / exception), `x` xfail.

| Scenario | h×glm | h×gem | cc×son | cc×opus | oc×glm | oc×gem |
|---|---|---|---|---|---|---|
| `agent_calls_memex_search` | . | **F** | . | . | . | . |
| `asset_lifecycle_detach` | x | x | . | . | . | . |
| `entity_cooccurrence_strongest` | . | . | . | **F** | . | **E** |
| `feedback_clarifies_under_ambiguity` | . | . | **F** | . | . | **F** |
| `feedback_records_success` | . | . | **F** | . | **F** | . |
| `feedback_surfaces_candidate_notes` | . | . | **F** | **F** | . | **F** |
| `kv_retrieves_convention` | . | . | . | . | . | **F** |
| `kv_writes_app_setting` | . | . | **F** | . | **F** | **F** |
| `kv_writes_global_convention` | . | . | . | . | . | **F** |
| `kv_writes_project_preference` | . | . | . | . | . | **F** |
| `kv_writes_user_preference` | . | . | . | . | **F** | **F** |
| `navigation_via_page_index` | . | . | . | . | **F** | . |
| `survey_broad_topic` | . | . | . | . | . | **F** |

### Error analysis (post-hoc)

Categorising the failures by the agent's actual tool sequence + final
answer (extracted from `session_log_text` in each run's `--output`
JSON):

**(A) Loose-KV namespace inference fragility** — 5 fails. The 4
`kv_writes_*` scenarios with loose, natural-language phrasing ("Remember
about me…", "Remember this for whenever I use Claude Code…") require
the agent to *infer* the right namespace from scope cues. The
hard-wakeword variants (`Store in KV: user:editor=Neovim`) pass on every
backend.

- Hermes×{GLM, Gemma}: all 4 loose-KV writes pass.
- Sonnet 4.6: `kv_writes_app_setting` fails (1/4).
- Opus 4.7: all 4 loose-KV writes pass.
- GLM-via-CC: `kv_writes_user_preference`, `kv_writes_app_setting` fail
  — sometimes picks wrong namespace (`user:preferences:claude_code_ui`
  when `app:` was correct), sometimes makes no tool call at all
  ("Vault loaded with 37 notes. Ready for your question.").
- **Gemma-via-CC fails 4/4**: makes ZERO tool calls and replies "I am
  ready to answer the evaluation questions against the vault. Please
  provide the question." Treats the "Remember about me…" intent as a
  setup statement, not a write trigger.

**(B) `feedback_records_success` paired-write routing** — 2 fails. The
scenario expects `memex_record_outcome(units=[{verb:'helpful'}])`. Some
models fall through to capturing the resolution as a *new note* via
`memex_add_note` / `memex_append_note`, never paired-writing on the
existing memory units.

- Sonnet 4.6: searches → finds Redis unit → calls `memex_append_note`
  with a "## Resolution confirmed" delta. Never `record_outcome`.
- GLM-via-CC: searches → calls `memex_add_note` ("title: Redis to
  in-process cache migration confirmed successful") + `memex_kv_write`.
  Never `record_outcome`.
- Opus 4.7, Gemma-via-CC, both Hermes: route correctly to
  `memex_record_outcome(units=[{...verb:'helpful'...}])`.

**(C) `feedback_surfaces_candidate_notes` multi-candidate enumeration**
— 3 fails. Scenario expects ≥2 candidate notes presented by title+date
so the user can pick. The agent's search returns the right notes; the
*answer format* is wrong (single note narrated in detail vs. a list).

- Sonnet, Opus, Gemma-via-CC all narrate one note ("The main note on
  the caching-layer bug is `incident-2025-08-redis` — Redis Cache
  Cascading Outage and System Recovery (August 14, 2025). Key points:
  …").
- GLM-via-CC and both Hermes enumerate multiple candidates.
- Note Opus fails here too — this is genuinely a model bias toward
  "answer the question directly" over "let the user pick", not just a
  Sonnet-only issue.

**(D) `feedback_clarifies_under_ambiguity`** — 2 fails. The user says
"that worked" with no specific referent (vault has multiple bug-fix
narratives). Scenario expects the agent to ask which fix the user
means before calling `record_outcome`.

- Sonnet, Gemma-via-CC guess a target and write the outcome.
- Opus, GLM-via-CC, both Hermes ask for clarification.

**(E) Cooccurrence-graph routing (`entity_cooccurrence_strongest`)** —
1 fail + 1 error.

- Opus 4.7 calls `memex_list_entities` 4x + `memex_note_search` +
  `memex_memory_search` + `memex_get_page_indices`. Does NOT call
  `memex_get_entity_cooccurrences`. Concludes "no individual is named
  by personal name; only the CTO recurs" — misses the graph-anchored
  cooccurrence the scenario expects.
- Gemma-via-CC hits the 300s subprocess timeout — loops on tool calls
  without converging.

**(F) Wrong-tool routing on broad questions (`agent_calls_memex_search`)**
— 1 fail (Hermes×Gemma). Scenario asserts the agent calls
`memex_memory_search` OR `memex_note_search` before answering. Gemma
reached for `memex_survey` instead. Real coverage — survey would be
fine in production — but the scenario was written before survey was a
mainstream option.

**(G) Page-index navigation (`navigation_via_page_index`)** — 1 fail
(GLM-via-CC). Scenario expects `memex_get_page_indices` on a large
note. GLM treated the question as content lookup + paired-write,
never opened the page index.

**(H) LLM-judge composite (`survey_broad_topic`)** — 1 fail
(Gemma-via-CC). Tool routing correct (called `memex_survey` + 2
searches + 2 `memex_read_note`), but the answer didn't meet the
judge's rubric for comprehensiveness.

**(I) Cascade (`kv_retrieves_convention`)** — 1 fail (Gemma-via-CC).
Depends on `kv_writes_project_preference` — which Gemma failed in
category (A). Even with the value present, Gemma doesn't route
preference questions to KV.

### Headline observations

- **All 7 hard-wake-word KV/TTL scenarios pass on every backend.**
  Imperative triggers (`Store in KV: <k>=<v>`, `KV: get <k>`,
  `KV: search <q>`) bypass routing reasoning end-to-end.
- **Hermes wraps the model in a tighter prompt loop** than the
  Claude Code subprocess — both GLM and Gemma do measurably better
  under Hermes than via `claude` CLI.
- **Opus 4.7 is the strongest claude-code variant.** Two failures,
  both routing-discipline (cooccurrence-graph, multi-candidate
  enumeration). Worth the $9.60 over Sonnet's $3.71 at single replicate
  if you need maximal coverage.
- **Sonnet 4.6 has 4 routing-discipline gaps** — loose-KV `app:`
  inference, paired-write vs add_note, clarification under ambiguity,
  multi-candidate enumeration. None of them are infrastructure now;
  they're prompt-side gaps in `agent_surface`.
- **Gemma-via-claude-code (8F + 1E) is brittle in this harness.** Most
  failures are "agent issues zero tool calls" — Gemma seems to treat
  many natural-language scaffolds as setup statements rather than
  action triggers. Hermes×Gemma works much better (1F) — the agent
  loop matters more than the model for this corpus.
- Claude-code drift between adjacent runs at single-replicate × 38
  scenarios was historically ±20pp; with the infrastructure fixes the
  per-scenario CI band tightens because the "no vault set" fallback no
  longer randomly damages unrelated scenarios. Use `--replicates 5`
  minimum for ranking calls.

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
