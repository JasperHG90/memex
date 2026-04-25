# Memory Budget on Unified-Memory Hosts

This guide is for operators deploying Memex to **memory-constrained hosts that share a single pool between CPU and GPU** — primarily NVIDIA Jetson Orin Nano (8 GiB), Jetson Orin Nano Super (16 GiB), and similar edge devices. It explains the four levers that determine peak memory usage, gives one validated worked example, and shows how to adapt the recipe to other devices.

If you are running Memex on a host with discrete VRAM (a desktop with a separate dGPU) or on a beefy x86 server with plenty of RAM, this page is informational rather than prescriptive — the defaults will usually be fine.

For the underlying Pydantic fields, see [Configuration Reference](../reference/configuration.md). For the broader server configuration story, see [Server Configuration Templates](server-configuration-templates.md).

## The four levers

Peak memory on a unified-memory host is the sum of five contributors. They share one cgroup `memory_max` ceiling — exceeding it causes the kernel to swap or OOM-kill, which on Jetson manifests as the worker wedging.

```
ONNX_GPU_MEM_LIMIT
  + Python heap (your workload)
  + cuDNN workspace (~1 GiB during reranker calls — see #50)
  + reranker batch peak memory (~ RERANKER_BATCH_SIZE * per-pair-bytes)
  + embedding batch peak memory (~ EMBEDDING_BATCH_SIZE * per-text-bytes)
  <= container memory_max
```

You tune Memex with four levers:

| Lever | What it caps | Where it lives |
|---|---|---|
| `ONNX_GPU_MEM_LIMIT` | Bytes the ONNX runtime is allowed to allocate for model weights and intermediate tensors. | Environment variable read by the ONNX runtime at startup. |
| `RERANKER_BATCH_SIZE` | Pairs of (query, document) the cross-encoder scores in one forward pass. Reranker peak memory is roughly linear in this number. | `server.memory.retrieval.reranker_batch_size` in YAML. |
| `EMBEDDING_BATCH_SIZE` | Texts the embedding model encodes in one forward pass. | `server.embedding_batch_size` in YAML. |
| Container `memory_max` | Hard ceiling enforced by the kernel cgroup. | Docker `--memory`, Kubernetes `resources.limits.memory`, systemd `MemoryMax=`. |

Memex also exposes three concurrency caps that pair with the batch-size levers:

| Cap | Limits | Where it lives |
|---|---|---|
| `reranker_max_concurrency` | Concurrent in-flight reranker calls. Sister to `reranker_batch_size`. | `server.reranker_max_concurrency` (default 4). |
| `embedding_max_concurrency` | Concurrent in-flight embedding calls. | `server.embedding_max_concurrency` (default 4). |
| `ner_max_concurrency` | Concurrent in-flight NER calls. | `server.ner_max_concurrency` (default 4). |

The concurrency caps prevent the worker from accumulating threads against a model whose batch is already saturating the GPU memory budget. **Tune them together with the matching batch size**: a host that needs `RERANKER_BATCH_SIZE=8` typically wants `reranker_max_concurrency` of 2-4, not the default 4.

### Why each lever matters

`ONNX_GPU_MEM_LIMIT` is the floor — the ONNX runtime grabs this much up front and never gives it back. Set it too high and there is no headroom for the Python heap, cuDNN workspace, or batch tensors. Set it too low and inference slows down because the runtime has to swap weights between calls.

`RERANKER_BATCH_SIZE` is the spike. The cross-encoder allocates a transient tensor proportional to batch size during scoring; this tensor lives inside the cuDNN workspace. **Exceeding the recommended value caused the cuDNN allocation failure that immediately preceded the wedge in [issue #50](https://github.com/JasperHG90/memex/issues/50)**. Reranker batch and `reranker_max_concurrency` are sister levers — both must be sized so the worst-case combined peak fits under `memory_max`.

`EMBEDDING_BATCH_SIZE` is the secondary spike. Embedding peak per call is lower than reranker peak per call, so embedding can usually run with 2x the reranker batch size on the same host.

Container `memory_max` is the ceiling. Set it to roughly 90% of available physical RAM so the kernel has headroom before swap or OOM-kill.

## Worked example: Jetson Orin Nano (8 GiB unified)

This is the device that wedged in [issue #50](https://github.com/JasperHG90/memex/issues/50). The values below are derived from the constraint reasoning in that issue (4 GiB ONNX arena + Python heap + ~1 GiB cuDNN workspace + reranker batch).

```yaml
# .memex.yaml on a Jetson Orin Nano 8 GiB
server:
  embedding_batch_size: 16
  reranker_max_concurrency: 2
  embedding_max_concurrency: 2
  ner_max_concurrency: 2
  memory:
    retrieval:
      reranker_batch_size: 8
```

Plus the environment variable and container limit:

```bash
ONNX_GPU_MEM_LIMIT=4000000000   # 4 GiB
docker run --memory=7000m memex-server
```

| Lever | Validated value | Why |
|---|---|---|
| `ONNX_GPU_MEM_LIMIT` | `4000000000` (4 GiB) | Half the unified pool. Leaves room for Python heap + cuDNN + batches under 7 GiB. |
| `RERANKER_BATCH_SIZE` | 8 | The wedge's neighbour incident in #50 happened with batch=32 plus concurrent calls; 8 is the largest value validated to coexist with `reranker_max_concurrency=2` on this device. |
| `EMBEDDING_BATCH_SIZE` | 16 | Embedding has lower per-call peak than reranker; 2x reranker batch is safe. |
| `reranker_max_concurrency` | 2 | Caps simultaneous reranker calls so the worst-case combined batch+cuDNN peak stays under `memory_max`. |
| `embedding_max_concurrency` | 2 | Pairs with the reduced embedding batch budget. |
| `ner_max_concurrency` | 2 | NER is the cheapest model but shares the same memory pool. |
| Container `memory_max` | 7000m (7 GiB) | Leaves 1 GiB headroom for the kernel before swap/OOM. |

> **Wedge warning.** Exceeding `RERANKER_BATCH_SIZE=8` on this device is the [issue #50](https://github.com/JasperHG90/memex/issues/50) wedge's neighbour incident — a cuDNN allocation failure inside the reranker scoring call, six minutes before the worker wedged. The reranker batch size and `reranker_max_concurrency` are sister levers; raise one only if you also lower the other.

## Adapting the recipe to other devices

We have **one** empirically validated data point (Jetson Orin Nano 8 GiB, from #50). For other hardware — Jetson Orin Nano Super 16 GiB, x86 hosts with discrete GPUs, or larger Jetsons — the **shape of the constraint is the same**, only the numbers move. **Do not** copy the Jetson tuple verbatim onto a 16 GiB host without verifying.

Recommended starting point for an unvalidated device:

1. **Container `memory_max`**: about 90% of available RAM. (16 GiB host -> ~14 GiB.)
2. **`ONNX_GPU_MEM_LIMIT`**: about 50% of `memory_max`. (14 GiB ceiling -> ~7 GiB.)
3. **Batch sizes**: start with the Jetson values (`RERANKER_BATCH_SIZE=8`, `EMBEDDING_BATCH_SIZE=16`).
4. **Concurrency caps**: start with `reranker_max_concurrency=2`, `embedding_max_concurrency=2`, `ner_max_concurrency=2`.
5. **Monitor** the `memex_sync_offload_inflight{stage="rerank"}` Prometheus gauge and reranker call duration under realistic load.
6. **Lower batch size first** if you see cuDNN allocation failures; only then raise the cap.

For x86 hosts with a discrete GPU and at least 16 GiB system RAM plus 8 GiB VRAM, the defaults (`reranker_max_concurrency=4`, `embedding_max_concurrency=4`, `ner_max_concurrency=4`, `RERANKER_BATCH_SIZE` per the model card) usually work without tuning.

## Querying the in-flight gauges

Memex exposes two Prometheus gauges so you can see what's actually wedging:

- `memex_extraction_inflight{stage}` — labels `scan` / `refine` / `summarize` / `block_summarize`.
- `memex_sync_offload_inflight{stage}` — labels `embed` / `rerank` / `ner`.

Both are labelled per-stage. **Always query with `sum by (stage)`, never `sum by ()`** — the global sum across stages is not a useful production signal because the **`refine` stage double-counts the `scan` calls it makes**. A refine task wraps `_instrument('refine')` around an inner `_process_single_chunk` that itself increments `scan`; a single LLM call that runs as part of refine increments BOTH gauges concurrently. The intent is correct (the refine task IS in flight, AND the scan substep IS in flight) — but a global sum will report `2 × refine_concurrency` at peak rather than `refine_concurrency`.

### Useful queries

```promql
# Per-stage in-flight (correct view of what's running):
sum by (stage) (memex_extraction_inflight)
sum by (stage) (memex_sync_offload_inflight)

# Saturation per cap (compare in-flight to its semaphore capacity):
sum by (stage) (memex_extraction_inflight{stage="refine"})
  / on() group_left() <refine_max_concurrency_value>

# Watchdog companion: stages with inflight > 0 for a long time
# (drives the same trigger semantics as `wedge_watchdog_seconds`):
sum by (stage) (memex_extraction_inflight) > 0
  and on(stage) avg by (stage) (changes(memex_extraction_inflight[5m])) == 0
```

### What NOT to query

```promql
# Misleading — refine and its child scan are double-counted.
sum(memex_extraction_inflight)

# Same problem; mixing the two metric families just makes the chart
# harder to read because they have disjoint stage label sets.
sum(memex_extraction_inflight) + sum(memex_sync_offload_inflight)
```

### Why the double-count is correct semantics

The intent: when a refine task is in progress *and* it's currently doing its scan substep, both `refine` and `scan` are in flight. Operators tuning `refine_max_concurrency` expect the `refine` gauge to reflect the cap; operators tuning `scan_max_concurrency` expect the `scan` gauge to reflect THAT cap. Disjoint scoping (e.g., suppressing the inner scan increment) would make either view incomplete. The price is that you must aggregate per-stage, not globally.

The same nuance applies to PR1 + PR1.5 sites: a `_summarize_single_node` runs entirely under `_instrument('summarize')` (no nested stages), so summarize/block_summarize/embed/rerank/ner do NOT double-count. **Only `refine` shadows `scan`**; this section flags that fact so it stays in operator memory.

## Cross-references

- The concurrency-cap fields (`reranker_max_concurrency`, `embedding_max_concurrency`, `ner_max_concurrency`) are documented in [Configuration Reference](../reference/configuration.md). Their `description=` text in `packages/common/src/memex_common/config.py` links back to this page so operators reading the YAML see the warning at config-time.
- For an explanation of why concurrent in-flight calls amplify peak memory, see [Extraction Pipeline](../explanation/extraction-pipeline.md).
- The batch-size levers (`reranker_batch_size`, `embedding_batch_size`) are documented in [Inference Model Backends](../explanation/inference-model-backends.md).
