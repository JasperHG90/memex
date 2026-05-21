# Evaluation results

> **Empirical caveat.** Every default in Memex was set from literature precedent and rules of thumb, then validated against the suites below. The suites are too small to *tune* those defaults — they catch regressions, not optimums. Read every number on this page through that lens.
>
> Sensitivity sweeps, Memory Worth calibration, and per-signal ablation deltas are pending work tracked in the design doc and in `BACKLOG.md`. See [Explanation: how Memex is evaluated](../explanation/how-memex-is-evaluated.md) for the longer version.

This page is a lookup of the latest measured numbers for each evaluation suite, plus the most recent external benchmark run. The internal suites live under `packages/eval/src/memex_eval/suites/`; their READMEs hold the per-suite write-up. Numbers shown here track those READMEs.

## Internal suite index

One row per suite. "Scope" names the components the suite gates against. "Scenarios" is the count of `suite.register(...)` calls in the suite's `__init__.py`. "Pass rate" and "Last run" come from the suite's latest committed baseline.

| Suite | Scope | Scenarios | Pass rate | Last run |
|---|---|---:|---:|---|
| `acme_corp` | Extraction, retrieval strategies, entity resolution, reflection, MW outcomes, deprioritization, intent classification, KV roundtrip, vault isolation | 40 | 1.0000 (40 / 40) | 2026-05-16 |
| `ai_research_lab` | Entity resolution edge cases (abbreviated names, titles), graph cooccurrence, cross-document links | 9 | 1.0000 (9 / 9) | 2026-05-16 |
| `project_nexus` | Contradiction detection, supersession-aware ranking, V1 lint pipeline, surprise-gated LLM lint, inline-note feature | 11 | 1.0000 (11 / 11) | 2026-05-16 |
| `agent_integration` | Hermes + Claude Code agent surface: triage, synthesis, temporal, entity, survey, faithfulness, navigation, feedback discipline, KV namespace routing, lifecycle choice | 39 | 1.0000 (h x glm-5.1) / 1.0000 (cc x opus-4.7) | 2026-05-18 |
| `retrieval_stability` | Top-k ranking regression: cross-encoder rerank, RRF, MMR, log-additive bounded-boost composition, note-rerank pipeline | 100 | 0.9700 (97 / 100) | 2026-05-18 |

Counts cited inline: `acme_corp` <code-ref path="packages/eval/src/memex_eval/suites/acme_corp/__init__.py" lines="111-742" />, `agent_integration` <code-ref path="packages/eval/src/memex_eval/suites/agent_integration/__init__.py" lines="126-1140" />, `ai_research_lab` <code-ref path="packages/eval/src/memex_eval/suites/ai_research_lab/__init__.py" lines="48-160" />, `project_nexus` <code-ref path="packages/eval/src/memex_eval/suites/project_nexus/__init__.py" lines="51-260" />, `retrieval_stability` <code-ref path="packages/eval/src/memex_eval/suites/retrieval_stability/__init__.py" lines="345-360" />.

---

## `acme_corp`

Source-document-organised suite covering the Acme Corp / TechCo Global universe. Eleven legacy scenario groups consolidated into one corpus where every scenario is grounded in a markdown source.

**Scope** — fact extraction, the four retrieval strategies (keyword, semantic, temporal, mental_model), entity resolution and type classification, the reflection loop, Memory Worth ranking after `record_outcome`, deprioritization with the `include_deprioritized=True` override, intent metadata, KV roundtrip, entity summarization, vault isolation.

| Field | Value |
|---|---|
| Scenarios | 40 |
| Headline metric | `suite.pass_rate` |
| Last pass rate | 1.0000 (40 / 40) |
| Last run | 2026-05-16, single replicate, fresh DB + freshly built snapshot |
| Cost | $0.004 (judge only — `api` backend, no agent cost) |
| Source | `packages/eval/src/memex_eval/suites/acme_corp/README.md` |

**Knobs exercised**: `server.memory.retrieval.reranking_mw_alpha`, `server.memory.retrieval.reranking_recency_alpha`, `server.memory.retrieval.reranking_temporal_alpha`, `server.memory.entity.resolution_threshold`, `server.memory.reflection.*`.

**Backend defaults**: `api` (direct `RemoteMemexAPI`). Scenarios that consume API-shaped data (`UnitMetadataMatches`, `KvRoundtrip`, `SummaryNonempty`, `EntityMentionContains`, `EntityCooccurs`) declare `expected_failure_modes=['claude-code', 'hermes']` because text-only agent backends cannot return those shapes.

---

## `ai_research_lab`

Verifies entity resolution, graph cooccurrence, and edge-case name handling across AI/NLP and quantum-computing research documents.

**Scope** — fuzzy name matching across diacritics, titles (`Dr. Amara Osei` <-> `Amara Osei`), and abbreviated first names (`J. Rodriguez` <-> `Juan Rodriguez`); cross-document cooccurrence links (Rodriguez <-> Osei across two papers).

| Field | Value |
|---|---|
| Scenarios | 9 |
| Headline metric | `suite.pass_rate` |
| Last pass rate | 1.0000 (9 / 9) |
| Last run | 2026-05-16, single replicate, fresh DB + freshly built snapshot |
| Cost | $0 (deterministic; no judge, no agent) |
| Source | `packages/eval/src/memex_eval/suites/ai_research_lab/README.md` |

**Knob exercised**: `server.memory.entity.resolution_threshold` — the fuzzy-match cutoff. Set at 0.65 from literature precedent; not calibrated against a labelled benchmark.

**Coverage note**: the one cooccurrence-only scenario declares `expected_failure_modes=['claude-code', 'hermes']` because graph cooccurrence is a server-side relation that text-only agent backends cannot reproduce from tool output.

---

## `project_nexus`

Tests contradiction detection, supersession ranking, and the lint pipeline on fictional Project Nexus engineering documentation. Two shared notes describe the same tech stack at different points in time (January 2025 -> July 2025); a second pair (`api-version-alpha` / `api-version-beta`) introduces contradicting API versioning policies that exercise the surprise-gated LLM lint path. The last two scenarios use inline notes to layer a Q4 2025 CI/CD switch on top of the shared sources without polluting the earlier scenarios.

| Field | Value |
|---|---|
| Scenarios | 11 |
| Headline metric | `suite.pass_rate` |
| Secondary metric | `metric.graded_score.mean` (LLM-judge migration summary) |
| Last pass rate | 1.0000 (11 / 11) |
| Last run | 2026-05-16, single replicate, fresh DB + freshly built snapshot |
| Cost | $0.0005 (judge only — `api` backend, no agent) |
| Source | `packages/eval/src/memex_eval/suites/project_nexus/README.md` |

**Knobs exercised**: `server.memory.retrieval.confidence_alpha`, `server.lint.surprise_threshold`.

**Coverage note**: the lint scenarios declare `expected_failure_modes=['claude-code', 'hermes']` because the lint outcomes inspect server-side findings.

---

## `agent_integration`

Tests how well an LLM agent — Claude Code via MCP, Hermes via the `memex-hermes-plugin`, or a Claude CLI driven by Ollama — uses Memex's tool surface to answer questions about a vault. Internal suites test the API directly; this suite tests the agent-facing surface.

**Layers** — smoke (3), triage (3), temporal (3), entity (3), survey (2), faithfulness (2), navigation (2), feedback (4), KV (12), lifecycle (4). Suite version 2.0.0.

| Field | Value |
|---|---|
| Scenarios | 39 (incl. one xfail tripwire for the `memex_delete_assets` gap in the Hermes plugin) |
| Headline metric | `suite.pass_rate_non_mutating` |
| Secondary metric | `metric.graded_score.mean` (use this for model ranking) |
| Last run | 2026-05-18, single replicate, post-harness-expansion |
| Source | `packages/eval/src/memex_eval/suites/agent_integration/README.md` |

### Latest backend matrix (2026-05-18)

| Backend | Model | Pass rate | Tokens in | Tokens out |
|---|---|---:|---:|---:|
| `hermes` | `glm-5.1:cloud` | 1.0000 (38 / 38 + 1 xfail) | 2.80M | 21.9K |
| `hermes` | `gemma4:31b-cloud` | 0.8947 (35 / 39) | 2.69M | 14.7K |
| `claude-code` | `claude-opus-4-7` | 1.0000 (39 / 39) | 4.57M | 42.3K |
| `ollama-claude` | `glm-5.1:cloud` | 0.8462 (33 / 39) | 5.01M | 30.2K |

Suite-level `pass_rate` excludes xfail from the denominator. Single-replicate confidence bands at 39 scenarios sit at +-10 to 15 percentage points on `ollama-claude x glm-5.1` — re-runs on this configuration flip about half the failing scenarios. Use `--replicates 5` for ranking-grade calls.

### Stable failure modes on this run

- `feedback_clarifies_under_ambiguity` on `ollama-claude x glm`. The model picks `memex_recent_notes` for discovery despite the explicit prohibition, then fabricates a `record_outcome` target.
- `kv_writes_app_setting` and `kv_writes_project_preference` on `ollama-claude x glm`. The loose, natural-language KV prompts ("Remember whenever I use Claude Code…") need namespace inference from scope cues; the model collapses to `user:` or omits the qualifier entirely.
- Three lifecycle scenarios (`lifecycle_append_meeting`, `lifecycle_append_parent_remains_retrievable`, `lifecycle_archive_legacy_warehouse_note`) on `hermes x gemma4`. The model picks `memex_add_note` over `memex_append_note` / `memex_set_note_status`.

The seven hard-wake-word KV scenarios (`Store in KV: <key>=<value>`, `KV: get <key>`, `KV: search <query>`) pass on every backend — the imperative trigger bypasses routing reasoning end-to-end.

### Costs (Hermes + GLM-5.1, 5 replicates x 38 scenarios)

- Agent: ~$2.50 (4M tokens x $0.60/MTok).
- Judge: ~$0.06 (200k tokens x ~$0.30/MTok Gemini Flash 3).
- Single-model total: ~$2.55.
- Two-model comparison: ~$5.10.

Override `EVAL_JUDGE_MODEL=anthropic/claude-haiku-4-5-20251001` to use the Anthropic judge — adds ~$0.90 to $1.30.

---

## `retrieval_stability`

Captures and verifies the top-k retrieval ranking for every query across the three retrieval corpora (`acme_corp`, `ai_research_lab`, `project_nexus`) in both `search_type='memory'` and `search_type='note'` modes. Ranks are pinned by MemoryUnit and Note UUIDs; the suite ships a post-extraction DB snapshot so LLM extraction non-determinism does not appear as ranking drift.

| Field | Value |
|---|---|
| Scenarios | 100 |
| Omitted scenarios | 2 (one query: `acme_corp` / "quarterly business review results" in both memory and note modes — persistently RBO ~0.69, deferred until diagnosed) |
| Headline metric | RBO (Rank-Biased Overlap, p=0.9) >= 0.92 per scenario |
| Last pass rate | 0.9700 (97 / 100) |
| Last run | 2026-05-18, verify against shipped snapshot + baselines |
| Source | `packages/eval/src/memex_eval/suites/retrieval_stability/README.md` |

**Scope** — memory rerank (`retrieval.engine._rerank_units`) and note rerank (`retrieval.document_search.NoteSearchEngine._rerank_results`). The `composite_boost_log_clip` knob is exercised here; its sweep is run via `just sweep-clip-l`.

**RBO floor**: 0.92. With the snapshot eliminating LLM-extraction variance, most scenarios produce identical rankings across runs (RBO = 1.0 exactly). A small number flicker between 0.92 and 1.0 from residual ONNX kernel non-determinism (cross-encoder + embedding regeneration).

### Sub-floor scenarios on the latest run

| Scenario | RBO | Diagnosis |
|---|---:|---|
| `ai_research_lab_amara_osei_note` | ~0.39 | Only 2 truly relevant notes; positions 3-10 are noise-floor filler whose order flips freely. RBO over top-10 is the wrong gate for queries with fewer than `top_k` relevant docs. |
| Two float-tie cases | ~0.91 | Just below the 0.92 floor; ONNX CPU-kernel non-determinism on near-tie scores. |

Follow-up work tracked in the suite README: per-query `top_k`, plus recall@k and precision@k metrics anchored on a `relevant_doc_ids` list so filler-order drift is not scored.

### Snapshot determinism

The shipped snapshot (~1.8 MB, checked into the repo) preserves `Note.id`, `MemoryUnit.id`, `Chunk.id`, and `Entity.id` verbatim across import. Embeddings are *regenerated* from text at import time using the ONNX backend named in `manifest.json`. On the reference CPU ISA the regeneration is bit-exact; cross-architecture drift (aarch64 capture imported on x86_64 or vice versa) is out of scope for this suite.

---

## External benchmark: LoCoMo

LoCoMo is an external long-context conversational-memory benchmark. The numbers below come from a single run on 2026-03-22 against Claude Opus 4 via the Claude Code CLI, with Gemini 3 Flash as the judge. Source: `docs/reference/evaluation-report.md` (being retired into this page).

### Headline

| Metric | Value |
|---|---:|
| Questions scored | 36 (excl. 3 image-dependent, 11 adversarial reported separately) |
| Overall score (non-adversarial) | 0.986 |
| Perfect answers | 35 of 36 (97.2%) |
| Wrong answers | 0 (0.0%) |
| Total cost | $9.14 |
| Total duration | 37.4 min |

### By category

| Category | Count | Mean score | Perfect | Wrong |
|---|---:|---:|---:|---:|
| Single-Hop | 9 | 0.944 | 8 | 0 |
| Multi-Hop | 9 | 1.000 | 9 | 0 |
| Open Domain | 3 | 1.000 | 3 | 0 |
| Temporal | 15 | 1.000 | 15 | 0 |
| Adversarial (unweighted) | 11 | 0.727 | 8 | 3 |
| Non-adversarial overall | 36 | 0.986 | 35 | 0 |

### Adversarial scoring — why these are reported separately

LoCoMo's adversarial questions deliberately swap the subject: they ask about person A when the ground-truth answer pertains to person B. The expected behaviour is for the model to detect the swap.

Memex is a search tool. Asked "What instrument does Caroline play?" it searches Caroline's instruments, finds "acoustic guitar", and returns that. The benchmark expects "clarinet and violin" — Melanie's instruments. In all three failed adversarial cases, the retrieval system found correct facts for the *queried* person. The benchmark tests something outside a search tool's scope, so adversarial scores are excluded from the weighted overall.

### Retrieval efficiency

| Metric | Value |
|---|---:|
| Total tokens (all turns) | 5,024,120 |
| Retrieval tokens (Memex) | 386,035 |
| Retrieval share of total | 7.7% |
| Retrieval tokens / question (mean) | 7,804 |
| Retrieval tokens / question (median) | 4,910 |

### Retrieval by tool

| Tool | Tokens | Share | Calls | Calls / q |
|---|---:|---:|---:|---:|
| `memory_search` | 208,433 | 54.0% | 78 | 1.6 |
| `note_search` | 92,776 | 24.0% | 54 | 1.1 |
| `get_nodes` | 46,258 | 12.0% | 29 | 0.6 |
| `get_entity_mentions` | 15,829 | 4.1% | 5 | 0.1 |
| `get_page_indices` | 13,670 | 3.5% | 28 | 0.6 |
| `list_entities` | 7,775 | 2.0% | 28 | 0.6 |
| `get_entity_cooccurrences` | 1,260 | 0.3% | 2 | 0.0 |
| `read_note` | 34 | 0.0% | 1 | 0.0 |

### Retrieval paths the agent picked

| Pattern | Count | Share | Avg score | Avg tools | Avg duration | Avg cost |
|---|---:|---:|---:|---:|---:|---:|
| Two-stage | 19 | 38% | 1.00 | 2.0 | 27s | $0.10 |
| Two-stage + entity | 9 | 18% | 0.83 | 3.8 | 40s | $0.13 |
| Deep verification | 7 | 14% | 0.93 | 4.4 | 41s | $0.17 |
| Deep + entity | 7 | 14% | 0.79 | 6.1 | 54s | $0.23 |
| Simple + entity | 5 | 10% | 0.80 | 2.8 | 31s | $0.11 |
| Exhaustive | 3 | 6% | 0.67 | 21.7 | 181s | $0.87 |

The full per-question table, the path diagrams, and the per-failure analysis live in the prior `docs/reference/evaluation-report.md` file. That page is being retired into this one; the numbers above are the authoritative LoCoMo summary as of 2026-03-22 and have not been re-run against later builds.

---

## How to reproduce these numbers

```bash
# Internal suite — fastest case (api backend, snapshot cache).
uv run memex-eval suite run acme_corp --from-snapshot auto

# Agent-integration suite — Hermes + GLM, 5 replicates.
uv run memex-eval suite run agent_integration --from-snapshot auto --replicates 5

# Retrieval-stability gate — auto-imports the shipped snapshot.
uv run memex-eval suite run retrieval_stability
```

Each suite README under `packages/eval/src/memex_eval/suites/<name>/README.md` carries the full reproduction recipe, the cost breakdown, and the per-scenario notes. Numbers on this page are mirrored from those READMEs and the per-suite baseline files; when the suite runs again, the README updates first and this page follows.

## See also

- [Tutorial: Getting started](../tutorial/getting-started.md)
- [How-to: Run the evaluation suite](../how-to/evaluation-suite.md)
- [Reference: CLI commands](cli-commands.md)
- [Explanation: how Memex is evaluated](../explanation/how-memex-is-evaluated.md)
