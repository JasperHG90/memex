# Memory budget on unified-memory hosts

You want to run Memex on a unified-memory edge device — a Jetson Orin Nano, an Apple-silicon box, or any host where the CPU and the GPU draw from one shared pool. On these hosts there is no separate VRAM to spend: every byte the ONNX runtime allocates for inference comes out of the same `memory_max` ceiling the Python heap, the score cache, and the OS page cache are already drawing from. Over-provision one lever and you do not get a clean out-of-VRAM error — you get an OOM-kill, or worse, the cuDNN allocation failure that neighboured the wedge in issue #50.

This guide gives you one validated recipe for a Jetson Orin Nano 8 GiB and a method for adapting it to a different host. It is deliberately narrow: it does not invent recipes for hardware nobody has measured.

For the host-agnostic knobs (extraction concurrency, reflection cadence, the reranker score cache, file-store connections), read [Run Memex on a low-resource host](./configuring-server/low-resource.md) first. This page covers only the levers that behave differently when the GPU and CPU share one budget.

## Prerequisites

- A working Memex server install (see [Tutorial: Getting started](../tutorials/getting-started.md)).
- A unified-memory host with GPU inference enabled via `MEMEX_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider`.
- A YAML config Memex picks up — `.memex.yaml` in the project root, `~/.config/memex/config.yaml`, or `MEMEX_CONFIG_PATH`.
- A known memory ceiling for the Memex process — the cgroup `memory_max` (or the container `--memory` flag) you intend to run under.

## The four levers

Four levers together bound peak resident memory on a unified-memory host. They are coupled: the GPU memory cap (`ONNX_GPU_MEM_LIMIT`) is a hard ceiling, and the two batch-size levers plus the concurrency caps determine how close inference gets to that ceiling at peak.

| Lever | Where to set it | What it bounds |
|---|---|---|
| `ONNX_GPU_MEM_LIMIT` | env: `MEMEX_ONNX_GPU_MEM_LIMIT` (bytes) | Hard cap on the GPU arena the ONNX CUDA provider may allocate. |
| `RERANKER_BATCH_SIZE` | YAML: `server.memory.retrieval.reranker_batch_size` | Documents scored per reranker forward pass (`0` = all at once). |
| `EMBEDDING_BATCH_SIZE` | YAML: `server.memory.retrieval.embedding_batch_size` | Texts encoded per embedding forward pass (`0` = all at once). |
| `*_max_concurrency` | YAML: `server.reranker_max_concurrency` / `embedding_max_concurrency` / `ner_max_concurrency` | In-flight inference calls per model — multiplies the per-call batch footprint. |

The shared constraint that ties them together: **a model's peak inference footprint is roughly `batch_size × max_concurrency`**, and that footprint must fit inside `ONNX_GPU_MEM_LIMIT`, which in turn must leave headroom under the host `memory_max`. Tune the batch and the concurrency together — never one alone.

### Recipe template

```yaml
# server.yaml — unified-memory recipe template
server:
  # Concurrency caps (default 16 each, sized for capable hosts).
  reranker_max_concurrency: <N>      # in-flight reranker calls
  embedding_max_concurrency: <N>     # in-flight embedding calls
  ner_max_concurrency: <N>           # in-flight NER calls
  memory:
    retrieval:
      reranker_batch_size: <B>       # RERANKER_BATCH_SIZE; 0 = no batching
      embedding_batch_size: <B>      # EMBEDDING_BATCH_SIZE; 0 = no batching
```

```bash
# Environment — GPU arena hard cap + provider selection
export MEMEX_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider
export MEMEX_ONNX_GPU_MEM_LIMIT=<bytes>   # ONNX_GPU_MEM_LIMIT, in bytes
```

Pick `<B>` and `<N>` so that `batch_size × max_concurrency` worth of pairs/texts fits inside `ONNX_GPU_MEM_LIMIT`, and pick `ONNX_GPU_MEM_LIMIT` so the arena plus the Python heap stays under the host `memory_max`.

## Worked example: Jetson Orin Nano 8 GiB

The Jetson Orin Nano 8 GiB is a unified-memory device: the 8 GiB is shared between the CPU and the integrated GPU. Reserve roughly half for the OS, the JetPack stack, and the Python heap, and cap the ONNX GPU arena at **4 GiB** (`4_000_000_000` bytes). This is the validated configuration:

```yaml
# server.yaml — Jetson Orin Nano 8 GiB
server:
  reranker_max_concurrency: 2
  embedding_max_concurrency: 2
  ner_max_concurrency: 2
  memory:
    retrieval:
      reranker_batch_size: 8     # RERANKER_BATCH_SIZE = 8
      embedding_batch_size: 8    # EMBEDDING_BATCH_SIZE = 8
```

```bash
export MEMEX_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider
export MEMEX_ONNX_GPU_MEM_LIMIT=4000000000   # 4 GiB GPU arena cap (4_000_000_000 bytes)
```

With `RERANKER_BATCH_SIZE=8` and `reranker_max_concurrency=2`, the worst case is 16 `(query, document)` pairs resident in the reranker at once — comfortably inside a 4 GiB arena on the Orin Nano, with headroom for the embedding and NER models that share the same `ONNX_GPU_MEM_LIMIT`.

## Why each lever matters

Each lever caps a distinct contributor to the unified-memory peak.

- **`ONNX_GPU_MEM_LIMIT`** is the hard ceiling. Without it, the ONNX CUDA provider grows its arena lazily and never gives memory back — on a unified-memory host that arena eats into the same pool the OS needs, so a single large batch can OOM-kill the box. Setting it bounds the arena up front and lets ONNX fall back to CPU rather than crash if it would exceed the cap.
- **`RERANKER_BATCH_SIZE`** caps the single biggest transient spike. The cross-encoder scores `(query, document)` pairs in one forward pass; peak memory is roughly linear in the batch. The default `0` ("all at once") is fine on a workstation and dangerous on an edge box.
- **`EMBEDDING_BATCH_SIZE`** does the same for the embedding model — peak is linear in the number of texts encoded per call. Default `0` batches everything.
- **`*_max_concurrency`** caps how many inference calls run in parallel. Each in-flight call holds a full batch, so concurrency multiplies the batch footprint: a batch of 8 at concurrency 2 is 16 items resident at peak.

## Warning: the reranker batch / concurrency wedge (issue #50)

`reranker_max_concurrency` and `reranker_batch_size` are **sister levers** and must be tuned together. Issue #50 documented a cuDNN allocation failure that appeared when the reranker batch and concurrency were raised independently: a large `reranker_batch_size` combined with high `reranker_max_concurrency` drove the cuDNN workspace allocation past the GPU arena, and cuDNN failed the allocation mid-forward-pass rather than degrading gracefully.

Always change `reranker_max_concurrency` and `reranker_batch_size` in the same edit, and keep `batch_size × max_concurrency` well inside `ONNX_GPU_MEM_LIMIT`. If you see a cuDNN allocation error under reranker load, lower both — not just one.

## Adapting the recipe to another host

There is exactly one validated recipe on this page — the Jetson Orin Nano 8 GiB. Rather than fabricate untested 16 GiB or 32 GiB tuples, adapt the Jetson Orin Nano numbers to your hardware with this method:

1. **Set `ONNX_GPU_MEM_LIMIT` to roughly half the unified-memory ceiling.** On the Jetson Orin Nano 8 GiB that is 4 GiB (`4_000_000_000`). Scale proportionally: a 16 GiB unified host can usually afford an 8 GiB arena, leaving the rest for the OS and the Python heap.
2. **Start from the Jetson batch/concurrency pair** (`reranker_batch_size: 8`, `*_max_concurrency: 2`). Raise the batch first — it improves throughput per call — and watch resident memory.
3. **Raise `*_max_concurrency` only after the batch is settled,** one step at a time, keeping `batch_size × max_concurrency` inside the GPU arena. Stop one step before memory tightens.
4. **Re-check the wedge.** After every change to either reranker lever, run a reranker-heavy search and confirm no cuDNN allocation error (issue #50).

## Verification

Restart the server and confirm three things.

**The GPU arena is capped.** Check the startup log: the ONNX CUDA provider reports the `gpu_mem_limit` it was given. It must match your `MEMEX_ONNX_GPU_MEM_LIMIT`.

**Resident memory stays under the ceiling.** Watch `docker stats` (or the cgroup `memory.current`) during a busy ingest and a reranker-heavy search. It must stay under your `memory_max`, not just at idle but at peak.

**Saturation is healthy.** With Prometheus wired up, watch `sum by (stage) (memex_sync_offload_inflight)`. Each per-stage value should sit at or below its `*_max_concurrency` cap, not pegged at the cap for long stretches. A pegged gauge means the host is the bottleneck — accept the slower throughput or raise the budget.

## See also

- [Run Memex on a low-resource host](./configuring-server/low-resource.md) — the host-agnostic memory knobs.
- [Reference: configuration](../reference/configuration-options.md) — `reranker_max_concurrency`, `embedding_max_concurrency`, `ner_max_concurrency` and the batch-size fields.
- [Explanation: inference model backends](../explanation/how-memex-works/retrieval.md)
