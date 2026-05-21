# Use a different reranker or embedding model

Memex ships with built-in ONNX models for embedding and reranking, so it works out of the box with no API key. When you want stronger retrieval — OpenAI `text-embedding-3-small` for cleaner vectors, Cohere `rerank-v3.5` for a sharper cross-encoder — point Memex at any LiteLLM-supported provider through the same YAML config. This guide walks you through the swap, the secrets, the restart, and how to confirm the new model is actually doing the work.

## Prerequisites

- A running Memex server you can restart.
- A config file you can edit. By default the server reads `.memex.yaml` from the current directory or the path in `MEMEX_CONFIG_PATH`.
- An API key for the provider you plan to use (OpenAI, Cohere, Voyage, Together AI, Gemini) — or a reachable self-hosted endpoint (Ollama, vLLM, TEI).
- A vault you can run a test search against.

## Procedure

### 1. Decide what you are swapping

Three knobs sit under `server` in your YAML:

| Field | Discriminator values | Source |
|---|---|---|
| `server.embedding_model` | `onnx` (default), `litellm` | <code-ref path="packages/common/src/memex_common/config.py" lines="2129-2133" /> |
| `server.memory.retrieval.reranker` | `onnx` (default), `litellm`, `disabled` | <code-ref path="packages/common/src/memex_common/config.py" lines="1013-1016" /> |
| `server.memory.nli.backend` | `onnx` (default), `litellm`, `disabled` | <code-ref path="packages/common/src/memex_common/config.py" lines="425-430" /> |

Embedding feeds extraction and every semantic search. Reranker only fires post-retrieval to reorder candidates. They are independent — you can run hosted embeddings with the built-in ONNX reranker, or the reverse.

### 2. Configure a LiteLLM embedding backend

The shape lives at <code-ref path="packages/common/src/memex_common/config.py" lines="301-331" />. The `model` string follows the LiteLLM `<provider>/<model-name>` convention:

```yaml
server:
  embedding_model:
    type: litellm
    model: openai/text-embedding-3-small
    dimensions: 384
```

The `dimensions` field is critical. Memex's `Vector` columns are fixed at 384 floats wide <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="33" />. If your chosen model emits a different vector width and does not support Matryoshka-style truncation, the database will reject the inserts. Models that DO support dimension reduction (OpenAI `text-embedding-3-*`, Cohere `embed-v3`) accept `dimensions: 384` and return shorter vectors directly.

For a self-hosted provider, add `api_base`:

```yaml
server:
  embedding_model:
    type: litellm
    model: ollama/nomic-embed-text
    api_base: http://localhost:11434
    dimensions: 384
```

### 3. Configure a LiteLLM reranker

The shape lives at <code-ref path="packages/common/src/memex_common/config.py" lines="334-356" />. Place it under `server.memory.retrieval`:

```yaml
server:
  memory:
    retrieval:
      reranker:
        type: litellm
        model: cohere/rerank-v3.5
```

To skip reranking entirely — useful on a memory-budget host or while debugging retrieval — set the discriminator to `disabled`:

```yaml
server:
  memory:
    retrieval:
      reranker:
        type: disabled
```

When disabled, retrieval still returns results — they are RRF-fused but not cross-encoder reordered <code-ref path="packages/core/src/memex_core/memory/models/reranking.py" lines="63-64" />. This matches the graceful-degradation contract: optional components fail open.

### 4. Supply the API key

Two paths. Inline in YAML — fine for a local dev box:

```yaml
server:
  embedding_model:
    type: litellm
    model: openai/text-embedding-3-small
    api_key: sk-paste-the-key-here
    dimensions: 384
```

Or via the provider's environment variable, which LiteLLM picks up automatically:

```bash
export OPENAI_API_KEY="sk-..."
export COHERE_API_KEY="..."
export VOYAGE_API_KEY="..."
export GEMINI_API_KEY="..."
```

Leave `api_key` off in the YAML and LiteLLM will read the env var at call time <code-ref path="packages/common/src/memex_common/config.py" lines="322-326" />. Prefer env vars in production — secrets in YAML show up in backups, log scrapes, and `git diff`.

### 5. Restart the server

The config is read at startup; live reload is not supported for model backends. Stop the server, start it again, and watch the log for the init line emitted by the adapter:

```
LiteLLM embedder initialised: model=openai/text-embedding-3-small api_base=None dimensions=384
LiteLLM reranker initialised: model=cohere/rerank-v3.5 api_base=None
```

These come from <code-ref path="packages/core/src/memex_core/memory/models/backends/litellm_embedder.py" lines="32-37" /> and <code-ref path="packages/core/src/memex_core/memory/models/backends/litellm_reranker.py" lines="42-46" />.

### 6. About the sigmoid-logit transform (no action needed)

The retrieval engine normalises raw reranker scores to `[0, 1]` with a sigmoid before composing the five boost factors <code-ref path="packages/core/src/memex_core/memory/retrieval/engine.py" lines="1606-1607" />. The built-in ONNX cross-encoder emits raw logits, so the sigmoid lands correctly. LiteLLM rerankers return `relevance_score` already in `[0, 1]` — feeding those through another sigmoid would crush the dynamic range.

The `LiteLLMReranker` adapter handles this for you: it applies the inverse sigmoid (logit) before returning, so the pipeline's downstream sigmoid recovers the provider's original probabilities <code-ref path="packages/core/src/memex_core/memory/models/backends/litellm_reranker.py" lines="78-85" />. There is no knob — you do not need to disable or invert anything. The transform is automatic.

## Verification

With the server back up, run a small search against a vault that has indexed content. From the CLI:

```bash
memex memory search "any query you know returns something" --top-k 10
```

Expect results back within a couple of seconds. Then check the Prometheus endpoint:

```bash
curl -s http://localhost:8000/api/v1/metrics | grep memex_cross_encoder_input_count
```

`memex_cross_encoder_input_count` is a histogram of how many candidates the reranker actually scored on each call <code-ref path="packages/core/src/memex_core/metrics.py" lines="346-352" />. If the reranker is wired up correctly, the bucket counters increment on every search. Zero observations after a search means the reranker did not fire — usually the backend type is `disabled` or the model failed to load.

Cross-check that LiteLLM was used and not the ONNX fallback by grepping the server log for the `LiteLLM embedder initialised` and `LiteLLM reranker initialised` lines from step 5. They print exactly once per process.

## Troubleshooting

**`LiteLLM Provider NOT provided` or `Unknown model` at first request.** The `model` field is missing the provider prefix. LiteLLM needs `openai/text-embedding-3-small`, not `text-embedding-3-small`. Add the prefix and restart.

**`pgvector` insert fails with `expected 384 dimensions, not N`.** The new embedding model emits a different vector width and the database column is fixed at 384 <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="33" />. Either set `dimensions: 384` in the embedding config (works for OpenAI `text-embedding-3-*`, Cohere `embed-v3` and other Matryoshka models) or pick a 384-dim model. A wider native model needs a schema migration and a re-embedding pass — out of scope for a config swap.

**Old vectors and new vectors will not compare cleanly.** Embedding models are not interchangeable mid-vault. Cosine similarity between an `all-MiniLM-L6-v2` vector and an `openai/text-embedding-3-small` vector is meaningless. After switching the embedding model, run a re-embedding job over existing units, or accept that semantic search recall degrades until older units age out. The reranker is a different story — swapping rerankers is safe at any time, because reranking re-scores raw text, not stored vectors.

**`RateLimitError: ... exceeded your current quota`.** The provider is throttling you. LiteLLM surfaces the provider's 429 unchanged. Lower `server.embedding_max_concurrency` and `server.reranker_max_concurrency` <code-ref path="packages/common/src/memex_common/config.py" lines="2141-2169" /> to cap parallel requests, or upgrade your provider plan. The default of 16 each suits capable hosts and HTTP-based providers, but small accounts may need 4 or fewer.

**Reranker scores look inverted or compressed.** Either the LiteLLM adapter is bypassed (e.g. by a custom subclass) and the engine's sigmoid is double-applied, or the provider does not return `relevance_score` in `[0, 1]`. Confirm the init log line names `LiteLLM reranker initialised` and check whether `memex_cross_encoder_input_count` increments — that proves the adapter path.

**Reranker disabled and recall feels worse.** Expected. RRF fusion produces a reasonable ordering, but cross-encoder reranking buys 5–15% recall on most workloads. The point of `disabled` is to fail open under host pressure, not to ship with it on. Switch back to `onnx` or `litellm` as soon as the host allows.

**`KeyError: 'OPENAI_API_KEY'` or similar on first call.** LiteLLM cannot find the env var. Either export it in the shell the server runs from, or pass `api_key` inline in the YAML. Restart the server after either change — env vars are read once at startup of the worker that handles the request.

## See also

- [Tutorial: Getting started](../../tutorials/getting-started.md)
- [How-to: Configure the default LLM](default-model.md)
- [Reference: server configuration](../../reference/configuration-options.md)
- [Explanation: architecture overview](../../explanation/how-memex-works/high-level-architecture.md)
