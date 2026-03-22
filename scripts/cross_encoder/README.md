# Train and evaluate the cross encoder ranking model

## Model

Fine-tuned `cross-encoder/ms-marco-MiniLM-L12-v2` using QAT (Quantization-Aware Training) with Int8 dynamic activation and Int4 weights. Exported to ONNX for CPU inference.

## Input format

The model was trained on the Memex memory unit format with date ranges:

```
[Start: January 01, 2024 (2024-01-01)] [End: ongoing] [World] Ruby Martinez is the Department Head...
```

Date range cases:
- **Start + no end (ongoing):** `[Start: ...] [End: ongoing]`
- **Start + end (completed):** `[Start: ...] [End: March 15, 2025 (2025-03-15)]`
- **No dates (timeless):** No date prefix at all — `[World] Text...`
- **End only (rare):** `[End: March 15, 2025 (2025-03-15)]`

This format allows the model to distinguish between current state, completed events, and timeless facts.

## Data

- `data/train_300.jsonl` — 315 triplets (query, positive, negative)
- `data/eval_70.jsonl` — 70 triplets
- `data/test_60.jsonl` — 60 triplets

All data uses fictional names and organizations. Memory unit texts follow the Memex extraction format with `| When: | Where: | Involving:` metadata.

## Commands

```bash
just train   # QAT fine-tuning + ONNX export (~20 min on CPU)
just eval    # Evaluate baseline (FP32 untrained) and trained model
```

## Results

### v2 — Date range format (2026-03-22)

Training: 10 epochs, batch size 8, margin ranking loss (margin=1.0), lr=6e-6.

**Note on absolute scores:** The v2 baseline scores are significantly higher than v1 (0.89 vs 0.63 accuracy@1 on eval). This is because the v1 data contained low-quality examples (single-word triplets, trivially short texts) that confused even a strong baseline model. The v2 data was fully rewritten with higher-quality, natural-sounding memory units, making the task more learnable overall. The negatives may also be somewhat easier to distinguish than in production retrieval where candidates share more structural similarity. The relative improvement between baseline and trained remains the primary signal.

#### Baseline (FP32 untrained `cross-encoder/ms-marco-MiniLM-L12-v2`)

| Metric | Eval | Test |
|---|---|---|
| accuracy@1 | 0.8857 | 0.8667 |
| accuracy@3 | 1.0000 | 0.9833 |
| accuracy@5 | 1.0000 | 1.0000 |
| mrr@10 | 0.9429 | 0.9208 |

#### Trained (QAT Int8/Int4)

| Metric | Eval | Test | Δ vs baseline |
|---|---|---|---|
| accuracy@1 | 0.9571 | 0.9333 | +7-8% |
| accuracy@3 | 1.0000 | 0.9833 | — |
| accuracy@5 | 1.0000 | 1.0000 | — |
| mrr@10 | 0.9786 | 0.9597 | +3-4% |

### v1 — Single date format (previous)

Larger eval corpus (shared with training data), so absolute scores are lower.

#### Baseline

| Metric | Eval | Test |
|---|---|---|
| accuracy@1 | 0.6286 | 0.5167 |
| accuracy@3 | 0.8143 | 0.8000 |
| accuracy@5 | 0.8571 | 0.8500 |
| mrr@10 | 0.7290 | 0.6681 |

#### Trained

| Metric | Eval | Test |
|---|---|---|
| accuracy@1 | 0.6714 | 0.7167 |
| accuracy@3 | 0.8714 | 0.8167 |
| accuracy@5 | 0.9000 | 0.8833 |
| mrr@10 | 0.7728 | 0.7788 |
