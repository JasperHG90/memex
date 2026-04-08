# Server Configuration Templates

This guide provides five complete configuration files for common Memex server deployment scenarios, from a minimal local setup to a GPU-accelerated edge device. Each template is self-contained and ready to copy.

For an explanation of how configuration layering works, see [How to Configure Memex](configure-memex.md). For a full reference of every key, see [Configuration Reference](../reference/configuration.md).

## Prerequisites

* Memex installed (`uv tool install "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"`)
* PostgreSQL with pgvector running (see [Getting Started](../tutorials/getting-started.md))

## How to Use These Templates

Each template is a complete configuration file. Copy it to `~/.config/memex/config.yaml` (global) or `.memex.yaml` (per-project) and adjust the values marked with `YOUR_...`.

Vault settings (`vault.active`, `vault.search`) are omitted from these templates because they are client-side concerns. See [Organize with Vaults](organize-with-vaults.md) for vault configuration.

For secrets, prefer environment variables over plaintext in YAML. Auth keys support `env:VAR_NAME` syntax; for other secrets (e.g., database password), use the `MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD` environment variable.

---

## Key Concepts

Before choosing a template, it helps to understand a few core mechanisms that the configuration controls.

### Default Model Propagation

`server.default_model` sets the LLM used across the entire system. When the server starts, this model propagates to every subsystem whose `model` field is `null`:

```
default_model
  ├── extraction.model
  ├── extraction.text_splitting.model  (page_index strategy only)
  ├── reflection.model
  ├── contradiction.model
  ├── document.model
  └── vault_summary.model
```

Set one model globally, then override only where quality or cost trade-offs differ. For example, use a cheap model for extraction but a stronger one for reflection:

```yaml
server:
  default_model:
    model: "gemini/gemini-3-flash-preview"  # used everywhere by default
  memory:
    reflection:
      model:
        model: "openai/gpt-4o"              # override for reflection only
```

### Search Strategies

Memex retrieval uses five strategies that run in parallel and are fused with Reciprocal Rank Fusion (RRF). Each strategy contributes a ranked list of candidates; RRF combines them without needing score normalization.

| Strategy | What it does | Best for | If disabled |
|:---------|:-------------|:---------|:------------|
| **Semantic** | Vector similarity via pgvector HNSW index | Conceptual queries, paraphrases, queries where exact keywords don't match | Lose meaning-based matching; only exact terms and entities work |
| **Keyword** | PostgreSQL full-text search (ts_rank_cd) with stemming | Specific terms, commands, proper nouns, technical identifiers | Lose exact-match precision and enriched metadata search |
| **Graph** | Entity NER extraction, then co-occurrence graph traversal | "What do we know about X?" queries, entity-centric exploration | Lose entity relationship discovery, alias matching (k8s/Kubernetes), co-occurrence |
| **Temporal** | Ranks by creation/update date, newest first | "What happened recently?" or "latest status" queries | Lose recency bias; older high-scoring facts may dominate |
| **Mental Model** | Searches synthesized observations from the reflection engine | High-level patterns, summaries, "What's the team's approach to X?" | Lose synthesized answers; only get atomic facts |

Disabling **semantic** saves an embedding call per query. Disabling **graph** saves an NER call per query. Disabling **mental_model** has no effect until the reflection engine has produced mental models.

All five strategies are enabled by default. In most cases, leave them all on. Disable individual strategies only if you understand the trade-off and have a specific reason (e.g., disabling graph on a fresh instance with no entities yet).

### Circuit Breaker

The circuit breaker protects against cascading failures when your LLM provider is down. It wraps all LLM calls (extraction, contradiction, reflection, document search).

**Three states:**

1. **CLOSED** (normal) -- all LLM calls proceed
2. **OPEN** (blocking) -- all LLM calls are rejected immediately with a fast error, no provider contact
3. **HALF_OPEN** (probing) -- one test request is allowed through

**Transitions:**

- After `failure_threshold` (default: 5) consecutive failures, the circuit opens
- After `reset_timeout_seconds` (default: 60) in the OPEN state, one probe request is allowed
- If the probe succeeds, the circuit closes and normal operation resumes
- If the probe fails, the circuit re-opens and the timer resets

**Why it matters:** Without a circuit breaker, a provider outage causes every request to hang for 30+ seconds before timing out. With it, failures are detected after 5 calls and all subsequent calls return instantly. This prevents runaway API costs, frees up server resources, and lets your API return meaningful errors ("provider temporarily unavailable") instead of timing out.

### Contradiction Detection

When new facts are ingested, the contradiction detection system automatically identifies when they update or contradict existing knowledge.

**Pipeline:**

1. **Triage** -- an LLM call scans new facts for corrective language ("actually," "updated to," "no longer"). Most facts pass through untouched.
2. **Candidate retrieval** -- for flagged facts, the system finds existing facts with high cosine similarity (threshold: `similarity_threshold`, default 0.5).
3. **Classification** -- an LLM classifies each pair as *reinforce*, *weaken*, or *contradict*.

**Confidence adjustment:**

Each fact has a confidence score (0.0 to 1.0). The `alpha` parameter (default: 0.1) controls how much each interaction adjusts it:

| Relationship | Adjustment | Example |
|:-------------|:-----------|:--------|
| Reinforce | Both facts get +alpha | New evidence confirms existing fact |
| Weaken | Superseded fact gets -alpha | New info suggests old fact is less reliable |
| Contradict | Superseded fact gets -2 x alpha | Direct contradiction; confidence drops fast |

With `alpha: 0.1`, a fact needs 5 direct contradictions to drop from 1.0 to 0.0. With `alpha: 0.3`, it only takes 2. Use lower alpha for stable domains, higher for fast-moving information.

When confidence drops below `superseded_threshold` (default: 0.3), the fact is hidden from search results but never deleted. The full history is always preserved for audit.

---

## Tuning Reference

The tables below summarize which parameters are worth tuning and which are best left at their defaults. Each template later in this guide applies these recommendations for its deployment scenario.

### Retrieval

| Parameter | Default | Tune? | Guidance |
|:----------|:--------|:------|:---------|
| `token_budget` | 1000 | **Yes** | Max tokens in retrieval results. 1000 is lean; 2000--3000 is appropriate when using capable models that benefit from more context. |
| `candidate_pool_size` | 60 | **Yes** | Candidates per strategy before RRF fusion. 60 is solid. Note: reranker input is capped at `min(effective_limit * 2, 75)`, so pools above 75 help pre-RRF diversity but don't increase the reranking pool. |
| `mmr_lambda` | 0.9 | **Yes** | Diversity vs relevance. 1.0 = pure relevance (may return near-duplicates). 0.7--0.8 = moderate diversity. Set to `null` to disable MMR entirely. |
| `temporal_decay_days` | 30.0 | Rarely | Half-life for recency scoring. At 30 days, a month-old fact scores 50% of a fresh one. Lower values create stronger recency bias. |
| `reranking_recency_alpha` | 0.2 | Rarely | Multiplicative recency boost during reranking. 0 = disabled, 0.2 = subtle, 0.5 = strong. |
| `rrf_k` | 60 | **No** | RRF smoothing constant. 60 is well-studied in the literature. Changing this rarely improves results. |
| `similarity_threshold` | 0.3 | **No** | pg_trgm threshold for entity name fuzzy matching in graph strategies. Leave alone. |
| `temporal_decay_base` | 2.0 | **No** | Exponential base for temporal decay. Leave alone. |
| `graph_semantic_seeding` | true | **No** | Adds semantic seed entities to graph search for better recall. Leave enabled. |

### Reflection

| Parameter | Default | Tune? | Guidance |
|:----------|:--------|:------|:---------|
| `min_priority` | 0.3 | **Yes** | The primary cost lever for reflection. The priority score combines urgency (new evidence), importance (mention frequency), and resonance (retrieval frequency). At 0.3, nearly all entities are reflected on. At 0.5, about 80% qualify. At 0.8, only the top 30--40% qualify. |
| `background_reflection_interval_seconds` | 600 | **Yes** | How often the reflection loop runs. 300s for cloud setups with cheap LLM throughput. 600s for production. 1800--3600s for edge devices. |
| `background_reflection_batch_size` | 10 | **Yes** | Entities per reflection cycle. 20--50 for cloud. 10 for production. 3--5 for edge devices. |
| `max_concurrency` | 3 | **Yes** | Parallel LLM calls during reflection. 5--8 for cloud APIs. 3 for production. 1 for local or edge. |
| `weight_urgency / importance / resonance` | 0.5 / 0.2 / 0.3 | Rarely | These express what Memex should prioritize, not performance. Urgency = new unreflected evidence. Importance = how often an entity appears across notes. Resonance = how often users retrieve it. Must sum to 1.0. Only tune if you have a clear reason to shift priorities. |
| `enrichment_enabled` | true | Rarely | Phase 6 tags memory units with concepts from the mental model, making old memories discoverable for new queries. Costs about 1 LLM call per batch. Disable only on extreme resource constraints. |
| `search_limit` | 10 | Rarely | Evidence candidates per reflection. 10 is well-tuned. Increase to 15--20 if using a cloud reranker. |
| `similarity_threshold` | 0.6 | Rarely | Evidence retrieval cutoff. If observations seem hallucinated, increase to 0.7. If they seem narrow, decrease to 0.5. |
| `stale_processing_timeout_seconds` | 1800 | **No** | Resets stuck PROCESSING items after 30 minutes. Leave alone. |
| `tail_sampling_rate` | 0.05 | **No** | Internal trace sampling rate. Leave alone. |

### Extraction

| Parameter | Default | Tune? | Guidance |
|:----------|:--------|:------|:---------|
| `max_concurrency` | 5 | **Yes** | Maximum parallel LLM calls during ingestion. 8--10 for cloud APIs with high rate limits. 2--3 for Ollama or edge devices. |
| `text_splitting.strategy` | page_index | **No** | Always use page_index. Short documents are automatically skipped via `short_doc_threshold_tokens`. |
| `block_token_target` | 2000 | Rarely | Target tokens per extracted block. Smaller values produce more granular facts (better recall, higher cost). 1500--2000 is the sweet spot. |
| `short_doc_threshold_tokens` | 500 | Rarely | Documents below this token count bypass the page_index scan. Increase to 1000 if most of your documents are short. |
| `scan_chunk_size_tokens` | 20000 | **No** | LLM scan batch size for the page_index strategy. Default works across all providers. |
| `max_node_length_tokens` | 1250 | **No** | Internal refinement threshold. Leave alone. |

---

## Templates

### 1. Minimal Local Development

**When to use this:** Getting started fast. Single developer, localhost only, all built-in defaults. You only need a Gemini API key and a running PostgreSQL instance.

**Configuration:**

```yaml
server:
  default_model:
    model: "gemini/gemini-3-flash-preview"
    api_key: "YOUR_GEMINI_KEY"              # or set GEMINI_API_KEY env var

  meta_store:
    type: postgres
    instance:
      host: localhost
      port: 5432
      database: memex
      user: postgres
      password: postgres
```

Everything else uses defaults: built-in ONNX embedding model, built-in ONNX reranker, local file store at `~/.local/share/memex`, all five search strategies enabled, no auth, no rate limiting, reflection disabled by default in single-worker mode.

**What to change:**

* Replace `YOUR_GEMINI_KEY` with your actual key, or set the `GEMINI_API_KEY` environment variable and remove the `api_key` line.
* Change the `password` to your PostgreSQL password.
* To use a different LLM provider, swap the model string (e.g., `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`).

---

### 2. Fully Local with Ollama

**When to use this:** Air-gapped environments, privacy-first setups, or offline development. No data leaves the machine. All inference runs locally via Ollama.

**Configuration:**

```yaml
server:
  default_model:
    model: "ollama_chat/llama3"
    base_url: "http://localhost:11434"

  embedding_model:
    type: litellm
    model: "ollama/nomic-embed-text"
    api_base: "http://localhost:11434"
    dimensions: 384                          # must match DB vector column width

  meta_store:
    type: postgres
    instance:
      host: localhost
      port: 5432
      database: memex
      user: postgres
      password: postgres

  memory:
    extraction:
      max_concurrency: 2                     # Ollama handles fewer parallel calls

    retrieval:
      reranker:
        type: disabled                       # Ollama has no reranking endpoint

    reflection:
      background_reflection_enabled: false   # enable once you verify Ollama throughput
      max_concurrency: 1
```

**What to change:**

* Pull the required models first: `ollama pull llama3` and `ollama pull nomic-embed-text`.
* If Ollama runs on a different host, update `base_url` and `api_base`.
* `dimensions: 384` is mandatory -- the database schema expects 384-dimensional embeddings. The server validates this at startup.
* You can use the built-in ONNX embedding model instead of Ollama's by removing the `embedding_model` section entirely. Trade-off: the ONNX model is fine-tuned for Memex but `nomic-embed-text` may produce better results for some domains.
* To enable reflection, set `background_reflection_enabled: true` with a long interval (e.g., `background_reflection_interval_seconds: 3600`) and `min_priority: 0.5` to limit LLM usage.

---

### 3. Cloud-Optimized (Best Quality)

**When to use this:** Best possible retrieval quality. Uses cloud LLM, cloud embeddings, and a cloud reranker. Suitable when cost is secondary to quality. Recommended for knowledge bases that will be queried frequently.

**Configuration:**

```yaml
server:
  default_model:
    model: "openai/gpt-4o"
    api_key: "YOUR_OPENAI_KEY"

  embedding_model:
    type: litellm
    model: "openai/text-embedding-3-small"
    api_key: "YOUR_OPENAI_KEY"
    dimensions: 384                          # Matryoshka: request 384-dim to match DB schema

  meta_store:
    type: postgres
    instance:
      host: localhost
      port: 5432
      database: memex
      user: postgres
      password: postgres

  memory:
    extraction:
      max_concurrency: 8                     # cloud APIs handle high parallelism

    retrieval:
      candidate_pool_size: 100               # larger pool before reranking = better recall
      token_budget: 3000                     # more context for capable models
      mmr_lambda: 0.8                        # moderate diversity filtering
      reranker:
        type: litellm
        model: "cohere/rerank-v3.5"
        api_key: "YOUR_COHERE_KEY"

    reflection:
      background_reflection_enabled: true
      background_reflection_interval_seconds: 300   # 5-minute cycles
      background_reflection_batch_size: 20
      max_concurrency: 5
      min_priority: 0.3                      # reflect on nearly all entities
      enrichment_enabled: true               # tag memories with reflected concepts
```

**What to change:**

* **LLM alternatives:** Swap OpenAI for Gemini (`gemini/gemini-3-flash-preview`) or Anthropic (`anthropic/claude-sonnet-4-20250514`). Any [LiteLLM-supported provider](https://docs.litellm.ai/docs/providers) works.
* **Embedding alternatives:** `gemini/text-embedding-004` (no `dimensions` needed -- native output is 768, so you'd need a DB migration). `cohere/embed-english-v3.0` with `dimensions: 384`.
* **Reranker alternatives:** `voyage/rerank-2` or `together_ai/Salesforce/Llama-Rank-V1`.
* `candidate_pool_size: 100` provides a larger pool for RRF fusion. The reranker input is capped at 75, so candidates above that threshold help diversity but don't increase reranking cost.
* Reduce `token_budget` to 1000--2000 if you want to limit LLM input costs downstream.

---

### 4. Production Deployment

**When to use this:** Internet-facing server or team-shared instance. Requires authentication, rate limiting, structured logging, and hardened defaults. This template uses S3-compatible storage (MinIO) and manages secrets via environment variables.

**Configuration:**

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  workers: 4
  allow_insecure: false                      # requires auth when binding to 0.0.0.0

  default_model:
    model: "gemini/gemini-3-flash-preview"

  embedding_model:
    type: onnx                               # no external dependency for embeddings

  auth:
    enabled: true
    keys:
      - key: "env:MEMEX_ADMIN_KEY"           # env: prefix reads from environment variable
        policy: admin
        description: "Admin -- full access"
      - key: "env:MEMEX_READER_KEY"
        policy: reader
        vault_ids: ["shared"]
        description: "CI pipeline -- read-only, scoped to 'shared' vault"
    exempt_paths:
      - "/api/v1/health"
      - "/api/v1/ready"
      - "/api/v1/metrics"

  rate_limit:
    enabled: true
    ingestion: "10/minute"
    search: "60/minute"
    batch: "5/minute"
    default: "120/minute"

  file_store:
    type: s3
    bucket: "my-memex-bucket"
    root: "memex-data"
    region: "us-east-1"
    # access_key_id and secret_access_key via env vars or IAM role

  meta_store:
    type: postgres
    pool_size: 20                            # tuned for multi-worker
    max_overflow: 40
    statement_timeout_ms: 60000              # 60s for long queries
    instance:
      host: "db.internal"
      port: 5432
      database: memex
      user: memex_app
      password: "change-me"                  # override via MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD

  logging:
    level: "INFO"
    json_output: true                        # structured logging for aggregators

  tracing:
    enabled: true
    endpoint: "http://phoenix.internal:6006/v1/traces"

  memory:
    extraction:
      max_concurrency: 5

    retrieval:
      token_budget: 2000

    reflection:
      background_reflection_enabled: true
      background_reflection_interval_seconds: 600
      background_reflection_batch_size: 10
      min_priority: 0.3

    circuit_breaker:
      failure_threshold: 10                  # tolerate occasional blips
      reset_timeout_seconds: 120             # give provider time to stabilize

    contradiction:
      enabled: true
```

**What to change:**

* Generate API keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and set `MEMEX_ADMIN_KEY` and `MEMEX_READER_KEY` environment variables.
* Set `MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD` as an environment variable instead of the plaintext `password` in YAML. The `env:` prefix syntax works only for `auth.keys[].key`, not for database credentials.
* Set `GOOGLE_API_KEY` (or your provider's env var) for the default model.
* For S3 credentials, set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables, or use IAM roles. For MinIO, also set `endpoint_url` under `file_store`.
* For GCS instead of S3, change `type: gcs` with `bucket` and `project` fields.
* Adjust `workers` based on available CPU cores. Each worker loads its own copy of any ONNX models.

---

### 5. GPU-Accelerated Edge Device (Jetson)

**When to use this:** Running on an NVIDIA Jetson (Orin Nano, Xavier, etc.) or similar GPU-equipped edge device with limited unified memory. This template is based on a real Jetson Orin Nano deployment with 8 GB of unified memory running three ONNX models (embedding, reranking, NER) on the GPU.

This template is **environment-variable only** because GPU and ONNX settings are not part of the YAML configuration -- they are read directly from the environment.

**Configuration:**

```bash
# ── GPU / ONNX ───────────────────────────────────────────────
NVIDIA_VISIBLE_DEVICES=all
NVIDIA_DRIVER_CAPABILITIES=compute,utility
LD_LIBRARY_PATH=/usr/local/cuda/lib64

# ONNX execution providers: try CUDA first, fall back to CPU
MEMEX_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider

# GPU memory limit in bytes (4 GB). Three ONNX models share this.
MEMEX_ONNX_GPU_MEM_LIMIT=4294967296

# Batch embedding and reranking to cap peak GPU memory.
# Smaller batches = less peak memory, more inference calls.
# The model retries with a halved batch size on GPU OOM.
MEMEX_SERVER__EMBEDDING_BATCH_SIZE=64
MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKER_BATCH_SIZE=8

# ── Server ───────────────────────────────────────────────────
# Single worker: each worker loads its own copy of the 3 ONNX models.
# On 8 GB unified memory, 2+ workers will OOM.
MEMEX_WORKERS=1

MEMEX_SERVER__CACHE_DIR=/cache/memex

# ── Storage ──────────────────────────────────────────────────
MEMEX_SERVER__FILE_STORE__TYPE=s3
MEMEX_SERVER__FILE_STORE__BUCKET=memex
MEMEX_SERVER__FILE_STORE__ROOT=
MEMEX_SERVER__FILE_STORE__ENDPOINT_URL=http://minio.internal:9000
MEMEX_SERVER__FILE_STORE__REGION=us-east-1
MEMEX_SERVER__FILE_STORE__ACCESS_KEY_ID=YOUR_MINIO_ACCESS_KEY
MEMEX_SERVER__FILE_STORE__SECRET_ACCESS_KEY=YOUR_MINIO_SECRET_KEY

# ── Database ─────────────────────────────────────────────────
MEMEX_SERVER__META_STORE__TYPE=postgres
MEMEX_SERVER__META_STORE__INSTANCE__HOST=postgres.internal
MEMEX_SERVER__META_STORE__INSTANCE__PORT=5432
MEMEX_SERVER__META_STORE__INSTANCE__DATABASE=memex
MEMEX_SERVER__META_STORE__INSTANCE__USER=memex
MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD=YOUR_DB_PASSWORD
MEMEX_SERVER__META_STORE__POOL_SIZE=20
MEMEX_SERVER__META_STORE__MAX_OVERFLOW=30

# ── Auth ─────────────────────────────────────────────────────
MEMEX_SERVER__AUTH__ENABLED=true
MEMEX_SERVER__AUTH__KEYS='[{"key":"YOUR_ADMIN_KEY","policy":"admin","description":"Admin key"},{"key":"YOUR_WRITER_KEY","policy":"writer","vault_ids":["global"],"description":"Scoped writer"}]'

# ── Tracing ──────────────────────────────────────────────────
MEMEX_SERVER__TRACING__ENABLED=true
MEMEX_SERVER__TRACING__ENDPOINT=http://phoenix.internal:6006/v1/traces

# ── LLM ──────────────────────────────────────────────────────
# Gemini for all LLM calls (extraction, reflection, contradiction).
# Set the provider API key directly; LiteLLM picks it up.
GOOGLE_API_KEY=YOUR_GEMINI_KEY

# ── Reflection tuning ────────────────────────────────────────
# Reflect only on high-priority entities (top ~30-40%) to conserve LLM calls.
MEMEX_SERVER__MEMORY__REFLECTION__MIN_PRIORITY=0.8
```

**What to change:**

* **GPU memory limit:** `MEMEX_ONNX_GPU_MEM_LIMIT` is in bytes. For 4 GB: `4294967296`. For 2 GB: `2147483648`. For 8 GB: `8589934592`. Set this to roughly 50--70% of your total GPU memory to leave room for the CUDA runtime.
* **Batch sizes:** `EMBEDDING_BATCH_SIZE=64` and `RERANKER_BATCH_SIZE=8` are tuned for 8 GB unified memory. On devices with less memory, reduce to 32/4. On devices with more, increase to 128/16. The models have automatic OOM recovery: if a batch fails, the batch size is halved and retried.
* **Workers:** `MEMEX_WORKERS=1` is critical. Each worker loads its own copy of the three ONNX models (embedding, reranker, NER). On an 8 GB Jetson, a second worker will exhaust GPU memory. Only increase if your device has 16+ GB.
* **Reflection:** `MIN_PRIORITY=0.8` means only the top ~30--40% of entities by priority score are reflected on. This conserves LLM calls on the edge. Decrease to 0.5 (top ~80%) if your LLM provider can handle the load.
* **Secrets management:** In production, replace `YOUR_...` values with your secrets manager (e.g., HashiCorp Vault template syntax: `{{ with secret "path" }}{{ .Data.data.key }}{{ end }}`).

---

## Verification

After copying a template, verify the configuration loads correctly:

```bash
# Check resolved configuration
memex config show

# Start the server
memex server start

# Test health endpoint
curl http://localhost:8000/api/v1/health
```

## See Also

* [Configure Memex](configure-memex.md) -- configuration layering, environment variables, vault resolution
* [Configuration Reference](../reference/configuration.md) -- full list of all settings, types, and defaults
* [Organize with Vaults](organize-with-vaults.md) -- vault creation and client-side vault settings
* [Inference Model Backends](../explanation/inference-model-backends.md) -- how ONNX and LiteLLM embedding/reranking models work
