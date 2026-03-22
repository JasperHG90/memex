---
license: mit
base_model: cross-encoder/ms-marco-MiniLM-L12-v2
tags:
- cross-encoder
- reranker
- hindsight
- agent-memory
- quantization
- onnx
language:
- en
pipeline_tag: text-classification
library_name: transformers
---

# Hindsight Memory Reranker

A fine-tuned cross-encoder reranking model optimized for ranking documents in Hindsight-formatted agent memory systems.

## Model Description

This model is a fine-tuned version of [`cross-encoder/ms-marco-MiniLM-L12-v2`](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L12-v2), specifically trained to rerank memory documents formatted according to the [Hindsight](https://arxiv.org/abs/2512.12818) memory architecture.

The model is trained with Quantization-Aware Training (QAT) and exported to ONNX format with INT8 dynamic activation and INT4 weight quantization for efficient inference.

### Key Features

- **Date-Range Aware**: Distinguishes between ongoing facts (`[End: ongoing]`), completed events (`[End: <date>]`), and timeless facts (no date prefix)
- **Hindsight-Optimized**: Trained on documents with temporal anchors, fact types, and contextual metadata
- **Quantized**: INT8/INT4 quantization for efficient CPU deployment
- **ONNX Export**: Ready for production deployment without PyTorch dependency

## Usage

### With ONNX Runtime

```python
import onnxruntime as ort
from tokenizers import Tokenizer

tokenizer = Tokenizer.from_file("tokenizer.json")
tokenizer.enable_truncation(max_length=512)
tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
session = ort.InferenceSession("model.onnx")

def score(query: str, documents: list[str]) -> list[float]:
    pairs = [(query, doc) for doc in documents]
    encodings = tokenizer.encode_batch(pairs)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)
    logits = session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    })[0]
    return logits.flatten().tolist()
```

### Document Formatting

Documents must be formatted with date ranges before ranking:

```
[Start: January 01, 2024 (2024-01-01)] [End: ongoing] [World] Ruby Martinez is the Department Head of the Engineering Department at TechCo Global.
```

Format cases:

| Scenario | Format |
|---|---|
| Ongoing fact (started, still true) | `[Start: Month DD, YYYY (YYYY-MM-DD)] [End: ongoing] [Type] Text` |
| Completed fact (started and ended) | `[Start: Month DD, YYYY (YYYY-MM-DD)] [End: Month DD, YYYY (YYYY-MM-DD)] [Type] Text` |
| Timeless fact (no temporal anchor) | `[Type] Text` |
| End-only (rare) | `[End: Month DD, YYYY (YYYY-MM-DD)] [Type] Text` |

Where:
- **Type**: `World` (enduring fact), `Event` (bounded occurrence), or `Observation` (synthesized insight)
- **Context** (optional): Category prefix before the text (e.g., `Architecture Decision: ...`)

## Training Details

### Training Data

315 hand-crafted triplets covering diverse domains: software engineering, data engineering, DevOps, personal life, health, finance, hobbies, work culture, and consumer products. All names and organizations are fictional.

Each triplet contains:
- `query`: Natural language question
- `positive`: The memory unit that correctly answers the query
- `negative`: A plausible but incorrect memory unit (hard negative — topically related but wrong)

Both positive and negative include `occurred_start` and `occurred_end` fields to train date-range awareness.

| Split | Samples |
|-------|---------|
| Train | 315 |
| Eval | 70 |
| Test | 60 |

Example:

```json
{
  "query": "What framework does the Memex API use?",
  "positive": {
    "text": "The Memex application remains a Python-based system utilizing the FastAPI framework. | When: As of March 20, 2026 | Involving: The development team | Following the abandonment of the Rust migration spike...",
    "occurred_start": "2026-03-20T00:00:00",
    "occurred_end": null,
    "type": "world",
    "context": ""
  },
  "negative": {
    "text": "A Rust migration of the Memex server was initiated as a proof-of-concept spike but was ultimately abandoned and never completed. | When: Before March 20, 2026 | Involving: The development team...",
    "occurred_start": "2026-02-01T00:00:00",
    "occurred_end": "2026-03-20T00:00:00",
    "type": "event",
    "context": ""
  }
}
```

### Training Configuration

| Parameter | Value |
|-----------|-------|
| Base Model | cross-encoder/ms-marco-MiniLM-L12-v2 |
| Epochs | 10 |
| Batch Size | 8 |
| Learning Rate | 6e-6 |
| Loss Function | Margin Ranking Loss (margin=1.0) |
| Max Sequence Length | 512 |
| Quantization | INT8 dynamic activation, INT4 weights (QAT via torchao) |

### Loss Function

Uses Margin Ranking Loss to ensure positive documents receive higher relevance scores than negative documents:

```
loss = max(0, margin - score_positive + score_negative)
```

## Evaluation Results

### v2 — Date range format (2026-03-22)

| Metric | Baseline (Eval) | Trained (Eval) | Baseline (Test) | Trained (Test) |
|--------|-----------------|-------------------|-----------------|-------------------|
| Accuracy@1 | 0.8857 | **0.9571** | 0.8667 | **0.9333** |
| Accuracy@3 | 1.0000 | 1.0000 | 0.9833 | 0.9833 |
| Accuracy@5 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Accuracy@10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| MRR@10 | 0.9429 | **0.9786** | 0.9208 | **0.9597** |

Note: The v2 baseline scores are higher than v1 because the v1 dataset contained low-quality examples (single-word triplets, trivially short texts) that confused even a strong baseline. The v2 data was fully rewritten with natural memory unit text. The relative improvement (baseline → trained) is the meaningful signal.

### v1 — Single date format (previous)

| Metric | Baseline (Eval) | Trained (Eval) | Baseline (Test) | Trained (Test) |
|--------|-----------------|-------------------|-----------------|-------------------|
| Accuracy@1 | 0.6286 | 0.6714 | 0.5167 | 0.7167 |
| Accuracy@3 | 0.8143 | 0.8714 | 0.8000 | 0.8167 |
| Accuracy@5 | 0.8571 | 0.9000 | 0.8500 | 0.8833 |
| MRR@10 | 0.7290 | 0.7728 | 0.6681 | 0.7788 |

## Model Architecture

- **Type**: Cross-Encoder for sequence classification
- **Base**: MiniLM-L12 (12 transformer layers)
- **Output**: Single relevance score (logits)
- **Quantization**: INT8 dynamic activation + INT4 weight (via torchao)

## Changelog

### v2 (2026-03-22)
- **Breaking**: Input format changed from `[Date: ...]` to `[Start: ...] [End: ...]` date ranges
- Added `occurred_end` support for distinguishing ongoing vs completed facts
- Timeless facts (no dates) now have no date prefix instead of a forced date
- Training data fully rewritten: 315 hand-crafted triplets with diverse domains
- Removed low-quality single-word examples from v1 dataset

### v1 (initial)
- Single `[Date: ...]` timestamp format
- 300 training triplets (mixed quality)

## Limitations

- Training data is synthetic; the model may not generalize to all real-world retrieval patterns
- Optimized for Memex memory unit format; may not perform well on arbitrary document formats
- English language only
- Small eval/test corpus (140/120 docs) — absolute accuracy numbers may not reflect production performance at larger scale

## License

MIT License
