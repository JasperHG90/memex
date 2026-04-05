# Memex Core (`memex-core`)

The engine powering Memex. Implements the Hindsight framework for memory extraction, retrieval, and reflection, backed by PostgreSQL with pgvector for metadata and vector search, and a local filesystem for raw note storage.

## Key Features

- **Extraction Pipeline** — Converts unstructured text into structured memory units (facts, observations, experiences) with entity resolution and embedding generation.
- **TEMPR Retrieval** — 5-strategy retrieval system: Temporal, Entity, Mental Model, Keyword (BM25), and Semantic (vector). Results are fused via Reciprocal Rank Fusion (RRF).
- **Reflection Engine** — Periodic background synthesis of raw observations into high-level mental models using LLM reasoning.
- **Document Search** — Hybrid note search with optional skeleton-tree reasoning and answer synthesis.
- **Storage Abstraction** — FileStore (local, fsspec-based) for raw notes; MetaStore (PostgreSQL + pgvector) for metadata, embeddings, and search indexes.
- **FastAPI Server** — REST API with NDJSON streaming, authentication, rate limiting, and webhooks.

## Architecture

### Entry Points

| Module | Description |
|:-------|:------------|
| `memex_core.api.MemexAPI` | Main API class — orchestrates all subsystems. |
| `memex_core.server` | FastAPI REST server with lifespan management. |
| `memex_core.memory.engine.MemoryEngine` | Memory orchestrator for extraction, retrieval, and reflection. |

### Services (`memex_core.services`)

Decomposed service layer, each handling a specific domain:

| Service | Domain |
|:--------|:-------|
| `ingestion` | Note ingestion, background processing, batch operations. |
| `search` | Memory search (TEMPR) and document search orchestration. |
| `notes` | Note CRUD, page index, node retrieval. |
| `entities` | Entity CRUD, mentions, co-occurrences, lineage. |
| `reflection` | Reflection task scheduling, batch processing, dead letter queue. |
| `vaults` | Vault CRUD, active vault management. |
| `vault_summary` | Vault summary generation and regeneration. |
| `kv` | Key-value store operations. |
| `stats` | System statistics and token usage reporting. |
| `lineage` | Provenance chain traversal. |
| `audit` | Audit log queries. |

### Memory Subsystems (`memex_core.memory`)

| Module | Role |
|:-------|:-----|
| `extraction` | LLM-based fact extraction, entity resolution, embedding generation. |
| `retrieval` | TEMPR multi-strategy search with RRF fusion. |
| `reflect` | Background observation-to-mental-model synthesis. |

### Storage (`memex_core.storage`)

| Backend | Implementation |
|:--------|:---------------|
| FileStore | fsspec-based filesystem abstraction (local, S3, GCS). |
| MetaStore | SQLAlchemy async + asyncpg for PostgreSQL with pgvector. |

### Processing (`memex_core.processing`)

Pipeline utilities for text splitting, diffing, fact processing, and linking.

## Documentation

- [Hindsight Framework](../../docs/explanation/hindsight-framework.md)
- [Extraction Pipeline](../../docs/explanation/extraction-pipeline.md)
- [REST API Reference](../../docs/reference/rest-api.md)
- [Configuration Reference](../../docs/reference/configuration.md)
