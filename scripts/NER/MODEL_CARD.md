---
license: apache-2.0
base_model: dslim/distilbert-NER
tags:
- token-classification
- ner
- quantization
- onnx
- distilbert
language:
- en
pipeline_tag: token-classification
---

# DistilBERT NER Quantized (QAT)

A quantized version of [dslim/distilbert-NER](https://huggingface.co/dslim/distilbert-NER) optimized for efficient named entity recognition. This model uses Quantization-Aware Training (QAT) with INT8 dynamic activation and INT4 weight quantization, exported to ONNX format for production deployment.

## Model Description

This model is a quantized version of the DistilBERT NER model, fine-tuned on the CoNLL-2003 dataset for named entity recognition. The quantization preserves accuracy while significantly reducing model size and inference latency.

### Key Features

- **Efficient Inference**: INT4 weights + INT8 dynamic activations for fast CPU inference
- **ONNX Export**: Ready for production deployment with ONNX Runtime
- **Entity Types**: Recognizes PER (Person), ORG (Organization), LOC (Location), MISC (Miscellaneous)
- **BIO Tagging**: Uses Begin-Inside-Outside tagging scheme for precise entity boundaries

## Usage

### With ONNX Runtime

```python
import onnxruntime as ort
import numpy as np
from transformers import AutoTokenizer
import json

class NEROnnxRunner:
    def __init__(self, model_dir: str):
        with open(f"{model_dir}/config.json", "r") as f:
            self.id2label = {int(k): v for k, v in json.load(f).items()}
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, use_fast=True)
        self.session = ort.InferenceSession(
            f"{model_dir}/model.onnx", providers=["CPUExecutionProvider"]
        )

    def predict(self, text: str) -> list[dict]:
        encoding = self.tokenizer(
            text,
            return_tensors="np",
            padding="max_length",
            truncation=True,
            max_length=128,
            return_offsets_mapping=True,
        )

        ort_inputs = {
            "input_ids": encoding["input_ids"].astype(np.int64),
            "attention_mask": encoding["attention_mask"].astype(np.int64),
        }
        logits = self.session.run(None, ort_inputs)[0]
        predictions = np.argmax(logits, axis=2)[0]

        batch_encoding = self.tokenizer(
            text, truncation=True, max_length=128, return_offsets_mapping=True
        )
        offsets = batch_encoding["offset_mapping"]

        entities = []
        for idx, (start, end) in enumerate(offsets):
            if start == end:
                continue
            label = self.id2label.get(predictions[idx], "O")
            if label != "O":
                entities.append({
                    "word": text[start:end],
                    "label": label,
                    "start": start,
                    "end": end,
                })
        return entities

runner = NEROnnxRunner("./distilbert-ner-qat-int4")
entities = runner.predict("Apple Inc. is based in Cupertino, California.")
# [{'word': 'Apple Inc.', 'label': 'B-ORG'}, {'word': 'Cupertino', 'label': 'B-LOC'}, {'word': 'California', 'label': 'B-LOC'}]
```

## Entity Types

| Label | Description |
|-------|-------------|
| PER | Person names |
| ORG | Organizations, companies, institutions |
| LOC | Locations, cities, countries |
| MISC | Miscellaneous entities (events, products, etc.) |

### BIO Tagging Scheme

- `B-X`: Beginning of entity type X
- `I-X`: Inside entity type X
- `O`: Outside any entity

## Training Details

### Calibration Data

The model was calibrated using 300 samples from the AG News dataset for QAT observer statistics.

### Quantization Configuration

| Parameter | Value |
|-----------|-------|
| Base Model | dslim/distilbert-NER |
| Weight Quantization | INT4 |
| Activation Quantization | INT8 (dynamic) |
| Calibration Samples | 300 |
| Max Sequence Length | 128 |
| Export Format | ONNX (opset 18) |

## Evaluation Results

### Test Set Performance (30 samples)

| Metric | Score |
|--------|-------|
| Row Recall | 90.00% |
| Full Recall Rows | 27/30 |

*Row Recall measures the percentage of samples where all expected entities were successfully extracted.*

### Sample Predictions

| Text | Entities |
|------|----------|
| Sam Altman returned to OpenAI as CEO | Sam Altman (PER), OpenAI (ORG) |
| The European Central Bank announced... | European Central Bank (ORG), Frankfurt (LOC) |
| Barack Obama gave a speech at COP26 in Glasgow | Barack Obama (PER), COP26 (MISC), Glasgow (LOC) |
| Microsoft agreed to acquire Activision Blizzard | Microsoft (ORG), Activision Blizzard (ORG) |

## Model Architecture

- **Type**: DistilBERT for Token Classification
- **Layers**: 6 transformer layers
- **Hidden Size**: 768
- **Attention Heads**: 12
- **Parameters**: ~66M (original)
- **Output**: 9 labels (O, B-PER, I-PER, B-ORG, I-ORG, B-LOC, I-LOC, B-MISC, I-MISC)

## Intended Use

- **Information Extraction**: Extracting named entities from text documents
- **Document Processing**: Identifying people, organizations, and locations
- **Knowledge Graph Construction**: Entity extraction for graph databases
- **Memory Systems**: Entity recognition for structured memory documents following the [Hindsight](https://arxiv.org/abs/2512.12818) memory architecture

## Limitations

- English language only
- May struggle with rare or domain-specific entity types
- Quantization may introduce slight accuracy degradation for edge cases
- Max sequence length of 128 tokens

## License

Apache 2.0 License
