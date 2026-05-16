# retrieval_stability — ranking regression gate

Captures and verifies the top-k retrieval ranking for every query in the three retrieval corpora (`acme_corp`, `ai_research_lab`, `project_nexus`) in both `search_type='memory'` and `search_type='note'` modes. Drift in the captured ranking indicates a regression in one of:

- embedder
- cross-encoder reranker (model and/or scoring composition)
- RRF / MMR / pre-filter pipeline
- log-additive bounded-boost composition (memory-search path)
- note-rerank pipeline (`CE → sigmoid → RRF → cosine`; note-search path)

## Scope

| Path | Code | What changes break the gate |
|---|---|---|
| Memory rerank | `retrieval.engine._rerank_units` (log-additive bounded-boost composition) | Boost-factor edits, alpha tweaks, clip-knob changes, RRF/MMR rewrites |
| Note rerank | `retrieval.document_search.NoteSearchEngine._rerank_results` | Sigmoid normalisation, RRF over notes, cosine-aggregation changes |

The `composite_boost_log_clip` knob lives only in the memory-search path. Note-search scenarios catch an independent class of regression.

## Baseline anchoring

Captured baselines pin **note_keys** (filename stems), not unit IDs. Rationale:

- Note keys are stable across re-ingest and across machines.
- Unit IDs are `gen_random_uuid()` per ingest. The snapshot cache pins them per-machine, but cross-machine cache-miss breaks unit-id pinning. Note-key pinning survives.
- Trade-off: two paragraphs in the same note that flip rank inside the top-k are invisible to a note-key baseline. The strict unit-id mode is a planned follow-up that engages when the runner can report a snapshot-cache hit.

## Workflow

```bash
# First time on a clean checkout (or after intentional retrieval changes):
MEMEX_EVAL_CAPTURE_BASELINES=1 memex-eval suite run retrieval_stability

# Subsequent runs verify against the captured baselines:
memex-eval suite run retrieval_stability

# Or by search mode:
memex-eval suite run retrieval_stability --group retrieval_stability_memory
memex-eval suite run retrieval_stability --group retrieval_stability_notes
```

Capture mode is an env-gate on the standard `suite run` flow — no
separate CLI subcommand. The outcome's `score()` reads the env var; in
capture mode it persists the captured ranking to
`baselines/<scenario_id>.json` (with a `meta` block carrying
`schema_version`, `top_k`, `search_type`) and returns `pass=1.0`. In verify mode it loads the baseline, asserts
the meta matches the current scenario+config, then computes the RBO
score. Meta mismatch raises a clear error pointing the operator at
recapture, rather than silently comparing against a stale baseline.

## Anatomy

```
retrieval_stability/
├── __init__.py            # suite definition; loads baselines at import time
├── _outcomes.py           # @register_outcome('ranking_baseline_rbo')
├── _setup_actions.py      # @register_setup_action('seed_paragraphs_from_sources') — extension example
├── README.md              # this file
├── sources/               # corpus markdown (flattened across 3 corpora)
└── baselines/             # captured per-scenario JSONs
```

The setup action `seed_paragraphs_from_sources` is shipped as a documented extension point per the user directive that the framework support direct-DB seeding. The `retrieval_stability` suite itself uses normal ingest (which is what the gate is meant to monitor); the handler is exercised only by its own unit test against a testcontainer Postgres so it stays warm-tested.

## Limits

- ONNX inference is not bit-exact across CPU ISAs. A baseline captured on aarch64 will drift on x86_64 even with identical code. The intended deployment model is per-architecture baselines or a CI matrix; this PR ships baselines for one architecture and leaves the multi-arch story for follow-up.
- The reranker model revision is pinned at the `memex_core.memory.models.base` registry by HuggingFace tag, not commit SHA. A retag without a code change invalidates baselines silently. Pinning to commit SHAs is a separate follow-up.
- The bounded-boost composition's clip is **dormant under neutral metadata** (every unit's boost product stays inside `[exp(-L), exp(+L)]` for any L ≥ ~0.1). Pure ranking-stability scenarios won't exercise the clip arithmetic — that's covered by a dedicated core unit test, not by this suite.

## Framework-file edit

This PR is mostly suite-local (under `suites/retrieval_stability/*`), but ships one one-line edit to the framework at `packages/eval/src/memex_eval/suite/agents.py:419` — `getattr(n, 'id', '')` → `getattr(n, 'note_id', '')`. `NoteSearchResult` carries the result's ID on `note_id`, not `id`; the pre-existing line populated `answer.retrieved_unit_ids` with empty strings for every note-search scenario. The bug was latent because the pre-existing note-search scenarios in `acme_corp` all use `KeywordsPresent`, which iterates `answer.units` for text matches and never reads `retrieved_unit_ids`. `RankingBaselineRbo` is the first outcome that consumes `retrieved_unit_ids` for the note-search path, so it surfaces the bug. Fix shipped here rather than in a separate PR because nothing else exercises the code path.

## RBO floor and xfailed scenarios

- `rbo_floor=0.85` (not 0.99x). Observed natural ranking variance across capture and verify runs on the same machine, same code, falls into the 0.79–1.00 band — driven by LLM-extraction non-determinism producing slightly different units each ingest, which in turn flips near-tie rerank positions. 0.85 catches the kind of drift a model swap or composition rewrite would produce while absorbing the noise floor.
- Three scenarios are persistently in the 0.73–0.80 range and are marked `expected_failure_modes=['api']` in `__init__.py` (`_PERSISTENT_FAIL_SCENARIOS` set):
  - `acme_corp_company_core_values_permanent_principles_note`
  - `ai_research_lab_what_has_elena_vasquez_been_working_on_memory`
  - `ai_research_lab_what_has_elena_vasquez_been_working_on_note`
- The remaining 99 scenarios are gated strictly; occasional individual flicker around the 0.85 boundary is expected and is the cost of extraction non-determinism. Follow-up work: deterministic extraction (LLM temp=0 + seed propagation; ONNX kernel pinning) and a per-scenario floor profile derived from N capture runs.
