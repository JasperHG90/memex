# About Inference Model Backends

Memex uses two inference models for search — an **embedding model** and a **reranking model**. By default, both are fine-tuned ONNX models that run locally. This document explains the backend architecture, how to swap providers, and the design decisions behind it.

## Context

The built-in ONNX models are fine-tuned on Memex's data format and run with zero external dependencies. However, users may want to:

- Use a hosted provider (OpenAI, Google, Cohere) for higher-quality embeddings
- Self-host models via Ollama, vLLM, or HuggingFace TEI
- Disable reranking to reduce latency or cost
- Use a different embedding dimension (requires a DB migration)

The backend system makes this possible without changing any application code.

## Architecture

All internal code programs against two **protocols** (interfaces), not concrete classes:

```
EmbeddingsModel          RerankerModel
  .encode(texts)           .score(query, texts)
       ▲                        ▲
       │                        │
  ┌────┴────┐              ┌────┴────┐
  │         │              │         │
FastEmbedder  LiteLLMEmbedder  FastReranker  LiteLLMReranker
 (ONNX)      (litellm)         (ONNX)       (litellm)
```

- **`EmbeddingsModel`** — any object with an `encode(text: list[str]) -> ndarray` method
- **`RerankerModel`** — any object with a `score(query: str, texts: list[str]) -> ndarray` method

Both protocols are defined in `memex_core.memory.models.protocols` and are `@runtime_checkable`, so `isinstance()` checks work.

## The Two Backends

### ONNX (default)

| Component | Model | Dimension |
|-----------|-------|-----------|
| Embedder | `JasperHG90/minilm-l12-v2-hindsight-embeddings` | 384 |
| Reranker | `JasperHG90/ms-marco-minilm-l12-hindsight-reranker` (v2) | — |

These are downloaded from Hugging Face on first use and cached in `~/.cache/memex/`. They run via ONNX Runtime on CPU (or GPU if `MEMEX_ONNX_PROVIDERS` is set).

Advantages:
- Zero external API calls — works offline
- Sub-millisecond inference on CPU
- Fine-tuned on Memex's embedding format (`Type (Context): Text`)

### LiteLLM

[LiteLLM](https://docs.litellm.ai/) provides a unified API across 100+ providers. When `type: litellm` is configured, Memex delegates to litellm for the actual API call.

**Embedding providers** (via `litellm.embedding`): OpenAI, Google Gemini, Cohere, Ollama, Azure, Bedrock, Together AI, Voyage, HuggingFace Inference API, and more.

**Reranking providers** (via `litellm.rerank`): Cohere, Together AI, Azure AI, Voyage, Infinity, hosted vLLM, and more.

## How It Works

### Factory dispatch

At startup, the server reads the config and calls the factory functions:

```python
embedding_model = await get_embedding_model(config.server.embedding_model)
reranking_model = await get_reranking_model(config.server.memory.retrieval.reranker)
```

Each factory inspects `config.type` and returns the appropriate backend:
- `type: onnx` → downloads ONNX model if needed, returns `FastEmbedder` / `FastReranker`
- `type: litellm` → returns `LiteLLMEmbedder` / `LiteLLMReranker` configured with model string, API base, and key
- `type: disabled` (reranker only) → returns `None`, retrieval skips the reranking step

### Reranker score normalisation

The retrieval engine applies sigmoid normalisation to raw reranker scores (`1 / (1 + exp(-score))`). This works correctly for the ONNX model, which outputs raw logits.

LiteLLM providers return `relevance_score` already in [0, 1]. To keep the retrieval engine unchanged, the `LiteLLMReranker` adapter applies the **inverse sigmoid (logit transform)** before returning scores:

```
logit(s) = log(s / (1 - s))
```

This way, when the retrieval engine applies sigmoid, it recovers the original provider scores. The transform is transparent to the rest of the system.

### Embedding dimension validation

When a litellm embedding backend is configured, the server sends a probe text at startup and checks the output dimension against `EMBEDDING_DIMENSION` (384). If they don't match, the server refuses to start with a clear error message suggesting:

1. Use a model that outputs the correct dimension
2. Set `dimensions` in the config (for models supporting Matryoshka embeddings)
3. Run a database migration to resize the vector columns

This prevents silent data corruption from dimension mismatches.

## Config Placement

The two backends live in different parts of the config because their scope differs:

- **Embedding** → `server.embedding_model` — used across the entire system (extraction, retrieval, reflection, KV store)
- **Reranker** → `server.memory.retrieval.reranker` — used only in the retrieval pipeline, alongside other retrieval tuning knobs like `reranking_recency_alpha`

## Key Files

| File | Purpose |
|------|---------|
| `memex_core/memory/models/protocols.py` | `EmbeddingsModel` and `RerankerModel` protocol definitions |
| `memex_core/memory/models/embedding.py` | `FastEmbedder` (ONNX) + `get_embedding_model()` factory |
| `memex_core/memory/models/reranking.py` | `FastReranker` (ONNX) + `get_reranking_model()` factory |
| `memex_core/memory/models/backends/litellm_embedder.py` | `LiteLLMEmbedder` adapter |
| `memex_core/memory/models/backends/litellm_reranker.py` | `LiteLLMReranker` adapter |
| `memex_common/config.py` | `OnnxBackend`, `LitellmEmbeddingBackend`, `LitellmRerankerBackend`, `DisabledBackend` config types |
