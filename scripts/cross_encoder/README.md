# Train and evaluate the cross encoder ranking model

## Baseline (FP32)

Untrained `cross-encoder/ms-marco-MiniLM-L12-v2`

Eval set

┏━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric      ┃ Score  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━┩
│ accuracy@1  │ 0.6286 │
│ accuracy@3  │ 0.8143 │
│ accuracy@5  │ 0.8571 │
│ accuracy@10 │ 0.9000 │
│ accuracy@15 │ 0.9143 │
│ accuracy@20 │ 0.9286 │
│ mrr@10      │ 0.7290 │
│ mrr@20      │ 0.7308 │
└─────────────┴────────┘

Test set

┏━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric      ┃ Score  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━┩
│ accuracy@1  │ 0.5167 │
│ accuracy@3  │ 0.8000 │
│ accuracy@5  │ 0.8500 │
│ accuracy@10 │ 0.8833 │
│ accuracy@15 │ 0.9000 │
│ accuracy@20 │ 0.9167 │
│ mrr@10      │ 0.6681 │
│ mrr@20      │ 0.6704 │
└─────────────┴────────┘

## Training Results

### Evaluation

┏━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric      ┃ Score  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━┩
│ accuracy@1  │ 0.6714 │
│ accuracy@3  │ 0.8714 │
│ accuracy@5  │ 0.9000 │
│ accuracy@10 │ 0.9143 │
│ accuracy@15 │ 0.9857 │
│ accuracy@20 │ 0.9857 │
│ mrr@10      │ 0.7728 │
│ mrr@20      │ 0.7784 │
└─────────────┴────────┘

### Test

┏━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric      ┃ Score  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━┩
│ accuracy@1  │ 0.7167 │
│ accuracy@3  │ 0.8167 │
│ accuracy@5  │ 0.8833 │
│ accuracy@10 │ 0.9167 │
│ accuracy@15 │ 0.9333 │
│ accuracy@20 │ 0.9667 │
│ mrr@10      │ 0.7788 │
│ mrr@20      │ 0.7820 │
└─────────────┴────────┘
