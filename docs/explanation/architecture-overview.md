# Architecture Overview

This document describes Memex's system architecture, package structure, and database schema. For detailed explanations of each subsystem, see the linked pages.

## System Architecture

Memex uses a four-layer design: client interfaces delegate to a facade API, which routes through domain services and memory engines, all backed by a unified storage layer.

```mermaid
graph TD
    subgraph CLIENT["Client Layer"]
        CLI["Typer CLI<br/>(memex_cli)"]
        MCP["MCP Server<br/>(memex_mcp)"]
        REST["FastAPI Server<br/>(memex_core.server)"]
    end

    CLI --> API
    MCP --> API
    REST --> API

    API["MemexAPI Facade<br/>ingest · recall · reflect · CRUD"]

    API --> SVC

    subgraph SVC["Service Layer"]
        direction LR
        IS["Ingestion"]
        SS["Search"]
        RS["Reflection"]
        OS["Entity · Note · KV<br/>Lineage · Vault<br/>Stats · Audit"]
    end

    SVC --> ENG

    subgraph ENG["Engine Layer"]
        direction LR
        EX["Extraction Engine<br/>Chunking · DSPy LLM<br/>Entity Resolution<br/>Dedup · Linking"]
        RT["Retrieval Engine<br/>TEMPR (5 strategies)<br/>RRF Fusion<br/>Reranking · MMR"]
        RF["Reflection Engine<br/>7-Phase Loop<br/>Contradiction Detection<br/>Trend Tracking<br/>Queue + Scheduler"]
    end

    ENG --> STG

    subgraph STG["Storage Layer"]
        direction LR
        MS["MetaStore<br/>PostgreSQL + pgvector<br/>HNSW · GIN tsvector<br/>GIN trigram · Advisory locks"]
        FS["FileStore<br/>Local / S3 / GCS<br/>fsspec backend<br/>LRU caching"]
    end
```

## Package Dependency Graph

Memex is a Python monorepo with 8 packages managed by `uv`.

```mermaid
graph LR
    CLI["memex_cli"] --> COMMON["memex_common"]
    CLI --> CORE["memex_core"]
    CLI --> MCP_PKG["memex_mcp"]

    CORE --> COMMON
    MCP_PKG --> COMMON
    EVAL["memex_eval"] --> COMMON
    SYNC["memex_obsidian_sync"] --> COMMON
```

| Package | Import | Purpose |
|---------|--------|---------|
| `packages/core` | `memex_core` | Storage, memory engine (extraction/retrieval/reflection), services, MemexAPI facade, FastAPI server |
| `packages/cli` | `memex_cli` | Typer CLI (`memex` command) — 12 command groups |
| `packages/mcp` | `memex_mcp` | FastMCP server — 31+ tools for LLM integration |
| `packages/common` | `memex_common` | Shared Pydantic models, hierarchical YAML config, HTTP client, exceptions |
| `packages/eval` | `memex_eval` | Evaluation: synthetic benchmarks + LoCoMo benchmark with LLM-as-judge |
| `packages/obsidian-sync` | `memex_obsidian_sync` | Watchdog-based Obsidian vault synchronization |
| `packages/firefox-extension` | — | TypeScript WebExtension for saving pages to Memex |
| `packages/claude-code-plugin` | — | Claude Code plugin: `/remember` and `/recall` skills, session hooks |

## Database Schema

The core data model centers on notes, memory units, entities, and mental models, connected through link and junction tables.

```mermaid
erDiagram
    notes ||--o{ chunks : contains
    notes ||--o{ memory_units : produces
    chunks ||--o{ nodes : "page index"

    memory_units ||--o{ unit_entities : "linked to"
    unit_entities }o--|| entities : references

    entities ||--o{ entity_cooccurrences : "co-occurs with"
    entities ||--o{ mental_models : "reflected into"

    memory_units ||--o{ memory_links : "from/to"

    mental_models ||--o{ reflection_queue : "queued for"

    notes ||--o{ audit_log : tracked

    notes {
        uuid id PK
        string title
        text original_text
        uuid vault_id FK
        string status
        timestamp created_at
    }
    memory_units {
        uuid id PK
        uuid note_id FK
        uuid chunk_id FK
        string fact_type
        text text
        vector embedding
        float confidence
        timestamp event_date
    }
    entities {
        uuid id PK
        string canonical_name
        string entity_type
        int mention_count
        uuid vault_id FK
    }
    memory_links {
        uuid id PK
        uuid from_unit_id FK
        uuid to_unit_id FK
        string link_type
        float weight
    }
    mental_models {
        uuid id PK
        uuid entity_id FK
        uuid vault_id FK
        jsonb observations
        int version
        vector embedding
    }
    chunks {
        uuid id PK
        uuid note_id FK
        text text
        vector embedding
        tsvector search_tsv
    }
```

### Index Strategy

| Type | Target | Purpose |
|------|--------|---------|
| HNSW (pgvector) | `chunks.embedding`, `memory_units.embedding`, `mental_models.embedding` | Cosine distance for semantic search |
| GIN (tsvector) | `chunks.search_tsv`, `memory_units.search_text` | Full-text keyword search |
| GIN (trigram) | `notes.title`, `entities.canonical_name` | Fuzzy matching |
| B-tree | Foreign keys, status columns, dates | Standard lookups and joins |

### Key Link Types

Memory links (`memory_links.link_type`) encode relationships between memory units:

| Link Type | Description |
|-----------|-------------|
| `causal` | X caused Y (LLM-extracted) |
| `temporal` | Sequential time ordering |
| `semantic` | Embedding similarity > threshold |
| `reinforces` | X supports/strengthens Y |
| `contradicts` | X conflicts with Y |
| `weakens` | X undermines Y |
| `enables` | X makes Y possible |
| `prevents` | X blocks Y |

## See Also

* [About the Hindsight Framework](hindsight-framework.md) — the three processing loops
* [About the Extraction Pipeline](extraction-pipeline.md) — how documents become structured memory
* [About Retrieval Strategies](retrieval-strategies.md) — TEMPR multi-strategy retrieval
* [About Reflection and Mental Models](reflection-and-mental-models.md) — background synthesis
