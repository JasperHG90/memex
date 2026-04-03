# FAQ

## How can I control costs?

The majority of LLM costs in Memex come from **mental model reflection** — the background process that synthesizes raw facts into higher-level mental models. Each reflection cycle makes multiple LLM calls per entity (7 phases), so entities that reflect frequently add up.

The single most impactful lever is the **`min_priority`** threshold. This controls the minimum priority score an entity must reach before it is selected for reflection. The default is `0.3`. Raising it means fewer entities qualify for reflection, directly reducing LLM calls.

In your `memex.yaml`:

```yaml
server:
  memory:
    reflection:
      min_priority: 0.6  # default: 0.3 — higher = fewer reflections = lower cost
```

Other cost-reduction options:

| Setting | What it does | Config path |
|---|---|---|
| **`min_priority`** | Raise to skip low-priority entities | `server.memory.reflection.min_priority` |
| **`background_reflection_enabled`** | Set `false` to disable automatic reflection entirely | `server.memory.reflection.background_reflection_enabled` |
| **`background_reflection_interval_seconds`** | Increase to reflect less often (default: 600s) | `server.memory.reflection.background_reflection_interval_seconds` |
| **`background_reflection_batch_size`** | Lower to process fewer entities per cycle (default: 10) | `server.memory.reflection.background_reflection_batch_size` |
| **`max_concurrency`** | Lower to reduce parallel reflection tasks (default: 3) | `server.memory.reflection.max_concurrency` |
| **`enrichment_enabled`** | Set `false` to skip Phase 6 enrichment (saves ~1 LLM call per entity) | `server.memory.reflection.enrichment_enabled` |
| **Use a cheaper model for reflection** | Override the reflection model independently | `server.memory.reflection.model` |

For the lowest cost setup, disable background reflection and trigger it manually when needed via `memex memory reflect`.

## How does Memex use LLM calls?

Memex makes LLM calls in three areas:

1. **Extraction** (on ingest) — chunks text and extracts structured facts, observations, and events. Cost scales with the amount of content you ingest.
2. **Reflection** (background) — synthesizes facts into mental models. This is the most expensive operation since it runs a multi-phase pipeline per entity.
3. **Retrieval** (on search, optional) — query expansion and answer synthesis when using `--answer` or `--reason` flags. Costs are per-query and modest.

Embedding and reranking use local ONNX models by default, so they incur no LLM API costs unless you explicitly configure a LiteLLM-backed provider.

## Can I use models other than Gemini?

Yes. Memex uses [LiteLLM](https://docs.litellm.ai/) under the hood, so any provider it supports works — OpenAI, Anthropic, Ollama, Azure, AWS Bedrock, and more. See [Configure Memex](./docs/how-to/configure-memex.md) for details on setting the model provider.

## Can I run Memex without an LLM key?

Partially. You can store and retrieve notes, use keyword/semantic search (powered by local ONNX models), and manage vaults and entities. However, fact extraction, reflection, AI-powered answers, and query expansion all require an LLM API key.

## How do I back up my data?

Memex stores data in two places:

1. **PostgreSQL** — all metadata, embeddings, entities, and mental models. Use standard `pg_dump` for backups.
2. **FileStore** — the raw Markdown note files. By default these are on local disk (configurable path in `memex.yaml`). Back up the directory, or use S3/GCS for built-in durability.

## Can multiple agents share the same Memex instance?

Yes. The server supports concurrent access, vault-scoped API keys, and policy-based access control (reader/writer/admin). Each agent can be scoped to its own vault or share a common one.
