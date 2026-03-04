---
license: apache-2.0
base_model: sentence-transformers/all-MiniLM-L12-v2
tags:
- sentence-transformers
- embeddings
- feature-extraction
- quantization
- onnx
- agent-memory
language:
- en
pipeline_tag: feature-extraction
library_name: sentence-transformers
---

# MiniLM-L12-v2 Memex Fine-tuned Embeddings

A fine-tuned sentence embedding model optimized for semantic similarity matching of structured memory documents containing facts, events, and observations that are formatted according to the [Hindsight](https://arxiv.org/abs/2512.12818) memory architecture

## Model Description

This model is a fine-tuned version of [`sentence-transformers/all-MiniLM-L12-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L12-v2), specifically trained to generate embeddings for documents formatted with epistemic type labels and contextual metadata.

The model is trained with Quantization-Aware Training (QAT) and exported to ONNX format with INT8 dynamic activation and INT4 weight quantization for efficient inference.

### Key Features

- **Epistemic-Aware**: Optimized for documents with type labels (World, Event, Observation)
- **Context-Sensitive**: Leverages contextual metadata for improved semantic matching
- **Quantized**: INT8/INT4 quantization for efficient deployment
- **ONNX Export**: Ready for production deployment

## Usage

### With ONNX Runtime

```python
import onnxruntime as ort
import numpy as np
from tokenizers import Tokenizer

tokenizer = Tokenizer.from_file("tokenizer.json")
tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
tokenizer.enable_truncation(max_length=512)

session = ort.InferenceSession("model.onnx", providers=['CPUExecutionProvider'])

def encode(texts: list[str]) -> np.ndarray:
    encodings = tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)

    outputs = session.run(None, {
        'input_ids': input_ids,
        'attention_mask': attention_mask,
    })
    return outputs[0]

embeddings = encode(["Your documents here"])
```

### With Sentence Transformers

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("your-username/minilm-l12-v2-memex-ft")
embeddings = model.encode(["Your documents here"])
```

### Document Formatting

Documents should be formatted with type and context labels before embedding:

```
{Type} ({Context}): {Text}
```

Examples:
- `World (Config): Production is pinned to Node 18.x LTS due to a dependency constraint.`
- `Event (Decision): We decided to implement a circuit breaker pattern to prevent cascading failures.`
- `Observation (Contact Info): Mike Smith is the account manager; his email is mike@vendor.com.`

**Supported Types:**
- `World` - Facts about the world
- `Event` - Personal events and actions
- `Observation` - Derived or inferred information

## Training Details

### Training Data

Synthetic triplet data for fine-tuning on structured memory documents.

| Split | Samples |
|-------|---------|
| Train | 300 |
| Eval | 50 |
| Test | 50 |

Each training example contains:
- `query`: Natural language question
- `positive`: Relevant structured document
- `negative`: Distractor document (similar but irrelevant)

Example:
```json
{
  "query": "What is our stance on remote work?",
  "positive": {
    "text": "I believe remote work requires asynchronous communication discipline to be effective.",
    "type": "World",
    "context": "Work Philosophy"
  },
  "negative": {
    "text": "The company policy allows for 3 days of remote work per week.",
    "type": "Event",
    "context": "HR Policy"
  }
}
```

### Training Configuration

| Parameter | Value |
|-----------|-------|
| Base Model | sentence-transformers/all-MiniLM-L12-v2 |
| Epochs | 8 |
| Batch Size | 8 |
| Learning Rate | 1e-6 |
| Loss Function | Multiple Negatives Ranking Loss |
| Max Sequence Length | 512 |
| Warmup Ratio | 0.5 |
| Quantization | INT8 dynamic activation, INT4 weights (QAT) |

### Loss Function

Uses Multiple Negatives Ranking Loss (MNRL) which treats each positive pair in a batch as a negative for other pairs, effectively creating many negative samples without explicit hard negatives.

## Evaluation Results

### Test Set Performance

| Metric | Baseline | Fine-tuned | Improvement |
|--------|----------|------------|-------------|
| Accuracy@1 | 0.47 | **0.67** | +43% |
| Accuracy@3 | 0.80 | **0.83** | +4% |
| Accuracy@5 | 0.88 | **0.93** | +6% |
| Accuracy@10 | 0.92 | **0.97** | +5% |
| MRR@10 | 0.65 | **0.77** | +18% |

### Eval Set Performance

| Metric | Baseline | Fine-tuned | Improvement |
|--------|----------|------------|-------------|
| Accuracy@1 | 0.54 | **0.70** | +30% |
| Accuracy@3 | 0.88 | **0.86** | - |
| Accuracy@5 | 0.96 | **0.96** | - |
| Accuracy@10 | 0.98 | **1.00** | +2% |
| MRR@10 | 0.71 | **0.80** | +13% |

## Model Architecture

- **Type**: Sentence Transformer (Bi-Encoder)
- **Base**: MiniLM-L12 (12 transformer layers)
- **Pooling**: Mean pooling over token embeddings
- **Normalization**: L2 normalization
- **Output**: 384-dimensional embedding vector
- **Quantization**: INT8 dynamic activation + INT4 weight (via torchao)

## Intended Use

This model is designed for:

- **Memory Retrieval**: Finding relevant memories in agent memory systems
- **Semantic Search**: Ranking documents by relevance to natural language queries
- **Clustering**: Grouping related memories by type or topic
- **Deduplication**: Identifying semantically similar documents

## Limitations

- Trained on synthetic data; may require domain-specific fine-tuning for production use
- Optimized for the structured format described above; may not generalize well to unstructured text
- English language only
- Small training dataset (300 examples)

## License

Apache 2.0 License
