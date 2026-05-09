# acme_corp suite

Source-doc-organized suite covering the **Acme Corp / TechCo Global** universe.
Consolidates eleven legacy scenario groups (basic extraction, temporal,
reflection, scale, assets, outcomes-MW, deprioritization, intent
classification, procedural KV, summarization, and vault isolation) into a
single suite where every scenario is grounded in a markdown source file
under `sources/`.

## Corpus

The corpus is a fictional company with three projects (Alpha, Beta, Zeta),
two products (Widget Pro, Widget Lite), an architecture overview, a data
platform deep dive (DataForge), eleven department docs from "TechCo Global"
(used for scale-stress retrieval), per-quarter business reviews, and three
intent-classification docs (permanent / durable / ephemeral). Two extra
documents (`project-gamma.md`, `project-delta.md`) carry `vault_name:`
frontmatter and live in distinct vaults to exercise multi-vault isolation.

## Components under test

- Fact extraction → memory units (basic + asset-bearing notes)
- Keyword + semantic + temporal + mental_model retrieval strategies
- Entity resolution (`Sarah Chen`, `Acme Corp`, etc.) and entity-type
  classification (`Person`, `Organization`)
- Reflection loop: top-N entities → reflect → mental_model search
- Memory worth ranking after `record_outcome` calls
- Deprioritization: `excluded_by_default` + `include_deprioritized=True`
  override
- Intent classification metadata (`permanent` / `durable` / `ephemeral`)
- KV store roundtrip (write via setup action, read via outcome)
- Entity summarization (`summarize_node`)
- Vault scoping: per-vault search + per-vault entity isolation

## Primary metrics

`suite.pass_rate` is the headline. Per-scenario `pass` is binary; the
ranking and excluded-by-default scenarios apply additional positional or
forbidden-keyword constraints.

## Knobs

The suite exercises:

- `server.memory.retrieval.reranking_mw_alpha`
- `server.memory.retrieval.reranking_recency_alpha`
- `server.memory.retrieval.reranking_temporal_alpha`
- `server.memory.entity.resolution_threshold`
- `server.memory.reflection.*` (via the `trigger_reflections` setup action)

## Backend support

The default `answer_mode` is `api` — these scenarios call `RemoteMemexAPI`
directly. Outcomes that consume API-shaped data (`UnitMetadataMatches`,
`KvRoundtrip`, `SummaryNonempty`, `EntityMentionContains`,
`EntityCooccurs`) are marked `expected_failure_modes=['claude-code',
'hermes']` because text-only agent backends cannot satisfy those shapes.
The keyword / ranking / LLM-judge scenarios run cleanly under all
backends.

## Setup actions

- `record_outcome` (project-zeta achievement vs incident ranking)
- `deprioritize` (Widget Lite EOL facts)
- `kv_write` (procedural KV roundtrip)
- `trigger_reflections` (mental-model retrieval)
