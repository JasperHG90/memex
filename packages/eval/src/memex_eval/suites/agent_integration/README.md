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

Latest baseline (single replicate, 39-scenario suite, 2026-05-18,
post-harness-expansion — `CLAUDE_CODE_HARNESS` routing-discipline
constraints + `MEMEX_RECORD_OUTCOME_DESC` / `MEMEX_KV_WRITE_DESC`
trigger lexicons; see `## Backends`):

| Backend | Model | pass_rate | tokens in | tokens out |
|---|---|---|---|---|
| `hermes` | `glm-5.1:cloud` | **1.0000** (38/38 + 1 xfail) | 2.80M | 21.9K |
| `hermes` | `gemma4:31b-cloud` | **0.8947** (35/39, 4 fail + 1 error + 1 xfail) | 2.69M | 14.7K |
| `claude-code` | `claude-opus-4-7` | **1.0000** (39/39) | 4.57M | 42.3K |
| `ollama-claude` | `glm-5.1:cloud` | **0.8462** (33/39) | 5.01M | 30.2K |

> Suite grew 38→39 scenarios with `feedback_deprioritize_observation_400_recovery`.
> Suite-level `pass_rate` excludes xfail from the denominator.

Sampling-variance check — 6 oc×glm fails re-run on a second replicate
(2026-05-18 09:13): 3 flipped to pass (`asset_lifecycle_detach`,
`kv_writes_global_convention`, `kv_writes_user_preference`),
3 stayed failing (`feedback_clarifies_under_ambiguity`,
`kv_writes_project_preference`, `kv_writes_app_setting`). GLM-5.1 has
significant temperature-driven variance on routing-discipline scenarios;
multi-replicate runs recommended for ranking-grade calls.

Prior baseline (2026-05-17, 38-scenario suite, post-infra-fix,
pre-harness-expansion):

| Backend | Model | pass_rate |
|---|---|---|
| `hermes` | `glm-5.1:cloud` | 1.0000 (37/37 + 1 xfail) |
| `hermes` | `gemma4:31b-cloud` | 0.9737 (36/37 + 1 xfail) |
| `claude-code` | `claude-opus-4-7` | 0.9474 (36/38) |
| `claude-code` | `claude-sonnet-4-6` | 0.8947 (34/38) |
| `ollama-claude` | `glm-5.1:cloud` | 0.8947 (34/38) |
| `ollama-claude` | `gemma4:31b-cloud` | 0.7838 (29/37 + 1 errored) |

Net effect of harness expansion (cc×opus: +0.0526; oc×glm: −0.0485 on
2-run mean): the expanded `<critical_constraint>` blocks help bigger
models exploit the structure and pull smaller models toward the
example's concrete arm. Same harness, different model capacity.

Pre-infrastructure-fix (2026-05-16, retained for context — gap between
this and the next row of the same model is the env-leak the framework
carried until 2026-05-17):

| Backend | Model | pass_rate |
|---|---|---|
| `hermes` | `glm-5.1:cloud` | 1.0000 (37/37 + 1 xfail) |
| `claude-code` | `claude-sonnet-4-6` | 0.8421 (32/38) |
| `ollama-claude` | `glm-5.1:cloud` | 0.9474 (36/38) |

### Per-scenario × per-model failure matrix (2026-05-18, post-harness)

Only scenarios that failed on at least one backend listed (28 passed
on all 4 configs and are omitted). Legend: `.` pass, `F` fail,
`E` error (timeout / exception), `x` xfail.

| Scenario | h×glm | h×gem | cc×opus | oc×glm |
|---|---|---|---|---|
| `asset_lifecycle_detach` | x | x | . | **F** (flaky — passed on rerun) |
| `feedback_clarifies_under_ambiguity` | . | . | . | **F** (stable across 2 runs) |
| `feedback_deprioritize_observation_400_recovery` | . | **E** | . | . |
| `feedback_surfaces_candidate_notes` | . | **F** | . | . |
| `kv_writes_app_setting` | . | . | . | **F** (stable across 2 runs; was failing pre-harness too) |
| `kv_writes_global_convention` | . | . | . | **F** (flaky — passed on rerun) |
| `kv_writes_project_preference` | . | . | . | **F** (stable across 2 runs) |
| `kv_writes_user_preference` | . | . | . | **F** (flaky — passed on rerun) |
| `lifecycle_append_meeting` | . | **F** | . | . |
| `lifecycle_append_parent_remains_retrievable` | . | **F** | . | . |
| `lifecycle_archive_legacy_warehouse_note` | . | **F** | . | . |

### Error analysis (post-hoc, 2026-05-18)

Categorising the failures by the agent's actual tool sequence + final
answer (extracted from `session_log_text` in each run's `--output`
JSON):

**(A) Loose-KV namespace inference fragility on oc×glm** — 3 stable
fails + 2 flaky-now-flipped. The 4 `kv_writes_*` scenarios with loose,
natural-language phrasing ("Remember about me…", "Remember this for
whenever I use Claude Code…") require the agent to *infer* the right
namespace from scope cues. The hard-wakeword variants
(`Store in KV: user:editor=Neovim`) pass on every backend.

- Hermes×{GLM, Gemma}: all 4 loose-KV writes pass.
- Opus 4.7: all 4 loose-KV writes pass.
- GLM-via-CC: `kv_writes_app_setting` + `kv_writes_project_preference`
  are stable fails across 2 runs; `kv_writes_global_convention` +
  `kv_writes_user_preference` are flaky (1 run fail, next pass).
  Failure mode: picks wrong namespace (e.g. `user:preferences:claude_code_ui`
  when `app:` was correct) or omits the scope qualifier entirely.

**(B) `feedback_records_success` paired-write routing** — 0 fails now.
The expanded `MEMEX_RECORD_OUTCOME_DESC` trigger lexicon + JWT-rotation
WRONG/RIGHT example landed. All 4 backends now correctly route to
`memex_record_outcome(units=[{...verb:'helpful'...}])` instead of
`memex_add_note(...)`. Previously cost cc×sonnet + oc×glm; now clean.

**(C) `feedback_surfaces_candidate_notes` multi-candidate enumeration**
— 1 fail (h×gem only). The expanded `<critical_constraint name="list_shape_questions">`
block with the deploy-pipeline WRONG/RIGHT example fixed cc×opus and
oc×glm; h×gem still falls through to single-note narration.

**(D) `feedback_clarifies_under_ambiguity` on oc×glm** — 1 stable fail.
The user says "that worked" with no specific referent. GLM-via-CC:
1. `memex_recent_notes(limit=5)` — **prohibited** by harness rule
2. `memex_memory_search(query="issue that was resolved or fix that worked")`
3. `memex_record_outcome(units=[3 fabricated units])` marking Redis
   migration + trend alerts as `helpful`

Despite the `<critical_constraint name="clarify_under_ambiguity">` and
the `NEVER use memex_recent_notes for discovery` prohibition both being
present in `CLAUDE_CODE_HARNESS`, GLM ignores them and fabricates a
target. Opus + both Hermes ask the user to clarify. This is a
small-model attention failure on negative-imperative constraints.

**(E) Note-lifecycle distinction on h×gem** — 3 fails. Gemma chooses
`memex_add_note` instead of `memex_append_note` /
`memex_set_note_status` for `lifecycle_append_meeting`,
`lifecycle_append_parent_remains_retrievable`,
`lifecycle_archive_legacy_warehouse_note`. The note-lifecycle imperatives
in the universal `agent_surface` don't land on this model.

**(F) `feedback_deprioritize_observation_400_recovery` on h×gem** — 1
error (timeout). Agent loops on tool calls without converging within
the 300s subprocess cap. Likely an extended search→deprio→search cycle.

**(G) `asset_lifecycle_detach`** — flaky on oc×glm (passed on rerun).
Was xfail-on-Hermes throughout; only oc×glm flips between pass and fail.

### Headline observations (2026-05-18)

- **All 7 hard-wake-word KV/TTL scenarios pass on every backend.**
  Imperative triggers (`Store in KV: <k>=<v>`, `KV: get <k>`,
  `KV: search <q>`) bypass routing reasoning end-to-end.
- **cc×opus 4.7 hits 1.00**, up from 0.9474 pre-harness. The expanded
  `<critical_constraint>` blocks + WRONG/RIGHT examples close the prior
  gaps (cooccurrence-graph routing, multi-candidate enumeration,
  paired-write vs add_note).
- **h×glm-5.1 hits 1.00** (38/38 + 1 xfail). Hermes wraps the model in
  a tighter prompt loop than the CC subprocess; both GLM and Gemma
  measurably better under Hermes than via `claude` CLI.
- **oc×glm-5.1 has net regression of ~2 scenarios** vs pre-harness
  baseline. Re-run shows ~half were sampling-flaky; the stable fails
  are `feedback_clarifies_under_ambiguity` + 2 of 4 loose-KV scenarios.
  Same harness as cc×opus → over-specification trap: GLM-5.1 attends
  to the example's concrete arm (`app:claude-code:` from the JWT example)
  and misclassifies cues for other namespaces.
- **h×gem-4 lost 3 lifecycle scenarios** in the new run — note-lifecycle
  imperatives (`append_note` vs `add_note`, `set_note_status` for archive)
  not landing. Gemma defaults to `add_note` regardless of intent.
- Single-replicate × 39-scenario CI bands are still ±10–15pp on GLM via
  ollama-claude (confirmed by 3/6 rerun flips). Use `--replicates 3`
  minimum for oc×glm; `--replicates 5` for ranking-grade calls.

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
