# Configuration Reference

Memex uses a nested configuration structure that can be defined in `config.yaml`, through environment variables (prefixed with `MEMEX_`), or via CLI overrides (`--set`).

## Structure Overview

The configuration is split into two main top-level sections:
- `server`: Core API and background worker settings.
- `dashboard`: Reflex web UI settings.

There is also a top-level `server_url` for clients.

---

## Server Settings (`server`)

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `host` | str | Host to bind the API server to. | `127.0.0.1` |
| `port` | int | Port to bind the API server to. | `8000` |
| `workers` | int | Number of worker processes. | `4` |
| `active_vault` | str | Default vault name for writes. | `global` |
| `attached_vaults` | list[str] | Vaults to include in search by default. | `[]` |
| `default_model.model` | str | System-wide default model ID. | `gemini/gemini-3-flash-preview` |
| `logging.log_file` | str | Path to the system log file. | `~/.local/share/memex/memex.log` |

### Meta Store (`server.meta_store`)
Configuration for the PostgreSQL metadata and vector database.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `type` | str | Must be `postgres`. | `postgres` |
| `instance.host` | str | Database hostname. | `localhost` |
| `instance.port` | int | Database port. | `5432` |
| `instance.user` | str | Database username. | `postgres` |
| `instance.password` | str | Database password. | `postgres` |
| `instance.database` | str | Database name. | `postgres` |
| `pool_size` | int | Connection pool size. | `10` |

### File Store (`server.file_store`)
Configuration for the document storage backend.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `type` | str | Currently only `local` is supported. | `local` |
| `root` | str | Directory where raw notes are saved. | `~/.local/share/memex/files` |

### Memory (`server.memory`)

#### Extraction (`server.memory.extraction`)
Settings for how facts are extracted from documents.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `model.model` | str | LLM ID for extraction. | `gemini/gemini-3-flash-preview` |
| `max_concurrency` | int | Max parallel extraction calls. | `5` |
| `text_splitting.strategy` | str | `simple` or `page_index`. | `page_index` |

**PageIndex Strategy (`page_index`):**
| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `scan_chunk_size_tokens` | int | Chunk size for LLM scanning. | `6000` |
| `block_token_target` | int | Target token count per block. | `2000` |
| `short_doc_threshold_tokens` | int | Bypass PageIndex for short docs. | `500` |
| `max_node_length_tokens` | int | Max tokens per node before refinement. | `1250` |
| `min_node_tokens` | int | Min tokens for a node to be indexed. | `0` |

**Simple Strategy (`simple`):**
| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `chunk_size_tokens` | int | Target size for blocks. | `1000` |
| `chunk_overlap_tokens` | int | Overlap between chunks. | `50` |

#### Retrieval (`server.memory.retrieval`)
Settings for TEMPR search.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `token_budget` | int | Max tokens for context returned to LLM. | `2000` |
| `retrieval_strategies.semantic` | bool | Enable semantic search. | `True` |
| `retrieval_strategies.keyword` | bool | Enable keyword search. | `True` |
| `retrieval_strategies.graph` | bool | Enable graph traversal. | `True` |
| `retrieval_strategies.temporal` | bool | Enable temporal search. | `True` |
| `retrieval_strategies.mental_model`| bool | Enable mental model strategy. | `True` |

#### Opinion Formation (`server.memory.opinion_formation`)
Settings for Bayesian confidence scoring.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `confidence.damping_factor` | float | Factor to dampen neighbor confidence. | `0.1` |
| `confidence.max_inherited_mass`| float | Max inherited alpha+beta mass. | `10.0` |
| `confidence.similarity_threshold`| float| Minimum cosine similarity. | `0.8` |

#### Reflection (`server.memory.reflection`)
Settings for the background consolidation engine.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `max_concurrency` | int | Max parallel entities to reflect on. | `3` |
| `weight_urgency` | float | Priority given to evidence count (0-1). | `0.5` |
| `weight_importance` | float | Priority given to global frequency. | `0.2` |
| `weight_resonance` | float | Priority given to user retrieval. | `0.3` |
| `search_limit` | int | Candidates to retrieve in Hunt phase. | `10` |
| `similarity_threshold` | float | Minimum similarity score for evidence. | `0.6` |
| `tail_sampling_rate` | float | Rate for tail sampling of traces. | `0.05` |
| `min_priority` | float | Minimum priority score for reflection. | `0.3` |
| `background_reflection_enabled` | bool | Run reflection loop in background. | `True` |
| `background_reflection_interval_seconds` | int | Interval between runs. | `600` |
| `background_reflection_batch_size` | int | Entities to process in batch. | `10` |

### Document Search (`server.document`)
Settings for raw document search and processing.

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `model.model` | str | LLM ID for skeleton-tree reasoning. | `gemini/gemini-3-flash-preview` |
| `search_strategies.semantic` | bool | Enable semantic search. | `True` |
| `search_strategies.keyword` | bool | Enable keyword search. | `True` |
| `search_strategies.graph` | bool | Enable graph traversal. | `True` |
| `search_strategies.temporal` | bool | Enable temporal search. | `True` |

---

## Dashboard Settings (`dashboard`)

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `host` | str | Host to serve the dashboard on. | `0.0.0.0` |
| `port` | int | Port for the Reflex web server. | `3001` |

---

## Environment Variable Mapping

Nested keys use double underscores:
- `MEMEX_SERVER__ACTIVE_VAULT=project-alpha`
- `MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD=secret`
- `MEMEX_SERVER_URL=http://localhost:8000`
