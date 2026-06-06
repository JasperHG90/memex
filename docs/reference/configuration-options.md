# Configuration options

Every Memex configuration key, its type, default, environment variable, and a one-line description. Grouped by Pydantic config class. Use the index to jump to a class; use the entries within a class to find a key.

> **Defaults are literature-precedent, not fine-tuned.** Any Memory Worth, FSFM, exploration, confidence, decay, or threshold knob below ships at a value chosen from prior work or an early empirical pass — they are starting points, not optima. Tune in your environment with the evaluation suite before treating any of them as load-bearing.

## How to read this page

- **Key.** The dotted path under `MemexConfig`. To write it in YAML, indent each segment as a key under the parent. Example: `server.memory.retrieval.rrf_k` becomes the nested YAML below.
  ```yaml
  server:
    memory:
      retrieval:
        rrf_k: 60
  ```
- **Type.** The Pydantic type. `int | None` means the field accepts either an integer or `null`.
- **Default.** What the field is set to if you omit it. `—` means the field is required.
- **Env var.** The exact `MEMEX_` environment variable that overrides the key. Nesting uses double underscores (`__`).
- **Description.** What the key controls, in one line. For fields with validators or interactions, see the entry's expanded notes below the table where they exist.

For the full layering rules (priority order: constructor > env > local YAML > global YAML > defaults) and worked examples, see the YAML walkthrough in [Reference: configuration](configuration.md).

---

## Index of config classes

| Class | Section | Purpose |
|---|---|---|
| `MemexConfig` | [MemexConfig](#memexconfig) | Top-level container — client + server settings. |
| `VaultConfig` | [VaultConfig](#vaultconfig) | Client-side write and read vault preferences. |
| `ServerConfig` | [ServerConfig](#serverconfig) | API server, model defaults, store backends. |
| `ModelConfig` | [ModelConfig](#modelconfig) | Reusable LLM model spec (model id, key, temp). |
| `LoggingConfig` | [LoggingConfig](#loggingconfig) | Log file, level, JSON output. |
| `AuthConfig` | [AuthConfig](#authconfig) | API-key authentication. |
| `ApiKeyConfig` | [ApiKeyConfig](#apikeyconfig) | One API key bound to a policy. |
| `CorsConfig` | [CorsConfig](#corsconfig) | Cross-origin policy. |
| `RateLimitConfig` | [RateLimitConfig](#ratelimitconfig) | Per-endpoint API rate limits. |
| `TracingConfig` | [TracingConfig](#tracingconfig) | OpenTelemetry export. |
| `PostgresInstanceConfig` | [PostgresInstanceConfig](#postgresinstanceconfig) | Postgres connection params. |
| `PostgresMetaStoreConfig` | [PostgresMetaStoreConfig](#postgresmetastoreconfig) | Postgres pool and timeouts. |
| `LocalFileStoreConfig` | [LocalFileStoreConfig](#localfilestoreconfig) | Local-disk note storage. |
| `S3FileStoreConfig` | [S3FileStoreConfig](#s3filestoreconfig) | S3-compatible note storage. |
| `GCSFileStoreConfig` | [GCSFileStoreConfig](#gcsfilestoreconfig) | Google Cloud Storage notes. |
| `OnnxBackend` | [Model backends](#model-backends) | Built-in ONNX model selector. |
| `LitellmEmbeddingBackend` | [Model backends](#model-backends) | LiteLLM embedding provider. |
| `LitellmRerankerBackend` | [Model backends](#model-backends) | LiteLLM rerank provider. |
| `LitellmNLIBackend` | [Model backends](#model-backends) | LiteLLM NLI classifier. |
| `DisabledBackend` | [Model backends](#model-backends) | Explicit "disabled" model. |
| `MemoryConfig` | [MemoryConfig](#memoryconfig) | Container for the memory engine. |
| `ExtractionConfig` | [ExtractionConfig](#extractionconfig) | Fact-extraction pipeline. |
| `SimpleTextSplitting` | [SimpleTextSplitting](#simpletextsplitting) | Flat CDC chunking. |
| `PageIndexTextSplitting` | [PageIndexTextSplitting](#pageindextextsplitting) | Hierarchical chunking. |
| `RetrievalConfig` | [RetrievalConfig](#retrievalconfig) | TEMPR retrieval + reranking. |
| `RelationConfig` | [RelationConfig](#relationconfig) | Related-note / link enrichment. |
| `SearchStrategiesConfig` | [SearchStrategiesConfig](#searchstrategiesconfig) | Strategy on/off for memory search. |
| `DocSearchStrategiesConfig` | [DocSearchStrategiesConfig](#docsearchstrategiesconfig) | Strategy on/off for doc search. |
| `ReflectionConfig` | [ReflectionConfig](#reflectionconfig) | Hindsight reflection loop. |
| `SummarizeNodeRateLimitConfig` | [SummarizeNodeRateLimitConfig](#summarizenoderatelimitconfig) | Per-entity summarize cap. |
| `ContradictionConfig` | [ContradictionConfig](#contradictionconfig) | Retain-time contradiction detection. |
| `ConsolidationConfig` | [ConsolidationConfig](#consolidationconfig) | Vault consolidation cadence. |
| `ConsolidateRateLimitConfig` | [ConsolidateRateLimitConfig](#consolidateratelimitconfig) | Per-vault consolidate cap. |
| `OutcomesConfig` | [OutcomesConfig](#outcomesconfig) | Outcome attribution policy. |
| `CircuitBreakerConfig` | [CircuitBreakerConfig](#circuitbreakerconfig) | LLM circuit breaker. |
| `LintConfig` | [LintConfig](#lintconfig) | Rule-based maintenance linter. |
| `LintConfidenceGate` | [LintConfidenceGate](#lintconfidencegate) | Confidence / variance lint gate. |
| `LintLLMConfig` | [LintLLMConfig](#lintllmconfig) | Surprise-gated LLM linter. |
| `LintLLMChecksConfig` | [LintLLMChecksConfig](#lintllmchecksconfig) | Per-check toggles for LLM lint. |
| `LintLLMCheckConfig` | [LintLLMCheckConfig](#lintllmcheckconfig) | One check's flag shape. |
| `NLIPolarityConfig` | [NLIPolarityConfig](#nlipolarityconfig) | NLI polarity gate. |
| `EntityMaintenanceConfig` | [EntityMaintenanceConfig](#entitymaintenanceconfig) | Cross-batch entity collapse. |
| `DeprioritizeScoreConfig` | [DeprioritizeScoreConfig](#deprioritizescoreconfig) | FSFM deprio scorer. |
| `DeprioritizeScoreWeights` | [DeprioritizeScoreWeights](#deprioritizescoreweights) | Component weights for the scorer. |
| `DeprioritizeScoreThresholds` | [DeprioritizeScoreThresholds](#deprioritizescorethresholds) | Threshold band for the scorer. |
| `DocumentConfig` | [DocumentConfig](#documentconfig) | Document search and synthesis. |
| `VaultSummaryConfig` | [VaultSummaryConfig](#vaultsummaryconfig) | Periodic vault summaries. |

Specials (not Pydantic fields) — see [Special environment variables](#special-environment-variables).

---

## MemexConfig

The top-level settings object. Lives at `MemexConfig` in `packages/common/src/memex_common/config.py`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `server_url` | `str` | `""` (derived from `server.host:server.port`) | `MEMEX_SERVER_URL` | Memex Core server URL used by CLI and MCP clients. |
| `api_key` | `SecretStr \| None` | `null` | `MEMEX_API_KEY` | API key the CLI and MCP clients send to the server. |
| `vault` | `VaultConfig` | see [VaultConfig](#vaultconfig) | `MEMEX_VAULT__*` | Client-side vault preferences. |
| `server` | `ServerConfig` | see [ServerConfig](#serverconfig) | `MEMEX_SERVER__*` | Server-side configuration. |

**Derived properties (read-only).**

| Property | Resolution | Description |
|---|---|---|
| `write_vault` | `vault.active` > `server.default_active_vault` | Effective write target for the client. |
| `read_vaults` | `vault.search` > `[vault.active]` > `[server.default_reader_vault]` | Effective read scope for the client. |

`MemexConfig` derives `server_url` after load: if you leave `server_url` empty, it becomes `http://{server.host}:{server.port}`.

---

## VaultConfig

Client-side write and read preferences. Lives at `MemexConfig.vault`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `active` | `str \| None` | `null` | `MEMEX_VAULT__ACTIVE` | Active write vault. Overrides `server.default_active_vault`. |
| `search` | `list[str] \| None` | `null` | `MEMEX_VAULT__SEARCH` | Vaults to search. Overrides `server.default_reader_vault`. Pass a JSON-string when using the env var: `'["a","b"]'`. |

---

## ServerConfig

The API server, models, and stores. Lives at `MemexConfig.server`.

### Bind and process

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `host` | `str` | `127.0.0.1` | `MEMEX_SERVER__HOST` | Address the API server binds to. |
| `port` | `int` | `8000` | `MEMEX_SERVER__PORT` | Port the API server binds to. |
| `workers` | `int` | `4` | `MEMEX_SERVER__WORKERS` | Number of worker processes. |
| `allow_insecure` | `bool` | `false` | `MEMEX_SERVER__ALLOW_INSECURE` | Permit binding to a non-localhost address without auth. The server refuses to start otherwise. |
| `cache_dir` | `str` | `~/.cache/memex` (platform-dependent) | `MEMEX_SERVER__CACHE_DIR` | Cache directory for ML model artefacts. |

### Vault defaults

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `default_active_vault` | `str` | `global` | `MEMEX_SERVER__DEFAULT_ACTIVE_VAULT` | Server fallback write vault when no client preference is set. |
| `default_reader_vault` | `str` | `global` | `MEMEX_SERVER__DEFAULT_READER_VAULT` | Server fallback read vault when no client preference is set. |

The vault names are validated after load: a name over 50 characters, or one containing characters outside `[a-zA-Z0-9_\-.]`, raises a `UserWarning`.

### Append endpoint

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `append_enabled` | `bool` | `true` | `MEMEX_SERVER__APPEND_ENABLED` | Toggle the note-append endpoint, MCP tool, and CLI. Off returns 503 / `FeatureDisabledError`. |
| `append_lock_acquire_timeout_seconds` | `float` (>= 0.1) | `30.0` | `MEMEX_SERVER__APPEND_LOCK_ACQUIRE_TIMEOUT_SECONDS` | Wait time for the per-parent advisory + row lock before returning 503. |
| `append_extraction_lock_timeout_seconds` | `float` (>= 0.1) | `300.0` | `MEMEX_SERVER__APPEND_EXTRACTION_LOCK_TIMEOUT_SECONDS` | Upper bound on Postgres `lock_timeout` while extraction runs inside an append. |

### Default and model backends

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `default_model` | `ModelConfig` | `ModelConfig(model='gemini/gemini-3-flash-preview')` | `MEMEX_SERVER__DEFAULT_MODEL__*` | System-wide LLM. Propagates into any sub-config whose `model` is `null`. |
| `embedding_model` | `OnnxBackend \| LitellmEmbeddingBackend` | `OnnxBackend()` | `MEMEX_SERVER__EMBEDDING_MODEL__*` | Embedding model backend. Default ONNX; set `type=litellm` for any LiteLLM provider. |

`default_model` propagates after load. Sub-configs that inherit it when their own `model` is `null`:

- `server.memory.extraction.model`
- `server.memory.extraction.text_splitting.model` (only on the `page_index` strategy)
- `server.memory.reflection.model`
- `server.memory.contradiction.model`
- `server.document.model`
- `server.vault_summary.model`

### Model-call concurrency and timeouts

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `embedding_batch_size` | `int` | `0` | `MEMEX_SERVER__EMBEDDING_BATCH_SIZE` | Max texts per ONNX embedding call. `0` = no batching. |
| `embedding_max_concurrency` | `int` (>= 1) | `16` | `MEMEX_SERVER__EMBEDDING_MAX_CONCURRENCY` | Max concurrent embedding-model calls across all three embed sites. |
| `embedding_call_timeout` | `int` (>= 1) | `30` | `MEMEX_SERVER__EMBEDDING_CALL_TIMEOUT` | Per-call embedding timeout in seconds. The underlying thread keeps running. |
| `reranker_max_concurrency` | `int` (>= 1) | `16` | `MEMEX_SERVER__RERANKER_MAX_CONCURRENCY` | Max concurrent reranker calls across both reranker sites. |
| `reranker_call_timeout` | `int` (>= 1) | `30` | `MEMEX_SERVER__RERANKER_CALL_TIMEOUT` | Per-call reranker timeout in seconds. The thread keeps running. |
| `ner_max_concurrency` | `int` (>= 1) | `16` | `MEMEX_SERVER__NER_MAX_CONCURRENCY` | Max concurrent NER calls. |
| `ner_call_timeout` | `int` (>= 1) | `30` | `MEMEX_SERVER__NER_CALL_TIMEOUT` | Per-call NER timeout in seconds. |
| `nli_max_concurrency` | `int` (>= 1) | `16` | `MEMEX_SERVER__NLI_MAX_CONCURRENCY` | Max concurrent NLI classify calls. Used by the lint LLM polarity gate. |

### Sub-config containers

| Key | Type | Env var | Description |
|---|---|---|---|
| `logging` | [`LoggingConfig`](#loggingconfig) | `MEMEX_SERVER__LOGGING__*` | Logging configuration. |
| `auth` | [`AuthConfig`](#authconfig) | `MEMEX_SERVER__AUTH__*` | API key authentication. |
| `cors` | [`CorsConfig`](#corsconfig) | `MEMEX_SERVER__CORS__*` | CORS configuration. |
| `rate_limit` | [`RateLimitConfig`](#ratelimitconfig) | `MEMEX_SERVER__RATE_LIMIT__*` | API rate limiting. |
| `file_store` | [`LocalFileStoreConfig` \| `S3FileStoreConfig` \| `GCSFileStoreConfig`](#filestore-backends) | `MEMEX_SERVER__FILE_STORE__*` | Note-storage backend (discriminator: `type`). |
| `meta_store` | [`PostgresMetaStoreConfig`](#postgresmetastoreconfig) | `MEMEX_SERVER__META_STORE__*` | Metadata store (discriminator: `type`, only `postgres` is supported). |
| `memory` | [`MemoryConfig`](#memoryconfig) | `MEMEX_SERVER__MEMORY__*` | Memory subsystems. |
| `document` | [`DocumentConfig`](#documentconfig) | `MEMEX_SERVER__DOCUMENT__*` | Document search and synthesis. |
| `vault_summary` | [`VaultSummaryConfig`](#vaultsummaryconfig) | `MEMEX_SERVER__VAULT_SUMMARY__*` | Periodic vault summaries. |
| `tracing` | [`TracingConfig`](#tracingconfig) | `MEMEX_SERVER__TRACING__*` | OpenTelemetry tracing. |

### Validators worth knowing

- `_check_default_db_password` — if `MEMEX_ENV=production` and `meta_store.instance.password` is the literal `"postgres"`, the server refuses to start.
- `_validate_vault_names` — warns on long names (>50 chars) or names with characters outside `[a-zA-Z0-9_\-.]`.
- `sync_default_model` — propagates `default_model` to sub-config `model=null` fields (see list above).

---

## ModelConfig

A reusable LLM-model block. Used by `server.default_model` and any sub-config that takes a `ModelConfig`.

| Key | Type | Default | Env var (suffix) | Description |
|---|---|---|---|---|
| `model` | `str` | — (required) | `__MODEL` | LiteLLM model identifier, e.g. `gemini/gemini-3-flash-preview`, `openai/gpt-4o`. |
| `base_url` | `HttpUrl \| None` | `null` | `__BASE_URL` | Base URL for the provider (Ollama, vLLM, custom). |
| `api_key` | `SecretStr \| None` | `null` | `__API_KEY` | Provider API key. |
| `max_tokens` | `int \| None` | `null` | `__MAX_TOKENS` | Maximum tokens to generate. |
| `temperature` | `float \| None` | `null` | `__TEMPERATURE` | Sampling temperature. |
| `reasoning_effort` | `ReasoningEffort \| None` | `null` | `__REASONING_EFFORT` | Reasoning effort, when the provider supports it. |
| `timeout` | `int` (>= 10) | `120` | `__TIMEOUT` | Per-request timeout in seconds. |
| `num_retries` | `int` (>= 1) | `3` | `__NUM_RETRIES` | Retries on LLM-call failure (e.g. schema validation errors). |

`ModelConfig` ships under several paths; substitute the parent path for the env-var stem. Example for `server.memory.extraction.model.timeout`: `MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__TIMEOUT`.

---

## LoggingConfig

Logging behaviour. Lives at `server.logging`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `log_file` | `str` | `~/.local/state/memex/memex.log` (platform-dependent) | `MEMEX_SERVER__LOGGING__LOG_FILE` | Path to the log file. |
| `level` | `str` | `WARNING` | `MEMEX_SERVER__LOGGING__LEVEL` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `json_output` | `bool` | `false` | `MEMEX_SERVER__LOGGING__JSON_OUTPUT` | Emit logs as JSON. |

---

## AuthConfig

API key authentication. Lives at `server.auth`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | `MEMEX_SERVER__AUTH__ENABLED` | Turn on API-key auth. |
| `keys` | `list[ApiKeyConfig]` | `[]` | `MEMEX_SERVER__AUTH__KEYS` | API keys with associated policies. |
| `exempt_paths` | `list[str]` | `["/api/v1/health","/api/v1/ready","/api/v1/metrics"]` | `MEMEX_SERVER__AUTH__EXEMPT_PATHS` | Paths that skip auth. |
| `webhook_secret` | `SecretStr \| None` | `null` | `MEMEX_SERVER__AUTH__WEBHOOK_SECRET` | Shared secret for `X-Webhook-Signature` HMAC-SHA256 validation. |

The legacy `api_keys` field is rejected on load with a migration message.

### ApiKeyConfig

One entry in `auth.keys`.

| Key | Type | Default | Description |
|---|---|---|---|
| `key` | `SecretStr` | — (required) | The API key secret, or `env:VAR_NAME` to read from an environment variable at load time. |
| `policy` | `Policy` | — (required) | One of `reader`, `writer`, `admin`. |
| `vault_ids` | `list[str] \| None` | `null` | Vault IDs/names this key may access. `null` = all vaults. |
| `read_vault_ids` | `list[str] \| None` | `null` | Extra read-only vault IDs/names. Only valid when `vault_ids` is set. |
| `description` | `str \| None` | `null` | Human-readable label. |

`Policy` values and their permission sets:

| Policy | Permissions |
|---|---|
| `reader` | `read` |
| `writer` | `read`, `write` |
| `admin` | `read`, `write`, `delete` |

Setting `read_vault_ids` while `vault_ids` is `null` raises a validation error; use a separate `reader` key instead.

---

## CorsConfig

CORS policy. Lives at `server.cors`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `origins` | `list[str]` | `["http://localhost:5173","http://localhost:3000","null"]` | `MEMEX_SERVER__CORS__ORIGINS` | Allowed origins. |
| `allow_credentials` | `bool` | `true` | `MEMEX_SERVER__CORS__ALLOW_CREDENTIALS` | Allow credentialed CORS requests. |
| `allow_methods` | `list[str]` | `["*"]` | `MEMEX_SERVER__CORS__ALLOW_METHODS` | Allowed HTTP methods. |
| `allow_headers` | `list[str]` | `["*"]` | `MEMEX_SERVER__CORS__ALLOW_HEADERS` | Allowed HTTP headers. |
| `allow_origin_regex` | `str \| None` | `(moz\|chrome)-extension://.*` | `MEMEX_SERVER__CORS__ALLOW_ORIGIN_REGEX` | Regex pattern (full-match) for additional allowed origins. Default allows browser extensions. |

---

## RateLimitConfig

SlowAPI-format rate limits. Lives at `server.rate_limit`. Format: `{count}/{second|minute|hour|day}`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | `MEMEX_SERVER__RATE_LIMIT__ENABLED` | Turn on rate limiting. |
| `ingestion` | `str` | `10/minute` | `MEMEX_SERVER__RATE_LIMIT__INGESTION` | Limit for ingestion endpoints. |
| `search` | `str` | `60/minute` | `MEMEX_SERVER__RATE_LIMIT__SEARCH` | Limit for search endpoints. |
| `batch` | `str` | `5/minute` | `MEMEX_SERVER__RATE_LIMIT__BATCH` | Limit for batch endpoints. |
| `default` | `str` | `120/minute` | `MEMEX_SERVER__RATE_LIMIT__DEFAULT` | Limit for all other endpoints. |

---

## TracingConfig

OpenTelemetry export. Lives at `server.tracing`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | `MEMEX_SERVER__TRACING__ENABLED` | Turn on OTLP tracing. |
| `endpoint` | `str` | `http://localhost:6006/v1/traces` | `MEMEX_SERVER__TRACING__ENDPOINT` | OTLP HTTP endpoint. |
| `headers` | `dict[str, str]` | `{}` | `MEMEX_SERVER__TRACING__HEADERS` | Headers for the OTLP exporter (e.g. auth tokens). |
| `service_name` | `str` | `memex` | `MEMEX_SERVER__TRACING__SERVICE_NAME` | Service name reported in traces. |

---

## PostgresInstanceConfig

Postgres connection. Lives at `server.meta_store.instance`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `host` | `str` | — (required) | `MEMEX_SERVER__META_STORE__INSTANCE__HOST` | Hostname or IP. |
| `port` | `int` | `5432` | `MEMEX_SERVER__META_STORE__INSTANCE__PORT` | Port. |
| `database` | `str` | — (required) | `MEMEX_SERVER__META_STORE__INSTANCE__DATABASE` | Database name. |
| `user` | `str` | — (required) | `MEMEX_SERVER__META_STORE__INSTANCE__USER` | Username. |
| `password` | `SecretStr` | — (required) | `MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD` | Password. Rejected as `"postgres"` when `MEMEX_ENV=production`. |

Computed: `connection_string = "postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"`.

---

## PostgresMetaStoreConfig

Postgres pool and timeouts. Lives at `server.meta_store`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `type` | `Literal["postgres"]` | `postgres` | `MEMEX_SERVER__META_STORE__TYPE` | Discriminator. Only `postgres` is supported. |
| `instance` | `PostgresInstanceConfig` | — (required) | `MEMEX_SERVER__META_STORE__INSTANCE__*` | Connection params. |
| `pool_size` | `int` | `20` | `MEMEX_SERVER__META_STORE__POOL_SIZE` | Base connection-pool size. |
| `max_overflow` | `int` | `30` | `MEMEX_SERVER__META_STORE__MAX_OVERFLOW` | Maximum overflow connections beyond `pool_size`. |
| `statement_timeout_ms` | `int` | `30000` | `MEMEX_SERVER__META_STORE__STATEMENT_TIMEOUT_MS` | Statement timeout in milliseconds. |

---

## LocalFileStoreConfig

Local-disk notes. Lives at `server.file_store` when `type=local`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `type` | `Literal["local"]` | `local` | `MEMEX_SERVER__FILE_STORE__TYPE` | Discriminator. |
| `root` | `str` | `~/.local/share/memex` (platform-dependent) | `MEMEX_SERVER__FILE_STORE__ROOT` | Root directory for notes. |
| `max_concurrent_connections` | `int` | `10` | `MEMEX_SERVER__FILE_STORE__MAX_CONCURRENT_CONNECTIONS` | Max concurrent filesystem operations. |

Computed: `notes_dir = "{root}/notes"`. `root_normalized` expands and resolves the root path.

## S3FileStoreConfig

S3-compatible note storage. Lives at `server.file_store` when `type=s3`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `type` | `Literal["s3"]` | `s3` | `MEMEX_SERVER__FILE_STORE__TYPE` | Discriminator. |
| `bucket` | `str` | — (required) | `MEMEX_SERVER__FILE_STORE__BUCKET` | S3 bucket name. |
| `root` | `str` | `""` | `MEMEX_SERVER__FILE_STORE__ROOT` | Key prefix inside the bucket. |
| `region` | `str \| None` | `null` | `MEMEX_SERVER__FILE_STORE__REGION` | AWS region. |
| `endpoint_url` | `str \| None` | `null` | `MEMEX_SERVER__FILE_STORE__ENDPOINT_URL` | Custom endpoint (e.g. MinIO). |
| `access_key_id` | `SecretStr \| None` | `null` | `MEMEX_SERVER__FILE_STORE__ACCESS_KEY_ID` | AWS access key ID. |
| `secret_access_key` | `SecretStr \| None` | `null` | `MEMEX_SERVER__FILE_STORE__SECRET_ACCESS_KEY` | AWS secret access key. |
| `session_token` | `SecretStr \| None` | `null` | `MEMEX_SERVER__FILE_STORE__SESSION_TOKEN` | AWS session token. |
| `max_concurrent_connections` | `int` | `10` | `MEMEX_SERVER__FILE_STORE__MAX_CONCURRENT_CONNECTIONS` | Max concurrent connections. |

## GCSFileStoreConfig

Google Cloud Storage notes. Lives at `server.file_store` when `type=gcs`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `type` | `Literal["gcs"]` | `gcs` | `MEMEX_SERVER__FILE_STORE__TYPE` | Discriminator. |
| `bucket` | `str` | — (required) | `MEMEX_SERVER__FILE_STORE__BUCKET` | GCS bucket name. |
| `root` | `str` | `""` | `MEMEX_SERVER__FILE_STORE__ROOT` | Key prefix inside the bucket. |
| `project` | `str \| None` | `null` | `MEMEX_SERVER__FILE_STORE__PROJECT` | GCP project ID. |
| `token` | `str \| None` | `null` | `MEMEX_SERVER__FILE_STORE__TOKEN` | Path to a JSON service-account key, `google_default`, or `anon`. |
| `endpoint_url` | `str \| None` | `null` | `MEMEX_SERVER__FILE_STORE__ENDPOINT_URL` | Custom endpoint (e.g. GCS emulator). |
| `max_concurrent_connections` | `int` | `10` | `MEMEX_SERVER__FILE_STORE__MAX_CONCURRENT_CONNECTIONS` | Max concurrent connections. |

### FileStore backends

The `server.file_store` field is discriminated by `type` (`local` / `s3` / `gcs`); the three classes above are mutually exclusive.

---

## Model backends

The `server.embedding_model`, `server.memory.retrieval.reranker`, and `server.memory.lint_llm.polarity.backend` fields each accept a model-backend object discriminated by `type`. The three classes:

### OnnxBackend (default for embedding, reranker, NLI)

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `Literal["onnx"]` | `onnx` | Discriminator. No other fields. |

### LitellmEmbeddingBackend

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `Literal["litellm"]` | `litellm` | Discriminator. |
| `model` | `str` | — (required) | LiteLLM model string, e.g. `openai/text-embedding-3-small`. |
| `api_base` | `HttpUrl \| None` | `null` | API base URL. Required for Ollama / TEI / vLLM. |
| `api_key` | `SecretStr \| None` | `null` | API key. Also overridable via provider env vars. |
| `dimensions` | `int \| None` | `null` | Output dimensions (Matryoshka / dimension-reduction). Must match the DB vector column or you need a migration. |

### LitellmRerankerBackend

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `Literal["litellm"]` | `litellm` | Discriminator. |
| `model` | `str` | — (required) | LiteLLM rerank string, e.g. `cohere/rerank-v3.5`. |
| `api_base` | `HttpUrl \| None` | `null` | API base URL for self-hosted reranking. |
| `api_key` | `SecretStr \| None` | `null` | Provider API key. |

### LitellmNLIBackend

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `Literal["litellm"]` | `litellm` | Discriminator. |
| `model` | `str` | — (required) | LiteLLM chat model used for NLI classification. |
| `api_base` | `HttpUrl \| None` | `null` | API base URL. |
| `api_key` | `SecretStr \| None` | `null` | Provider API key. |

### DisabledBackend

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `Literal["disabled"]` | `disabled` | Discriminator. No other fields. Skips model loading entirely. |

The `embedding_model` discriminator accepts `onnx` or `litellm`. The `reranker` and `polarity.backend` discriminators accept `onnx`, `litellm`, or `disabled`.

---

## MemoryConfig

Container for the memory engine. Lives at `server.memory`.

| Key | Type | Env var (suffix) | Description |
|---|---|---|---|
| `extraction` | [`ExtractionConfig`](#extractionconfig) | `__EXTRACTION__*` | Fact extraction. |
| `reflection` | [`ReflectionConfig`](#reflectionconfig) | `__REFLECTION__*` | Hindsight reflection. |
| `retrieval` | [`RetrievalConfig`](#retrievalconfig) | `__RETRIEVAL__*` | TEMPR retrieval. |
| `contradiction` | [`ContradictionConfig`](#contradictionconfig) | `__CONTRADICTION__*` | Retain-time contradiction. |
| `outcomes` | [`OutcomesConfig`](#outcomesconfig) | `__OUTCOMES__*` | Outcome attribution. |
| `consolidation` | [`ConsolidationConfig`](#consolidationconfig) | `__CONSOLIDATION__*` | Vault consolidation. |
| `consolidate_rate_limit` | [`ConsolidateRateLimitConfig`](#consolidateratelimitconfig) | `__CONSOLIDATE_RATE_LIMIT__*` | Consolidate rate limit. |
| `circuit_breaker` | [`CircuitBreakerConfig`](#circuitbreakerconfig) | `__CIRCUIT_BREAKER__*` | LLM circuit breaker. |
| `lint` | [`LintConfig`](#lintconfig) | `__LINT__*` | Rule-based lint. |
| `lint_llm` | [`LintLLMConfig`](#lintllmconfig) | `__LINT_LLM__*` | LLM-driven lint. |
| `entity_maintenance` | [`EntityMaintenanceConfig`](#entitymaintenanceconfig) | `__ENTITY_MAINTENANCE__*` | Entity-cluster collapse. |
| `deprioritize_score` | [`DeprioritizeScoreConfig`](#deprioritizescoreconfig) | `__DEPRIORITIZE_SCORE__*` | FSFM deprio scorer. |

---

## ExtractionConfig

Fact extraction. Lives at `server.memory.extraction`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__*` | LLM for extraction. |
| `text_splitting` | [`PageIndexTextSplitting` \| `SimpleTextSplitting`](#pageindextextsplitting) | `PageIndexTextSplitting()` | `MEMEX_SERVER__MEMORY__EXTRACTION__TEXT_SPLITTING__*` | Splitter (discriminator: `strategy`). |
| `max_concurrency` | `int` | `5` | `MEMEX_SERVER__MEMORY__EXTRACTION__MAX_CONCURRENCY` | Max concurrent LLM calls for fact extraction. |
| `wedge_watchdog_seconds` | `int \| None` (>= 1) | `null` | `MEMEX_SERVER__MEMORY__EXTRACTION__WEDGE_WATCHDOG_SECONDS` | Opt-in OS-thread watchdog that dumps tracebacks via `faulthandler` if no stage decrements within this many seconds. |
| `intent_risk_classifier_enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__EXTRACTION__INTENT_RISK_CLASSIFIER_ENABLED` | Honour the LLM-emitted intent/risk on each fact. When `false`, every fact is forced to `durable`/`none` and per-fact metrics are suppressed. |

Computed: `active_strategy` returns `simple` or `page_index` from the configured `text_splitting`.

## SimpleTextSplitting

Flat content-defined chunking. Lives at `server.memory.extraction.text_splitting` when `strategy=simple`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `strategy` | `Literal["simple"]` | `simple` | `MEMEX_SERVER__MEMORY__EXTRACTION__TEXT_SPLITTING__STRATEGY` | Discriminator. |
| `chunk_size_tokens` | `int` | `1000` | `…__CHUNK_SIZE_TOKENS` | Target size for content-defined blocks in tokens. |
| `chunk_overlap_tokens` | `int` | `50` | `…__CHUNK_OVERLAP_TOKENS` | Overlapping tokens between chunks. |

## PageIndexTextSplitting

Hierarchical document chunking. Lives at `server.memory.extraction.text_splitting` when `strategy=page_index` (the default).

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `strategy` | `Literal["page_index"]` | `page_index` | `…__STRATEGY` | Discriminator. |
| `scan_chunk_size_tokens` | `int` | `20000` | `…__SCAN_CHUNK_SIZE_TOKENS` | Max tokens per LLM scan call. Documents under this limit scan in one call. |
| `block_token_target` | `int` | `2000` | `…__BLOCK_TOKEN_TARGET` | Target token count per block. |
| `short_doc_threshold_tokens` | `int` | `500` | `…__SHORT_DOC_THRESHOLD_TOKENS` | Documents below this count with no headers bypass PageIndex. |
| `max_node_length_tokens` | `int` | `1250` | `…__MAX_NODE_LENGTH_TOKENS` | Max tokens per node before refinement triggers. |
| `min_node_tokens` | `int` | `0` | `…__MIN_NODE_TOKENS` | Nodes with this many tokens or fewer are dropped. |
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `…__MODEL__*` | Model for PageIndex LLM calls. |
| `scan_max_concurrency` | `int` (>= 1) | `20` | `…__SCAN_MAX_CONCURRENCY` | Max concurrent scan calls. Reduce on memory-constrained hosts. |
| `refine_max_concurrency` | `int` (>= 1) | `20` | `…__REFINE_MAX_CONCURRENCY` | Max concurrent refine-tree calls. |
| `summarize_max_concurrency` | `int` (>= 1) | `20` | `…__SUMMARIZE_MAX_CONCURRENCY` | Max concurrent summary calls (leaf, parent, block). |
| `gap_rescan_threshold_tokens` | `int` (>= 500) | `2000` | `…__GAP_RESCAN_THRESHOLD_TOKENS` | Minimum gap (tokens) between detected headers that triggers a secondary re-scan. |

---

## RetrievalConfig

TEMPR retrieval, fusion, reranking, exploration. Lives at `server.memory.retrieval`.

> Several knobs below sit on the empirical-precedent caveat: `decay_alpha`, `confidence_alpha`, `reranking_mw_alpha`, `reranking_recency_alpha`, `reranking_temporal_alpha`, `mmr_lambda`, `rrf_k`, `mmr_embedding_weight`, `mmr_entity_weight`, `composite_boost_log_clip`, `exploration_*`, `superseded_threshold`, `temporal_decay_*`, `anisotropy_*`, `graph_semantic_*`, `mw_ema_half_life_days`. Defaults reflect literature precedent or an early empirical pass — not fine-tuned values. Tune in your environment.

### Pool and budget

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `token_budget` | `int` | `1000` | `MEMEX_SERVER__MEMORY__RETRIEVAL__TOKEN_BUDGET` | Maximum token budget for retrieval results (greedy packing). |
| `candidate_pool_size` | `int` | `60` | `…__CANDIDATE_POOL_SIZE` | Candidates retrieved per strategy. |
| `rrf_k` | `int` | `60` | `…__RRF_K` | Reciprocal Rank Fusion constant. Higher = more uniform blending. |
| `fact_type_partitioned_rrf` | `bool` | `false` | `…__FACT_TYPE_PARTITIONED_RRF` | Run RRF independently per fact type, then interleave results. |
| `fact_type_budget` | `int` | `20` | `…__FACT_TYPE_BUDGET` | Per-type candidate limit when `fact_type_partitioned_rrf=true`. |

### Strategy toggles and graph

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `retrieval_strategies` | [`SearchStrategiesConfig`](#searchstrategiesconfig) | all `true` | `…__RETRIEVAL_STRATEGIES__*` | On/off per strategy. |
| `graph_retriever_type` | `str` | `entity_cooccurrence` | `…__GRAPH_RETRIEVER_TYPE` | One of `entity_cooccurrence`, `causal`, `link_expansion`. |
| `graph_semantic_seeding` | `bool` | `true` | `…__GRAPH_SEMANTIC_SEEDING` | Use semantic seeding to bootstrap graph traversal. |
| `graph_semantic_seed_top_k` | `int` | `5` | `…__GRAPH_SEMANTIC_SEED_TOP_K` | Top-K units used to discover seed entities. |
| `graph_semantic_seed_weight` | `float` | `0.7` | `…__GRAPH_SEMANTIC_SEED_WEIGHT` | Weight of seed entities (NER weight = 1.0). |
| `similarity_threshold` | `float` | `0.3` | `…__SIMILARITY_THRESHOLD` | Minimum `pg_trgm` similarity for entity name matching. |
| `causal_weight_threshold` | `float` | `0.3` | `…__CAUSAL_WEIGHT_THRESHOLD` | Minimum link weight for causal graph expansion in `memory_links`. |
| `link_expansion_causal_threshold` | `float` | `0.3` | `…__LINK_EXPANSION_CAUSAL_THRESHOLD` | Minimum weight for causal links in the link-expansion graph strategy. |

### Temporal

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `temporal_decay_days` | `float` | `30.0` | `…__TEMPORAL_DECAY_DAYS` | Half-life in days for temporal decay scoring. |
| `temporal_decay_base` | `float` | `2.0` | `…__TEMPORAL_DECAY_BASE` | Base for temporal-decay exponential: `score = base ^ (-days / decay_days)`. |
| `temporal_extraction_enabled` | `bool` | `true` | `…__TEMPORAL_EXTRACTION_ENABLED` | Enable NLP-based temporal-constraint extraction (`dateparser`). |
| `temporal_concretization_enabled` | `bool` | `true` | `…__TEMPORAL_CONCRETIZATION_ENABLED` | Enable LLM-assisted concretization for unresolved temporal expressions. |

### MMR

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `mmr_lambda` | `float \| None` | `0.9` | `…__MMR_LAMBDA` | MMR diversity λ. `null` disables, `1.0` = pure relevance, `0.0` = max diversity. |
| `mmr_embedding_weight` | `float` | `0.6` | `…__MMR_EMBEDDING_WEIGHT` | Embedding-cosine weight in the hybrid MMR kernel. |
| `mmr_entity_weight` | `float` | `0.4` | `…__MMR_ENTITY_WEIGHT` | Entity-Jaccard weight in the hybrid MMR kernel. |

### Reranking and boosts

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `reranker` | [`OnnxBackend` \| `LitellmRerankerBackend` \| `DisabledBackend`](#model-backends) | `OnnxBackend()` | `…__RERANKER__*` | Reranker backend (discriminator: `type`). |
| `reranker_batch_size` | `int` | `0` | `…__RERANKER_BATCH_SIZE` | Max documents per ONNX reranker inference call. `0` = no batching. |
| `cross_encoder_cache_enabled` | `bool` | `true` | `…__CROSS_ENCODER_CACHE_ENABLED` | In-process TTL cache for cross-encoder scores. Set `false` to bypass. |
| `cross_encoder_cache_size` | `int` (>= 1) | `10000` | `…__CROSS_ENCODER_CACHE_SIZE` | Maximum `(model_version, query_hash, unit_id)` entries in the cache. |
| `cross_encoder_cache_ttl_seconds` | `int` (>= 1) | `86400` | `…__CROSS_ENCODER_CACHE_TTL_SECONDS` | TTL for the cross-encoder cache (default 24h). |
| `reranking_recency_alpha` | `float` | `0.2` | `…__RERANKING_RECENCY_ALPHA` | Recency-boost strength for reranking. `0` = no boost. |
| `reranking_temporal_alpha` | `float` | `0.2` | `…__RERANKING_TEMPORAL_ALPHA` | Temporal-proximity-boost strength for reranking. `0` = no boost. |
| `reranking_mw_alpha` | `float` | `0.3` | `…__RERANKING_MW_ALPHA` | Memory Worth boost strength for reranking. `0` = no MW influence. |
| `confidence_alpha` | `float` (`[0.0, 2.0]`) | `0.0` | `…__CONFIDENCE_ALPHA` | Confidence boost strength for reranking. Off by default; raise after calibration data accumulates. |
| `decay_alpha` | `float` (`[0.0, 2.0]`) | `0.0` | `…__DECAY_ALPHA` | FSFM-lite decay boost strength for reranking. Off by default. |
| `composite_boost_log_clip` | `float` | `inf` | `…__COMPOSITE_BOOST_LOG_CLIP` | Symmetric clip on the aggregate metadata multiplier in log-space. `inf` is a no-op. Accepts `"inf"`/`"+inf"`/`"infinity"` strings. Serialized as `"inf"` in JSON. |
| `certainty_modulation_enabled` | `bool` | `false` | `…__CERTAINTY_MODULATION_ENABLED` | When `true`, multiplies the confidence boost by a certainty factor from the closed-form Beta(1,1) posterior. |
| `mw_ema_half_life_days` | `float` (> 0) | `60.0` | `…__MW_EMA_HALF_LIFE_DAYS` | Half-life in days for EMA decay of Memory Worth counters (only when `Vault.mw_mode=ema`). |

### Anisotropy correction

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `anisotropy_window_size` | `int` | `1024` | `…__ANISOTROPY_WINDOW_SIZE` | Sliding window for Z-score anisotropy correction. `0` disables. |
| `anisotropy_min_samples` | `int` | `32` | `…__ANISOTROPY_MIN_SAMPLES` | Minimum observations before correction activates. |

### Exploration injection

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `exploration_mode` | `Literal["epsilon_greedy","thompson","off"]` | `epsilon_greedy` | `…__EXPLORATION_MODE` | Algorithm for exploration injection. |
| `exploration_epsilon` | `float` | `0.05` | `…__EXPLORATION_EPSILON` | Probability of injecting exploration units (epsilon-greedy only). |
| `exploration_max_injections` | `int` | `2` | `…__EXPLORATION_MAX_INJECTIONS` | Max units injected per call (when the outer roll succeeds). |
| `exploration_low_mw_threshold` | `int` | `5` | `…__EXPLORATION_LOW_MW_THRESHOLD` | Units with `success_co_count + failure_co_count` below this are eligible. |

`exploration_epsilon` is ignored when `exploration_mode != epsilon_greedy`. A non-default `exploration_epsilon` under a non-greedy mode logs (not raises) at startup.

### Related-note enrichment and supersession

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `superseded_threshold` | `float` | `0.3` | `…__SUPERSEDED_THRESHOLD` | Confidence below this marks a unit as superseded. |
| `relations` | [`RelationConfig`](#relationconfig) | see below | `…__RELATIONS__*` | Related-note enrichment. |
| `fsfm_branch_enabled` | `bool` | `true` | `…__FSFM_BRANCH_ENABLED` | Pre-reranker FSFM filter (Forgetting-Survival-Frequency-Magnitude). |

---

## RelationConfig

Related-note enrichment in search results. Lives at `server.memory.retrieval.relations`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `top_k_related` | `int` | `3` | `MEMEX_SERVER__MEMORY__RETRIEVAL__RELATIONS__TOP_K_RELATED` | Max related notes per result. `0` disables. |
| `max_shared_entities` | `int` | `0` | `…__MAX_SHARED_ENTITIES` | Max entity names included per related note. `0` omits the field. |
| `entity_fanout_cap` | `int` | `50` | `…__ENTITY_FANOUT_CAP` | Entities with `mention_count` above this are excluded from relation queries (too generic). |
| `max_links` | `int` | `3` | `…__MAX_LINKS` | Max contradiction links inlined per result. `0` omits all inline links. |

---

## SearchStrategiesConfig

Memory-search strategies. Lives at `server.memory.retrieval.retrieval_strategies`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `semantic` | `bool` | `true` | `…__RETRIEVAL_STRATEGIES__SEMANTIC` | Semantic (vector) search. |
| `keyword` | `bool` | `true` | `…__RETRIEVAL_STRATEGIES__KEYWORD` | Keyword (BM25) search. |
| `graph` | `bool` | `true` | `…__RETRIEVAL_STRATEGIES__GRAPH` | Graph (entity cooccurrence / causal / link expansion). |
| `temporal` | `bool` | `true` | `…__RETRIEVAL_STRATEGIES__TEMPORAL` | Temporal recency. |
| `mental_model` | `bool` | `true` | `…__RETRIEVAL_STRATEGIES__MENTAL_MODEL` | Mental-model search (memory only). |

## DocSearchStrategiesConfig

Document-search strategies. Lives at `server.document.search_strategies`. Same fields as `SearchStrategiesConfig` but without `mental_model`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `semantic` | `bool` | `true` | `MEMEX_SERVER__DOCUMENT__SEARCH_STRATEGIES__SEMANTIC` | Semantic. |
| `keyword` | `bool` | `true` | `…__KEYWORD` | Keyword. |
| `graph` | `bool` | `true` | `…__GRAPH` | Graph. |
| `temporal` | `bool` | `true` | `…__TEMPORAL` | Temporal. |

---

## ReflectionConfig

Hindsight Reflection Engine. Lives at `server.memory.reflection`.

> The three priority weights (`weight_urgency`, `weight_importance`, `weight_resonance`) must sum to **exactly 1.0**. The model validator rejects sums greater than 1.0.

### Model and concurrency

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `MEMEX_SERVER__MEMORY__REFLECTION__MODEL__*` | Override LLM for reflection. |
| `max_concurrency` | `int` (> 1) | `3` | `…__MAX_CONCURRENCY` | Max concurrent entities reflected on per batch. |
| `tail_sampling_rate` | `float` (`[0, 1]`) | `0.05` | `…__TAIL_SAMPLING_RATE` | Tail-sampling rate for traces. |

### Priority weights (literature-precedent — not fine-tuned)

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `weight_urgency` | `float` (>= 0) | `0.5` | `…__WEIGHT_URGENCY` | Weight for Accumulated Evidence (Urgency). |
| `weight_importance` | `float` (>= 0) | `0.2` | `…__WEIGHT_IMPORTANCE` | Weight for Global Frequency (Importance). |
| `weight_resonance` | `float` (>= 0) | `0.3` | `…__WEIGHT_RESONANCE` | Weight for User Retrieval (Resonance). |

### Search

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `search_limit` | `int` (>= 0) | `10` | `…__SEARCH_LIMIT` | Candidates to retrieve in the Hunt phase. |
| `similarity_threshold` | `float` (>= 0) | `0.6` | `…__SIMILARITY_THRESHOLD` | Min similarity for evidence retrieval. |
| `min_priority` | `float` (`[0, 1]`) | `0.3` | `…__MIN_PRIORITY` | Minimum priority score for an entity to be selected. |

### Background loop

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `background_reflection_enabled` | `bool` | `true` | `…__BACKGROUND_REFLECTION_ENABLED` | Run the periodic reflection loop. |
| `background_reflection_interval_seconds` | `int` (>= 10) | `600` | `…__BACKGROUND_REFLECTION_INTERVAL_SECONDS` | Seconds between background reflection runs. |
| `background_reflection_batch_size` | `int` (> 0) | `10` | `…__BACKGROUND_REFLECTION_BATCH_SIZE` | Entities per background reflection batch. |
| `enrichment_enabled` | `bool` | `true` | `…__ENRICHMENT_ENABLED` | Run Phase 6 enrichment after reflection (evolves contributing memory units). |
| `stale_processing_timeout_seconds` | `int` (>= 60) | `1800` | `…__STALE_PROCESSING_TIMEOUT_SECONDS` | Seconds after which a `PROCESSING` item is considered stale and reset to `PENDING`. |

### Variance prioritisation and observation refresh

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `variance_prioritisation_enabled` | `bool` | `false` | `…__VARIANCE_PRIORITISATION_ENABLED` | Sort each entity bucket by closed-form Beta(1,1) posterior variance. Ships inert at the default. |
| `refresh_obs_priority_lane` | `bool` | `true` | `…__REFRESH_OBS_PRIORITY_LANE` | When `true`, refresh-observation tasks claim priority over regular reflect tasks. |
| `min_evidence_for_obs_retention` | `int` (>= 0) | `2` | `…__MIN_EVIDENCE_FOR_OBS_RETENTION` | Override LLM `should_drop=true` and keep the observation when this many evidence items survive. |
| `refresh_obs_retry_backoff_min_seconds` | `int` (>= 1) | `2` | `…__REFRESH_OBS_RETRY_BACKOFF_MIN_SECONDS` | Min jitter for `AdvisoryLockTakenError` re-claim. |
| `refresh_obs_retry_backoff_max_seconds` | `int` (>= 1) | `5` | `…__REFRESH_OBS_RETRY_BACKOFF_MAX_SECONDS` | Max jitter for `AdvisoryLockTakenError` re-claim. Must be >= min. |
| `reconcile_historical_deprios_on_boot` | `bool` | `true` | `…__RECONCILE_HISTORICAL_DEPRIOS_ON_BOOT` | Repair refresh-task rows missing for any deprio'd MU on scheduler ticks. |
| `reconcile_batch_size` | `int` (`[1, 500]`) | `50` | `…__RECONCILE_BATCH_SIZE` | Per-tick reconcile-pass batch size. |
| `summarize_node_rate_limit` | [`SummarizeNodeRateLimitConfig`](#summarizenoderatelimitconfig) | see below | `…__SUMMARIZE_NODE_RATE_LIMIT__*` | Per-(entity, vault) rate limit for `memex_memory_summarize_node`. |

---

## SummarizeNodeRateLimitConfig

Token-bucket cap for `memex_memory_summarize_node`. Lives at `server.memory.reflection.summarize_node_rate_limit`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `…__SUMMARIZE_NODE_RATE_LIMIT__ENABLED` | Turn off in tests with `false`. |
| `per_entity_per_seconds` | `int` (> 0) | `60` | `…__PER_ENTITY_PER_SECONDS` | Window in seconds across which `burst` calls are allowed per (entity, vault). |
| `burst` | `int` (> 0) | `1` | `…__BURST` | Token-bucket capacity. `1` = no bursting. |
| `max_keys` | `int` (> 0) | `10000` | `…__MAX_KEYS` | LRU eviction cap on tracked `(entity_id, vault_id)` keys. |

---

## ContradictionConfig

Retain-time contradiction detection. Lives at `server.memory.contradiction`.

> Thresholds are literature-precedent — not fine-tuned.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__CONTRADICTION__ENABLED` | Enable contradiction detection after extraction. |
| `alpha` | `float` | `0.1` | `…__ALPHA` | Hindsight step size for confidence adjustment. |
| `similarity_threshold` | `float` | `0.5` | `…__SIMILARITY_THRESHOLD` | Min cosine similarity for candidate retrieval. |
| `similarity_threshold_explicit_claim` | `float` (`[0, 1]`) | `0.35` | `…__SIMILARITY_THRESHOLD_EXPLICIT_CLAIM` | Looser threshold for units with an explicit resolution/contradiction claim. Must be <= `similarity_threshold`. |
| `max_candidates_per_unit` | `int` | `15` | `…__MAX_CANDIDATES_PER_UNIT` | Max candidates compared per flagged unit. |
| `claim_too_aggressive_max_links` | `int` (>= 1) | `5` | `…__CLAIM_TOO_AGGRESSIVE_MAX_LINKS` | Lint threshold for the `claim_too_aggressive` rule. |
| `superseded_threshold` | `float` | `0.3` | `…__SUPERSEDED_THRESHOLD` | Confidence below this = superseded. |
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `…__MODEL__*` | LLM for classification. |

---

## ConsolidationConfig

Vault-level consolidation. Lives at `server.memory.consolidation`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__CONSOLIDATION__ENABLED` | Run the per-vault consolidation tick. |
| `cadence_seconds` | `int` (>= 60) | `86400` | `…__CADENCE_SECONDS` | Wall-clock interval between ticks (default 24h). |
| `units_per_tick` | `int` (>= 1) | `500` | `…__UNITS_PER_TICK` | Per-tick budget (oldest-first). |
| `entity_lock_timeout_seconds` | `float` (`[0.1, 60.0]`) | `5.0` | `…__ENTITY_LOCK_TIMEOUT_SECONDS` | Per-entity advisory-lock timeout when racing with `reconsolidate`. |

---

## ConsolidateRateLimitConfig

Per-vault cap for `memex_memory_consolidate`. Lives at `server.memory.consolidate_rate_limit`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__CONSOLIDATE_RATE_LIMIT__ENABLED` | Turn off in tests with `false`. |
| `per_vault_per_seconds` | `int` (> 0) | `3600` | `…__PER_VAULT_PER_SECONDS` | Window in seconds across which `burst` calls are allowed per vault. |
| `burst` | `int` (> 0) | `1` | `…__BURST` | Token-bucket capacity. `1` = no bursting. |
| `max_keys` | `int` (> 0) | `10000` | `…__MAX_KEYS` | LRU eviction cap on tracked `vault_id` keys. |

---

## OutcomesConfig

Outcome-attribution policy. Lives at `server.memory.outcomes`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `coverage_check_mode` | `Literal["strict","permissive"]` | `permissive` | `MEMEX_SERVER__MEMORY__OUTCOMES__COVERAGE_CHECK_MODE` | `strict` rejects partial coverage; `permissive` records `coverage_ratio`. |
| `contradiction_failure_weight` | `float` (`[0, 1]`) | `0.5` | `…__CONTRADICTION_FAILURE_WEIGHT` | Per-link failure-counter bump on `weakens`/`contradicts`. Stored as int via half-up rounding. |
| `mw_mode_default` | `Literal["stationary","ema"]` | `ema` | `…__MW_MODE_DEFAULT` | Default Memory Worth mode for new vaults. Per-vault `Vault.mw_mode` overrides. |

---

## CircuitBreakerConfig

LLM circuit breaker. Lives at `server.memory.circuit_breaker`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__CIRCUIT_BREAKER__ENABLED` | Turn off in tests with `false`. |
| `failure_threshold` | `int` (>= 1) | `5` | `…__FAILURE_THRESHOLD` | Consecutive failures before opening. |
| `reset_timeout_seconds` | `float` (> 0) | `60.0` | `…__RESET_TIMEOUT_SECONDS` | Seconds open before allowing a probe. |

---

## LintConfig

Rule-based maintenance linter. Lives at `server.memory.lint`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__LINT__ENABLED` | Enable periodic lint runs. |
| `interval_seconds` | `int` (>= 60) | `21600` (6h) | `…__INTERVAL_SECONDS` | Interval between lint runs. |
| `confidence_gate` | [`LintConfidenceGate`](#lintconfidencegate) | see below | `…__CONFIDENCE_GATE__*` | Confidence/variance gate for findings. |
| `external_proposals` | [`ExternalLintProposalsConfig`](#externallintproposalsconfig) | see below | `…__EXTERNAL_PROPOSALS__*` | Bounds for externally-submitted lint proposals. |

## ExternalLintProposalsConfig

Bounds for externally-submitted lint proposals (the agent-skill ingress).
Lives at `server.memory.lint.external_proposals`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `cooldown_days` | `int` (>= 0) | `30` | `MEMEX_SERVER__MEMORY__LINT__EXTERNAL_PROPOSALS__COOLDOWN_DAYS` | Days after a resolution/dismissal during which an identical external proposal (same rule/target/vault) is suppressed. `0` disables the cooldown. |
| `max_batch` | `int` (1–1000) | `100` | `…__MAX_BATCH` | Maximum proposals accepted in a single submission request. |
| `require_vault` | `bool` | `true` | `…__REQUIRE_VAULT` | Reject external proposals without a vault — global (NULL-vault) findings stay internal-only, and the pending-dedup index does not deduplicate NULL vaults. When disabled, the resulting NULL-vault external findings resolve only via `no_op`/dismiss (mutating actions need `vaults_affected` evidence, a server-owned key submitters cannot set). |

## LintConfidenceGate

Per-lint confidence/variance gate. Lives at `server.memory.lint.confidence_gate` and `server.memory.lint_llm.confidence_gate`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `confidence_min` | `float` (`[0, 1]`) | `0.0` | `…__CONFIDENCE_GATE__CONFIDENCE_MIN` | Minimum confidence (mean) for a finding to surface. `0.0` = no floor. |
| `variance_max` | `float` (`[0, 1/12]`) | `1/12` | `…__CONFIDENCE_GATE__VARIANCE_MAX` | Maximum variance for a finding to surface. `1/12` = no ceiling. |

A finding is suppressed when `confidence < confidence_min` OR `variance > variance_max`. Cold-start units have variance = `1/12`, so any non-trivial `variance_max` ceiling skips them. Avoid `variance_max=0.0` — the predicate is `variance > variance_max`, so it blocks essentially every unit. Use a small positive value (e.g. `0.001`) for tight gating.

---

## LintLLMConfig

Surprise-gated LLM-driven lint. Lives at `server.memory.lint_llm`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__LINT_LLM__ENABLED` | Master switch; `false` makes the tick a no-op. |
| `interval_seconds` | `int` (>= 60) | `21600` (6h) | `…__INTERVAL_SECONDS` | Interval between lint ticks. |
| `units_per_tick` | `int` (>= 1) | `20` | `…__UNITS_PER_TICK` | Max candidate units per tick per vault. |
| `surprise_threshold` | `float` (`[0, 1]`) | `0.7` | `…__SURPRISE_THRESHOLD` | Minimum surprise score for a unit to be eligible. |
| `cost_cap_per_24h` | `int` (>= 0) | `10` | `…__COST_CAP_PER_24H` | Max LLM lint calls per vault per 24h. `0` disables the tick. |
| `deferred_queue_cap` | `int` (>= 0) | `100` | `…__DEFERRED_QUEUE_CAP` | Max `llm_deferred` proposal rows per vault. Excess evicted oldest-first. |
| `surprise_k` | `int` (>= 1) | `8` | `…__SURPRISE_K` | Top-k peer-similarity count for the surprise score. |
| `checks` | [`LintLLMChecksConfig`](#lintllmchecksconfig) | all enabled | `…__CHECKS__*` | Per-check feature flags. |
| `confidence_gate` | [`LintConfidenceGate`](#lintconfidencegate) | see below | `…__CONFIDENCE_GATE__*` | Confidence/variance gate before LLM check. |
| `polarity` | [`NLIPolarityConfig`](#nlipolarityconfig) | see below | `…__POLARITY__*` | NLI polarity fallback. |
| `propose_winner_min_confidence` | `float` (`[0, 1]`) | `0.6` | `…__PROPOSE_WINNER_MIN_CONFIDENCE` (also `MEMEX_SERVER_LINT_LLM_PROPOSE_WINNER_MIN_CONFIDENCE`) | Definitive verdicts below this confidence are downgraded to `inconclusive`. |

---

## LintLLMChecksConfig

Per-check toggles for the LLM-lint signatures. Lives at `server.memory.lint_llm.checks`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `semantic_contradiction` | [`LintLLMCheckConfig`](#lintllmcheckconfig) | `enabled=true` | `…__CHECKS__SEMANTIC_CONTRADICTION__*` | `CheckSemanticContradiction` signature. |
| `schema_drift` | [`LintLLMCheckConfig`](#lintllmcheckconfig) | `enabled=true` | `…__CHECKS__SCHEMA_DRIFT__*` | `CheckSchemaDrift` signature. |
| `propose_contradiction_winner` | [`LintLLMCheckConfig`](#lintllmcheckconfig) | `enabled=true` | `…__CHECKS__PROPOSE_CONTRADICTION_WINNER__*` | `ProposeContradictionWinner` signature. |

## LintLLMCheckConfig

One per-check flag.

| Key | Type | Default | Env var (suffix) | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `__ENABLED` | Invoke this check when the surprise gate fires. |

---

## NLIPolarityConfig

NLI polarity gate for contradiction detection. Lives at `server.memory.lint_llm.polarity`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__LINT_LLM__POLARITY__ENABLED` | Kill switch. `false` falls back to cosine-only. |
| `backend` | `OnnxBackend \| LitellmNLIBackend \| DisabledBackend` | `OnnxBackend()` | `…__POLARITY__BACKEND__*` | NLI backend (discriminator: `type`). |
| `polarity_threshold` | `float` (`[0, 1]`) | `0.6` | `…__POLARITY__POLARITY_THRESHOLD` | Minimum contradiction probability to clear the surprise gate. |
| `rate_limit_per_vault_per_hour` | `int \| None` (>= 1) | `null` | `…__POLARITY__RATE_LIMIT_PER_VAULT_PER_HOUR` | Per-vault hourly cap. `null` = unlimited. |

The model validator accepts the legacy flat `{type: ...}` form and lifts `type` under `backend.type`.

---

## EntityMaintenanceConfig

Cross-batch entity-cluster collapse. Lives at `server.memory.entity_maintenance`. Off by default.

> The `cluster_min_threshold` must be <= `pair_threshold`; the model validator rejects otherwise. Two entities whose canonical names are identical after case-folding + whitespace-stripping always cluster via an exact-name fast path (score 1.0), regardless of `pair_threshold` — this is what surfaces `ACME Corp` / `acme corp` and `Marc de haas` / `Marc de Haas`. The threshold governs only non-identical near-duplicates and is kept high (0.85): a token-insertion variant like `Marc Haas` / `Marc de Haas` only scores ~0.39, while distinct names sharing a phonetic code (`Robert`/`Roberta`) reach ~0.60, so lowering it floods proposals with false positives without catching the token-insertion case. Proposals are non-destructive until approved via `memex lint resolve`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `scan_enabled` | `bool` | `false` | `MEMEX_SERVER__MEMORY__ENTITY_MAINTENANCE__SCAN_ENABLED` | Master switch. Off by default. |
| `top_n` | `int` (>= 2) | `100` | `…__TOP_N` | Max entities per scan, by `mention_count`. |
| `scan_cooldown_days` | `int` (>= 0) | `7` | `…__SCAN_COOLDOWN_DAYS` | Per-entity cooldown between scans. |
| `pair_threshold` | `float` (`[0, 1]`) | `0.85` | `…__PAIR_THRESHOLD` | Min pairwise similarity to connect two non-identical entities (identical names always cluster via the fast path). |
| `cluster_min_threshold` | `float` (`[0, 1]`) | `0.70` | `…__CLUSTER_MIN_THRESHOLD` | Cohesion guard — min pairwise similarity across every pair in a cluster. |

---

## DeprioritizeScoreConfig

FSFM-inspired graph-aware deprioritization scorer. Lives at `server.memory.deprioritize_score`.

> Weights, lambdas, and thresholds below are literature-precedent — not fine-tuned. Calibrate before treating them as load-bearing.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__MEMORY__DEPRIORITIZE_SCORE__ENABLED` | Enable lint rules and auto-band step. |
| `interval_seconds` | `int` (>= 60) | `86400` (24h) | `…__INTERVAL_SECONDS` | Scorer interval (when invoked from the periodic lint task). |
| `weights` | [`DeprioritizeScoreWeights`](#deprioritizescoreweights) | see below | `…__WEIGHTS__*` | Per-component weights. |
| `lambda_link` | `float` (>= 0) | `0.01` | `…__LAMBDA_LINK` | Per-link recency decay rate per day. |
| `mu_entity` | `float` (>= 0) | `0.005` | `…__MU_ENTITY` | Entity-dormancy decay rate per day. |
| `thresholds` | [`DeprioritizeScoreThresholds`](#deprioritizescorethresholds) | see below | `…__THRESHOLDS__*` | Threshold band. |
| `cooldown_days` | `int` (>= 0) | `14` | `…__COOLDOWN_DAYS` | Days after `memory_restore` during which the auto-band must not re-deprioritize. |

### DeprioritizeScoreWeights

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `graph` | `float` (`[0, 1]`) | `0.5` | `…__WEIGHTS__GRAPH` | Weight on the graph-pressure component (inbound `MemoryLink` aggregate). |
| `mw` | `float` (`[0, 1]`) | `0.25` | `…__WEIGHTS__MW` | Weight on `1 − Memory Worth posterior`. |
| `temporal` | `float` (`[0, 1]`) | `0.15` | `…__WEIGHTS__TEMPORAL` | Weight on temporal staleness `1 − exp(-age / stability)`. |
| `entity` | `float` (`[0, 1]`) | `0.10` | `…__WEIGHTS__ENTITY` | Weight on entity-dormancy via the freshest linked entity. |

### DeprioritizeScoreThresholds

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `propose` | `float` (`[0, 1]`) | `0.30` | `…__THRESHOLDS__PROPOSE` | Score above which a maintenance proposal is emitted. |
| `auto_deprioritize` | `float` (`[0, 1]`) | `0.55` | `…__THRESHOLDS__AUTO_DEPRIORITIZE` | Score above which the auto-band flips `is_deprioritized=true`. |
| `disagreement_range` | `float` (`[0, 1]`) | `0.45` | `…__THRESHOLDS__DISAGREEMENT_RANGE` | Range across the four normalized components above which the rule emits `components_disagree`. |
| `contradicted_low_credibility_max` | `float` (`[0, 1]`) | `0.3` | `…__THRESHOLDS__CONTRADICTED_LOW_CREDIBILITY_MAX` | Σ source-credibility below which low-credibility-only contradictions escalate. |
| `high_mw_threshold` | `float` (`[0, 1]`) | `0.7` | `…__THRESHOLDS__HIGH_MW_THRESHOLD` | MW posterior above which a unit counts as "previously high MW". |
| `high_mw_min_outcomes` | `int` (>= 1) | `5` | `…__THRESHOLDS__HIGH_MW_MIN_OUTCOMES` | Min `(success + failure)` outcomes for the high-MW escalation pattern. |

---

## DocumentConfig

Document search and synthesis. Lives at `server.document`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `MEMEX_SERVER__DOCUMENT__MODEL__*` | Skeleton-tree reasoning and answer synthesis LLM. |
| `search_strategies` | [`DocSearchStrategiesConfig`](#docsearchstrategiesconfig) | all `true` | `…__SEARCH_STRATEGIES__*` | Strategies for document search. |
| `mmr_lambda` | `float \| None` (`[0, 1]`) | `0.8` | `…__MMR_LAMBDA` | Default MMR λ. `null` disables. Overridable per-request. |

---

## VaultSummaryConfig

Periodic vault summaries. Lives at `server.vault_summary`.

| Key | Type | Default | Env var | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `true` | `MEMEX_SERVER__VAULT_SUMMARY__ENABLED` | Enable periodic generation via the scheduler. |
| `interval_seconds` | `int` (>= 60) | `3600` | `…__INTERVAL_SECONDS` | Interval between update checks (default 1h). |
| `model` | `ModelConfig \| None` | `null` (inherits `default_model`) | `…__MODEL__*` | LLM for vault-summary calls. |
| `batch_size` | `int` (`[10, 200]`) | `50` | `…__BATCH_SIZE` | Hard note-count cap per batch. |
| `max_batch_tokens` | `int` (`[1000, 100000]`) | `8000` | `…__MAX_BATCH_TOKENS` | Token budget per batch (estimated from serialized metadata). |
| `max_patch_log` | `int` (`[1, 100]`) | `20` | `…__MAX_PATCH_LOG` | Maximum entries in the update log. |
| `max_narrative_tokens` | `int` (`[50, 500]`) | `200` | `…__MAX_NARRATIVE_TOKENS` | Maximum tokens for the vault narrative text. |
| `dormant_threshold_days` | `int` (>= 1) | `30` | `…__DORMANT_THRESHOLD_DAYS` | Age in days since the most recent note past which theme trends are forced to `dormant` on read. |

---

## Special environment variables

These variables are not Pydantic fields; the loader reads them directly.

| Variable | Default | Description |
|---|---|---|
| `MEMEX_CONFIG_PATH` | — | Explicit path to a YAML config file. Overrides local file search. |
| `MEMEX_LOAD_GLOBAL_CONFIG` | `true` | Set to `false` to skip `~/.config/memex/config.yaml`. |
| `MEMEX_LOAD_LOCAL_CONFIG` | `true` | Set to `false` to skip CWD-and-parents search. |
| `MEMEX_ENV` | — | When set to `production`, refuses to start with the default Postgres password (`postgres`). |
| `MEMEX_VAULT__ACTIVE` | — | Equivalent to `vault.active`. |
| `MEMEX_VAULT__SEARCH` | — | Equivalent to `vault.search`. Must be a JSON-encoded string: `'["a","b"]'`. |
| `MEMEX_API_KEY` | — | Equivalent to `api_key`. |

The loader searches local YAML files in this order (first match wins): `memex_core.yaml`, `.memex.yaml`, `memex_core.config.yaml`. The search walks CWD and every parent directory.

---

## Constants

Not configuration — referenced by the schema and worth knowing.

| Constant | Value | Description |
|---|---|---|
| `GLOBAL_VAULT_ID` | `ac9b6a45-d388-5ddb-9fa9-50d4e5bca511` | Deterministic UUID for the `global` vault (namespace `memex:global`). |
| `GLOBAL_VAULT_NAME` | `global` | Name of the global vault. Also the default for `default_active_vault` and `default_reader_vault`. |
| `CHARS_PER_TOKEN` | `4` | Approximate conversion ratio between characters and tokens. |
| `LOCAL_CONFIG_NAMES` | `['memex_core.yaml', '.memex.yaml', 'memex_core.config.yaml']` | YAML filenames searched in CWD and parents. |
| `_MAX_VARIANCE` (lint) | `1/12` ≈ `0.0833` | Upper bound on Beta(1,1) cold-start variance; ceiling for `LintConfidenceGate.variance_max`. |

---

## See also

- [Tutorial: getting started](../tutorials/getting-started.md)
- [How-to: configure Memex](../how-to/configuring-server/default-model.md)
- [Reference: configuration (YAML walkthrough and examples)](configuration.md)
- [Explanation: inference model backends](../explanation/how-memex-works/retrieval.md)
