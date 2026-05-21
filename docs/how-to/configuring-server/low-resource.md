# Run Memex on a low-resource host

You want to run Memex on a small VPS, a laptop you also use for other work, or an edge box like a Raspberry Pi or Jetson. The defaults assume a beefy workstation: high concurrency caps, large batch sizes, a 10,000-entry score cache. On a tight host that adds up to memory pressure, swap, or an OOM-kill mid-ingest. This guide walks through the six knobs that bring resident memory down to something a 2–8 GiB host can hold.

Memex's peak memory comes from a handful of contributors that all share one ceiling: ONNX model weights, the Python heap, transient inference tensors during embedding and reranking, and the on-disk file-store connection pool. Every knob below caps one of those contributors.

If you are deploying to a Jetson or another unified-memory edge device with a shared CPU/GPU memory pool, also read [Memory Budget on Unified-Memory Hosts](../memory-budget.md) — it covers the GPU-specific levers (`ONNX_GPU_MEM_LIMIT`, cuDNN workspace) this page does not.

## Prerequisites

- A working Memex server install (see [Tutorial: Getting started](../../tutorials/getting-started.md)).
- A YAML config file Memex picks up — `.memex.yaml` in the project root, `~/.config/memex/config.yaml`, or a path pointed at by `MEMEX_CONFIG_PATH`. See [Configure Memex](../configure-memex.md) for the precedence rules.
- An idea of the host's memory ceiling. The recipe below targets a host with roughly 2 GiB usable for the Memex process.

## Procedure

Apply the knobs together. Each one alone helps; the set keeps the worst-case in check.

### 1. Cap extraction concurrency

Extraction fans out one LLM call per chunk, plus structural scan/refine/summarize calls for the PageIndex strategy on long documents. Each in-flight call holds a chunk of text and a response buffer in memory.

```yaml
server:
  memory:
    extraction:
      max_concurrency: 2          # default: 5
      text_splitting:
        strategy: page_index
        scan_max_concurrency: 2      # default: 20
        refine_max_concurrency: 2    # default: 20
        summarize_max_concurrency: 2 # default: 20
```

`max_concurrency` governs the per-chunk fact-extraction fan-out. The three `*_max_concurrency` knobs under `text_splitting` cap the three stages of the PageIndex large-document scan independently — they have different memory profiles, so the config exposes them as three knobs rather than one. <code-ref path="packages/common/src/memex_common/config.py" lines="730-752" />

### 2. Cap reflection concurrency

Reflection processes one entity at a time through a four-to-five-call Hindsight loop. The default fans out three entities in parallel.

```yaml
server:
  memory:
    reflection:
      max_concurrency: 2          # default: 3, minimum 2
      background_reflection_batch_size: 4   # default: 10
```

`max_concurrency` is the in-flight cap during a batch. `background_reflection_batch_size` is the number of entities the periodic drain picks up per tick. Lowering both spreads the same total work over more ticks without raising the peak.

If reflection still dominates memory on your host, raise the interval too:

```yaml
server:
  memory:
    reflection:
      background_reflection_interval_seconds: 1800   # default: 600 (10 min)
```

A 30-minute cadence is fine for a personal vault. <code-ref path="packages/common/src/memex_common/config.py" lines="525-558" />

### 3. Shrink the reranker batch size and concurrency

The reranker is the single biggest transient memory spike. The cross-encoder scores `(query, document)` pairs in one forward pass; peak memory is roughly linear in the batch.

```yaml
server:
  reranker_max_concurrency: 2   # default: 16
  embedding_max_concurrency: 2  # default: 16
  ner_max_concurrency: 2        # default: 16
  memory:
    retrieval:
      reranker_batch_size: 8    # default: 0 (means "all at once")
```

The default `reranker_batch_size: 0` tells the ONNX runtime to score every candidate in one call. On a low-resource host, set an explicit batch so the peak is bounded. Eight is a safe starting point.

The three `*_max_concurrency` knobs at the `server` level cap how many in-flight calls each model can have at once. They pair with the batch size: a batch of 8 with concurrency 2 is the worst-case 16 pairs in memory at once. <code-ref path="packages/common/src/memex_common/config.py" lines="2141-2179" />

### 4. Cap the reranker score cache

Memex caches reranker scores keyed by `(model_version, query_hash, unit_id)`. The default cap is 10,000 entries — useful on a busy server, oversized on a quiet laptop.

```yaml
server:
  memory:
    retrieval:
      cross_encoder_cache_size: 1000   # default: 10000
      cross_encoder_cache_ttl_seconds: 3600  # default: 86400
```

Lower the cap to bound resident memory; lower the TTL if your queries don't repeat much (entries that won't be reused waste cache slots). To bypass the cache entirely, set `cross_encoder_cache_enabled: false`. Do not set the size or TTL to zero — `TTLCache` rejects both. <code-ref path="packages/common/src/memex_common/config.py" lines="1022-1043" />

### 5. Cap file-store concurrent connections

The file-store backend pools its connections — to the local disk for the default backend, or to S3 or GCS for the cloud backends. Each open connection holds a buffer.

```yaml
server:
  file_store:
    type: local           # or s3 / gcs
    max_concurrent_connections: 4   # default: 10
```

For a small VPS doing one ingest at a time, four is plenty. For S3 / GCS on a constrained host, four also avoids hitting the per-process TCP socket budget. <code-ref path="packages/common/src/memex_common/config.py" lines="130-136" />

### 6. Optional: disable the reranker on a tiny host

On a host with under 2 GiB of usable memory, the ONNX cross-encoder model itself — about 90 MB on disk, several hundred MB resident with weights and workspace — may not earn its keep. Retrieval falls back gracefully to RRF-fused results without it.

```yaml
server:
  memory:
    retrieval:
      reranker:
        type: disabled
```

Searches still work. The ordering is RRF-fused over the five retrieval strategies; you lose the cross-encoder's final-pass quality boost but not the recall. <code-ref path="packages/common/src/memex_common/config.py" lines="359-362" />

## Verification

After applying the config, restart the server and check three things.

**Resident memory drops.** Compare `ps -o rss= -p <pid>` (or `docker stats` if you containerise) before and after. On a stock 8 GiB workstation switching to the recipe above, resident memory typically drops by 200–500 MB at idle and substantially more under load.

**Ingest still completes.** Run a small ingest end-to-end:

```bash
memex note add path/to/test-note.md
memex memory search "a phrase from the note"
```

You should see the note get extracted, indexed, and returned by search within the normal time window. Extraction throughput is lower with capped concurrency — that is the trade.

**Saturation is healthy under load.** If you have Prometheus wired up, watch `sum by (stage) (memex_extraction_inflight)` and `sum by (stage) (memex_sync_offload_inflight)`. Each per-stage value should sit at or below its cap during a busy ingest, not pegged at the cap for long stretches. If it pegs, your host is the bottleneck — either raise the cap (and the memory ceiling with it) or accept the slower throughput.

## Troubleshooting

### OOM during reflection drain

You see the worker get killed (or the container restart) ten minutes after server start, with no foreground request in flight. The reflection loop fired at its first interval and held more memory than the host had.

Lower `background_reflection_batch_size` (try 4, then 2) and raise `background_reflection_interval_seconds` to 1800 or higher. Confirm `max_concurrency: 2` is set. If the OOM persists, set `background_reflection_enabled: false` and run reflection on demand instead.

### Re-ingest is much slower than before

Throughput dropped from a few hundred chunks per minute to a few dozen. That is the cost of lower concurrency caps — extraction is serial-ish now.

If your ingest happens overnight or in a batch window, leave it. If you need faster turnaround, raise `extraction.max_concurrency` and `scan_max_concurrency` one step at a time until memory tightens, then back off one. Watch `memex_extraction_inflight{stage="refine"}` while you do — it shadows the `scan` gauge (the refine task increments both as it walks its substep), so always aggregate per-stage.

### Reranker disabled but searches still return results

This is the intended behaviour, not a bug. Memex's retrieval is RRF-fused over five strategies; the reranker is a final-pass re-scoring on top. With `reranker.type: disabled` the fusion runs and returns the same set, just in fusion order rather than cross-encoder order. If the result quality is unacceptable, re-enable the reranker and instead tune `reranker_batch_size` and `reranker_max_concurrency` down — that keeps the quality boost at lower peak memory.

## See also

- [Tutorial: Getting started](../../tutorials/getting-started.md)
- [How-to: Memory budget on unified-memory hosts](../memory-budget.md)
- [Reference: configuration](../../reference/configuration.md)
- [Explanation: inference model backends](../../explanation/inference-model-backends.md)
