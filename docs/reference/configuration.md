# Configuration Reference

Memex uses a layered configuration system built on [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/). Configuration is resolved in the following priority order (highest wins):

1. **Constructor overrides** (programmatic)
2. **Environment variables** (prefixed `MEMEX_`, nested with `__`)
3. **Local YAML config** (`memex_core.yaml`, `.memex.yaml`, or `memex_core.config.yaml` in CWD or parents)
4. **Global YAML config** (`~/.config/memex/config.yaml`)
5. **Defaults**

An explicit config path can be set via the `MEMEX_CONFIG_PATH` environment variable, which overrides the local file search.

---

## Top-Level Settings (`MemexConfig`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `server_url` | string | `""` (derived) | URL of the Memex Core server used by clients (CLI, MCP, Dashboard). If empty, derived from `server.host` and `server.port`. |
| `server` | object | — | Core API server and storage configuration. See [Server Settings](#server-settings). |
| `vault` | object | — | Client-side vault overrides. See [Vault Settings](#vault-settings-vault). |
| `dashboard` | object | — | Dashboard UI configuration. See [Dashboard Settings](#dashboard-settings). |

---

## Server Settings (`server`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `host` | string | `127.0.0.1` | Host to bind the API server to. |
| `port` | int | `8000` | Port to bind the API server to. |
| `workers` | int | `4` | Number of worker processes (Gunicorn). |
| `default_active_vault` | string | `global` | Server default vault for writing new memories. Clients can override via `vault.active`. |
| `default_reader_vault` | string | `""` | Server default vault for read-only search. Clients can override via `vault.search`. |
| `default_model` | [ModelConfig](#modelconfig) | `gemini/gemini-3-flash-preview` | System-wide default LLM. Sub-configs with `model: null` inherit this value. |
| `allow_insecure` | bool | `false` | Allow binding to non-localhost addresses without authentication. When `false` (default), the server refuses to start on a non-localhost address unless auth is enabled. |
| `cors` | object | — | CORS (Cross-Origin Resource Sharing) configuration. See [CORS](#cors-servercors). |

### Default Model Propagation

When the server starts, `default_model` is propagated to any sub-config whose `model` field is `null`:

- `server.memory.extraction.model`
- `server.memory.extraction.text_splitting.model` (page_index strategy only)
- `server.memory.reflection.model`
- `server.memory.contradiction.model`
- `server.document.model`

Set a sub-config's `model` explicitly to override the default for that subsystem.

---

### CORS (`server.cors`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `origins` | list[string] | `["http://localhost:5173", "http://localhost:3000"]` | Allowed origins for CORS requests. |
| `allow_credentials` | bool | `true` | Whether to allow credentials (cookies, auth headers) in CORS requests. |
| `allow_methods` | list[string] | `["*"]` | HTTP methods allowed in CORS requests. |
| `allow_headers` | list[string] | `["*"]` | HTTP headers allowed in CORS requests. |

---

### Authentication (`server.auth`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `enabled` | bool | `false` | Enable API key authentication. Disabled by default for localhost. |
| `api_keys` | list[string] | `[]` | List of valid API keys. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `exempt_paths` | list[string] | `["/api/v1/health", "/api/v1/ready", "/api/v1/metrics"]` | Paths that do not require authentication. |
| `webhook_secret` | string \| null | `null` | Shared secret for HMAC-SHA256 webhook signature validation. Callers send `X-Webhook-Signature` header with `hex(HMAC-SHA256(secret, body))`. |

---

### Rate Limiting (`server.rate_limit`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `enabled` | bool | `false` | Enable rate limiting. Disabled by default. |
| `ingestion` | string | `10/minute` | Rate limit for ingestion endpoints. |
| `search` | string | `60/minute` | Rate limit for search endpoints. |
| `batch` | string | `5/minute` | Rate limit for batch endpoints. |
| `default` | string | `120/minute` | Default rate limit for all other endpoints. |

Rate limits use [SlowAPI](https://github.com/laurentS/slowapi) format: `{count}/{period}` where period is `second`, `minute`, `hour`, or `day`.

---

### Logging (`server.logging`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `log_file` | string | `~/.local/state/memex/memex.log` | Path to the log file. Platform-dependent via `platformdirs.user_log_dir`. |
| `level` | string | `WARNING` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `json_output` | bool | `false` | Output logs as JSON (for production log aggregators). |

---

### Meta Store (`server.meta_store`)

PostgreSQL metadata and vector database configuration.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `type` | string | `postgres` | Storage backend type. Currently only `postgres` is supported. |
| `pool_size` | int | `10` | Connection pool size. |
| `max_overflow` | int | `20` | Maximum overflow connections beyond `pool_size`. |
| `statement_timeout_ms` | int | `30000` | Statement timeout in milliseconds for queries (30s default). |

#### PostgreSQL Instance (`server.meta_store.instance`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `host` | string | `localhost` | Hostname or IP of the PostgreSQL server. **Required.** |
| `port` | int | `5432` | Port the PostgreSQL server listens on. |
| `database` | string | `postgres` | Database name. **Required.** |
| `user` | string | `postgres` | Database username. **Required.** |
| `password` | string | `postgres` | Database password. **Required.** Stored as `SecretStr`. |

The connection string is assembled as: `postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}`

---

### File Store (`server.file_store`)

Document storage backend for raw note files.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `type` | string | `local` | Storage backend type. Currently only `local` is supported. |
| `root` | string | `~/.local/share/memex` | Root directory for data storage. Platform-dependent via `platformdirs.user_data_dir`. |
| `max_concurrent_connections` | int | `10` | Maximum concurrent filesystem operations. |

Notes are stored under `{root}/notes/`.

---

### Memory (`server.memory`)

#### Extraction (`server.memory.extraction`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `model` | [ModelConfig](#modelconfig) \| null | `null` (inherits `default_model`) | LLM for fact extraction. |
| `max_concurrency` | int | `5` | Maximum concurrent LLM calls for fact extraction. |
| `text_splitting` | object | `page_index` strategy | Text splitting strategy and parameters. See below. |

##### Page Index Strategy (`text_splitting.strategy: page_index`)

Default strategy. Uses hierarchical document structure for intelligent chunking.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `strategy` | string | `page_index` | Discriminator value. |
| `scan_chunk_size_tokens` | int | `6000` | Chunk size in tokens for LLM scanning path. |
| `block_token_target` | int | `2000` | Target token count per block. |
| `short_doc_threshold_tokens` | int | `500` | Documents below this token count with no headers bypass PageIndex. |
| `max_node_length_tokens` | int | `1250` | Max tokens per node before triggering refinement. |
| `min_node_tokens` | int | `0` | Nodes with this many tokens or fewer are skipped during indexing. Set to e.g. `25` to drop trivially short sections. |
| `model` | [ModelConfig](#modelconfig) \| null | `null` (inherits `default_model`) | Model for PageIndex LLM calls. |

##### Simple Strategy (`text_splitting.strategy: simple`)

Flat content-defined chunking without document structure awareness.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `strategy` | string | `simple` | Discriminator value. |
| `chunk_size_tokens` | int | `1000` | Target size for content-defined blocks in tokens. |
| `chunk_overlap_tokens` | int | `50` | Number of overlapping tokens between chunks. |

---

#### Retrieval (`server.memory.retrieval`)

TEMPR multi-strategy search configuration.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `token_budget` | int | `2000` | Maximum token budget for retrieval results (greedy packing). |
| `similarity_threshold` | float | `0.3` | Minimum `pg_trgm` similarity score for entity name matching in graph strategies. |
| `temporal_decay_days` | float | `30.0` | Half-life in days for temporal decay scoring. |
| `temporal_decay_base` | float | `2.0` | Base for temporal decay exponential: `score = base ^ (-days / decay_days)`. |
| `rrf_k` | int | `60` | Reciprocal Rank Fusion constant. Higher values produce more uniform blending across strategies. |
| `candidate_pool_size` | int | `60` | Number of candidates retrieved per strategy in multi-strategy RRF retrieval. |
| `mmr_lambda` | float \| null | `0.9` | MMR (Maximal Marginal Relevance) diversity lambda for memory search. `1.0` = pure relevance, `0.0` = max diversity. `null` disables MMR. `0.9` is conservative — suppresses near-duplicates while preserving distinct results. |
| `mmr_embedding_weight` | float | `0.6` | Weight of cosine similarity in the MMR hybrid similarity kernel. |
| `mmr_entity_weight` | float | `0.4` | Weight of entity Jaccard similarity in the MMR hybrid similarity kernel. |

##### Retrieval Strategies (`server.memory.retrieval.retrieval_strategies`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `semantic` | bool | `true` | Enable semantic (vector) search strategy. |
| `keyword` | bool | `true` | Enable keyword (BM25) search strategy. |
| `graph` | bool | `true` | Enable graph (entity) search strategy. |
| `temporal` | bool | `true` | Enable temporal search strategy. |
| `mental_model` | bool | `true` | Enable mental model search strategy (memory search only; not available for document search). |

---

#### Reflection (`server.memory.reflection`)

Hindsight Reflection Engine configuration for synthesizing observations into mental models.

| Key | Type | Default | Constraints | Description |
|:----|:-----|:--------|:------------|:------------|
| `model` | [ModelConfig](#modelconfig) \| null | `null` (inherits `default_model`) | — | LLM for reflection. |
| `max_concurrency` | int | `3` | > 1 | Maximum concurrent entities to reflect on in a single batch. |
| `weight_urgency` | float | `0.5` | >= 0 | Weight for Accumulated Evidence (Urgency) in priority calculation. |
| `weight_importance` | float | `0.2` | >= 0 | Weight for Global Frequency (Importance) in priority calculation. |
| `weight_resonance` | float | `0.3` | >= 0 | Weight for User Retrieval (Resonance) in priority calculation. |
| `search_limit` | int | `10` | >= 0 | Number of candidates to retrieve in the Hunt phase. |
| `similarity_threshold` | float | `0.6` | >= 0 | Minimum similarity score for retrieving evidence. |
| `min_priority` | float | `0.3` | 0-1 | Minimum priority score required for an entity to be selected for reflection. |
| `tail_sampling_rate` | float | `0.05` | 0-1 | Rate for tail sampling of traces/memories (5% default). |
| `background_reflection_enabled` | bool | `false` | — | Whether to run the periodic reflection loop in the background. |
| `background_reflection_interval_seconds` | int | `600` | >= 10 | Interval in seconds between background reflection runs. |
| `background_reflection_batch_size` | int | `10` | > 0 | Number of entities to process in each background reflection batch. |

The three priority weights (`weight_urgency`, `weight_importance`, `weight_resonance`) must sum to exactly 1.0.

---

#### Contradiction Detection (`server.memory.contradiction`)

Retain-time contradiction detection runs after extraction to identify and link contradictory, superseding, or updating memory units.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `enabled` | bool | `true` | Enable contradiction detection after extraction. |
| `alpha` | float | `0.1` | Hindsight step size for confidence adjustment. |
| `similarity_threshold` | float | `0.5` | Minimum cosine similarity for candidate retrieval. |
| `max_candidates_per_unit` | int | `15` | Maximum candidates to compare per flagged unit. |
| `superseded_threshold` | float | `0.3` | Confidence below this marks a unit as superseded. |
| `model` | [ModelConfig](#modelconfig) \| null | `null` (inherits `default_model`) | LLM for contradiction classification. |

---

#### Circuit Breaker (`server.memory.circuit_breaker`)

Protects against cascading failures from LLM provider outages.

| Key | Type | Default | Constraints | Description |
|:----|:-----|:--------|:------------|:------------|
| `enabled` | bool | `true` | — | Whether the circuit breaker is enabled. |
| `failure_threshold` | int | `5` | >= 1 | Number of consecutive failures before opening the circuit. |
| `reset_timeout_seconds` | float | `60.0` | > 0 | Seconds to stay open before allowing a probe request. |

---

### Document Search (`server.document`)

Configuration for raw document (note) search and processing.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `model` | [ModelConfig](#modelconfig) \| null | `null` (inherits `default_model`) | LLM for skeleton-tree reasoning and answer synthesis. |
| `mmr_lambda` | float \| null | `null` (disabled) | Default MMR (Maximal Marginal Relevance) lambda for document search. `1.0` = pure relevance, `0.0` = max diversity. `null` disables MMR. Can be overridden per-request. |

##### Document Search Strategies (`server.document.search_strategies`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `semantic` | bool | `true` | Enable semantic (vector) search. |
| `keyword` | bool | `true` | Enable keyword (BM25) search. |
| `graph` | bool | `true` | Enable graph (entity) search. |
| `temporal` | bool | `true` | Enable temporal search. |

Note: Document search does not support the `mental_model` strategy.

---

## Dashboard Settings (`dashboard`)

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `host` | string | `0.0.0.0` | Host to serve the React + Vite dashboard on. |
| `port` | int | `3001` | Port for the dashboard dev/preview server. |

---

## ModelConfig

Reusable model configuration block used by `default_model`, `extraction.model`, `reflection.model`, `document.model`, and `text_splitting.model`.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `model` | string | **Required** | Full model identifier string (e.g. `ollama_chat/llama3`, `gemini/gemini-3-flash-preview`, `openai/gpt-4o`). Uses [LiteLLM](https://docs.litellm.ai/) format. |
| `base_url` | string \| null | `null` | Base URL for the API (for Ollama, vLLM, or other local inference). |
| `api_key` | string \| null | `null` | API key for the model provider. Stored as `SecretStr`. |
| `max_tokens` | int \| null | `null` | Maximum tokens to generate. |
| `temperature` | float \| null | `null` | Sampling temperature. |
| `reasoning_effort` | string \| null | `null` | Reasoning effort level (if supported by the model). |

---

## Vault Settings (`vault`)

Client-side vault preferences for CLI and MCP. These control which vaults are used for writes and reads, separate from the server's own defaults.

| Key | Type | Default | Description |
|:----|:-----|:--------|:------------|
| `active` | string \| null | `null` | Client write vault name. Overrides `server.default_active_vault`. |
| `search` | list[string] \| null | `null` | Client read vault names for search scope. Overrides `server.default_reader_vault`. |

### Convenience Properties on `MemexConfig`

| Property | Resolution Order | Description |
|:---------|:-----------------|:------------|
| `config.write_vault` | `vault.active` > `server.default_active_vault` | Resolved write vault name. |
| `config.read_vaults` | `vault.search` > `[vault.active]` > `[server.default_reader_vault]` | Resolved list of read vault names. |

### Resolution Precedence

The write and read vaults are resolved identically for both CLI and MCP:

| Priority | Write vault | Read vaults |
|:---------|:------------|:------------|
| 1 (highest) | Explicit param (`--vault` / tool `vault_id`) | Explicit param (`--vault` / tool `vault_ids`) |
| 2 | Env var `MEMEX_VAULT__ACTIVE` | Env var `MEMEX_VAULT__SEARCH` |
| 3 | `.memex.yaml` → `vault.active` | `.memex.yaml` → `vault.search` |
| 4 | Global config → `vault.active` | Global config → `vault.search` |
| 5 (lowest) | `server.default_active_vault` (default: `global`) | `[server.default_reader_vault]` (default: `[global]`) |

> **CLI vs MCP:** The CLI always picks up `.memex.yaml` from CWD because you control the working directory. MCP is spawned as a subprocess by the IDE — whether it inherits the project CWD is **not guaranteed**. For consistent behavior across both, use environment variables.

### Environment Variables for Vault Config

```bash
# In a shell (CLI)
export MEMEX_VAULT__ACTIVE=my-project
export MEMEX_VAULT__SEARCH='["my-project", "shared"]'
```

```json
// In .claude/mcp.json (MCP)
{
  "env": {
    "MEMEX_VAULT__ACTIVE": "my-project",
    "MEMEX_VAULT__SEARCH": "[\"my-project\", \"shared\"]"
  }
}
```

> **Important:** `MEMEX_VAULT__SEARCH` must be a **string** containing a JSON array, not a native JSON array. Env vars are always strings — write `"[\"a\", \"b\"]"`, not `["a", "b"]`. The latter will fail MCP config validation. Pydantic-settings automatically JSON-decodes string env vars when the target field is a complex type like `list[str]`, so the string `'["a", "b"]'` becomes the Python list `["a", "b"]`.

### Example Configurations

**Per-project** (`.memex.yaml` — reliable for CLI, not guaranteed for MCP):

```yaml
vault:
  active: my-project
  search: [my-project, shared-knowledge]
```

**Simple** — only set write vault, reads default to `[active]`:

```yaml
vault:
  active: my-project
```

**No vault section** — falls back to server defaults:

```yaml
server:
  default_active_vault: global
  default_reader_vault: global
```

---

## Environment Variable Mapping

All configuration keys can be set via environment variables using the `MEMEX_` prefix and double underscores (`__`) for nesting.

### Examples

```bash
# Top-level
export MEMEX_SERVER_URL=http://localhost:8000

# Server settings
export MEMEX_SERVER__HOST=0.0.0.0
export MEMEX_SERVER__PORT=9000
export MEMEX_SERVER__DEFAULT_ACTIVE_VAULT=project-alpha

# Client vault overrides
export MEMEX_VAULT__ACTIVE=my-project
export MEMEX_VAULT__SEARCH='["my-project", "shared"]'

# PostgreSQL
export MEMEX_SERVER__META_STORE__INSTANCE__HOST=db.example.com
export MEMEX_SERVER__META_STORE__INSTANCE__PORT=5432
export MEMEX_SERVER__META_STORE__INSTANCE__DATABASE=memex_prod
export MEMEX_SERVER__META_STORE__INSTANCE__USER=memex
export MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD=secret

# Connection pool tuning
export MEMEX_SERVER__META_STORE__POOL_SIZE=20
export MEMEX_SERVER__META_STORE__MAX_OVERFLOW=40
export MEMEX_SERVER__META_STORE__STATEMENT_TIMEOUT_MS=60000

# Authentication
export MEMEX_SERVER__AUTH__ENABLED=true
export MEMEX_SERVER__AUTH__API_KEYS='["key1", "key2"]'
export MEMEX_SERVER__AUTH__WEBHOOK_SECRET=my-webhook-secret

# CORS
export MEMEX_SERVER__CORS__ORIGINS='["http://localhost:5173", "https://app.example.com"]'
export MEMEX_SERVER__CORS__ALLOW_CREDENTIALS=true

# Security
export MEMEX_SERVER__ALLOW_INSECURE=false

# Rate limiting
export MEMEX_SERVER__RATE_LIMIT__ENABLED=true
export MEMEX_SERVER__RATE_LIMIT__INGESTION=20/minute

# Logging
export MEMEX_SERVER__LOGGING__LEVEL=INFO
export MEMEX_SERVER__LOGGING__JSON_OUTPUT=true

# Default model
export MEMEX_SERVER__DEFAULT_MODEL__MODEL=openai/gpt-4o
export MEMEX_SERVER__DEFAULT_MODEL__API_KEY=sk-...

# Retrieval tuning
export MEMEX_SERVER__MEMORY__RETRIEVAL__TOKEN_BUDGET=4000
export MEMEX_SERVER__MEMORY__RETRIEVAL__RRF_K=80
export MEMEX_SERVER__MEMORY__RETRIEVAL__MMR_LAMBDA=0.9

# Reflection
export MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED=false

# Circuit breaker
export MEMEX_SERVER__MEMORY__CIRCUIT_BREAKER__FAILURE_THRESHOLD=10

# Contradiction detection
export MEMEX_SERVER__MEMORY__CONTRADICTION__ENABLED=true
export MEMEX_SERVER__MEMORY__CONTRADICTION__ALPHA=0.1
```

### Special Environment Variables

| Variable | Description |
|:---------|:------------|
| `MEMEX_CONFIG_PATH` | Explicit path to a YAML config file. Overrides local file search. |
| `MEMEX_LOAD_GLOBAL_CONFIG` | Set to `false` to skip loading `~/.config/memex/config.yaml`. |
| `MEMEX_LOAD_LOCAL_CONFIG` | Set to `false` to skip searching CWD and parents for config files. |
| `MEMEX_VAULT__ACTIVE` | Client write vault override. Equivalent to `vault.active` in YAML. See [Vault Settings](#vault-settings-vault). |
| `MEMEX_VAULT__SEARCH` | Client read vaults override. Must be a **string** containing a JSON array (e.g., `'["a", "b"]'`). Pydantic-settings JSON-decodes it automatically. Equivalent to `vault.search` in YAML. See [Vault Settings](#vault-settings-vault). |

---

## Config File Locations

### Global Config

```
~/.config/memex/config.yaml
```

Platform-dependent path via `platformdirs.user_config_dir('memex')`.

### Local Config (searched in order)

The following filenames are searched in the current working directory and all parent directories:

1. `memex_core.yaml`
2. `.memex.yaml`
3. `memex_core.config.yaml`

The first file found is used. Local config is merged on top of global config via deep merge.

---

## Example Config Files

### Minimal (local development)

```yaml
server:
  meta_store:
    instance:
      host: localhost
      port: 5432
      database: memex
      user: postgres
      password: postgres
```

### Production

```yaml
server:
  host: 0.0.0.0
  port: 8000
  workers: 8

  default_model:
    model: openai/gpt-4o
    api_key: sk-...

  cors:
    origins:
      - "https://app.example.com"
      - "https://admin.example.com"
    allow_credentials: true

  auth:
    enabled: true
    api_keys:
      - "key-1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      - "key-2-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    webhook_secret: "whsec-xxxxxxxxxxxxxxxxxxxxxxxx"

  rate_limit:
    enabled: true
    ingestion: 20/minute
    search: 120/minute
    batch: 10/minute
    default: 200/minute

  logging:
    level: INFO
    json_output: true
    log_file: /var/log/memex/memex.log

  meta_store:
    instance:
      host: db.internal
      port: 5432
      database: memex_prod
      user: memex_app
      password: ${MEMEX_DB_PASSWORD}
    pool_size: 20
    max_overflow: 40
    statement_timeout_ms: 60000

  file_store:
    type: local
    root: /data/memex

  memory:
    extraction:
      max_concurrency: 10
      text_splitting:
        strategy: page_index
        scan_chunk_size_tokens: 8000

    retrieval:
      token_budget: 4000
      candidate_pool_size: 100
      rrf_k: 80
      mmr_lambda: 0.9  # Conservative diversity filtering (null to disable)

    reflection:
      background_reflection_enabled: true
      background_reflection_interval_seconds: 300
      background_reflection_batch_size: 20
      max_concurrency: 5

    circuit_breaker:
      failure_threshold: 10
      reset_timeout_seconds: 120

    contradiction:
      enabled: true
      alpha: 0.1

  document:
    mmr_lambda: 0.7

dashboard:
  host: 0.0.0.0
  port: 3001
```

### Per-Project (vault override)

Place a `.memex.yaml` in your project root to set the vault context per project:

```yaml
vault:
  active: my-project              # client write target
  search: [my-project, shared]    # client read scope
```

The `vault.active` setting overrides `server.default_active_vault`. The `vault.search` list overrides the default reader vault and controls which vaults are included in search queries.

### Local Ollama

```yaml
server:
  default_model:
    model: ollama_chat/llama3
    base_url: http://localhost:11434

  meta_store:
    instance:
      host: localhost
      database: memex
      user: postgres
      password: postgres
```
