# Memex Memory Provider

Long-term memory with knowledge graph, entity resolution, multi-strategy retrieval (TEMPR), survey decomposition, and session briefings.

## Requirements

- Running Memex server (`memex server start -d`, default `http://127.0.0.1:8000`)
- Memex CLI with an initialized vault (`memex config init` + `memex vault create`)

## Setup

```bash
hermes memory setup    # select "memex"
```

Or manually:

```bash
hermes config set memory.provider memex
```

Config lives at `$HERMES_HOME/memex/config.json`. Secrets (API key) go to `$HERMES_HOME/.env`.

## Tools

- `memex_recall` — memory-unit search (facts, observations, events)
- `memex_retrieve_notes` — whole-note search
- `memex_survey` — broad query decomposition (server-side parallel fan-out)
- `memex_retain` — explicit ingest (supports session-note append)
- `memex_list_entities` — entity-graph search
- `memex_get_entity_mentions` — source facts for an entity
- `memex_get_entity_cooccurrences` — related entities

The plugin injects routing guidance into the system prompt — when to pair
recall with retrieve_notes, when to use survey, how to chain entity tools.
Tool descriptions themselves stay neutral.

## Memory modes

- `hybrid` (default) — briefing + prefetch + tools
- `context` — briefing + prefetch, no tools
- `tools` — tools only, no auto-inject

See the full README at https://github.com/JasperHG90/memex/blob/main/packages/hermes-plugin/README.md.
