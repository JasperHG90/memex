# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "accelerate>=0.26.0",
#     "datasets>=4.4.2",
#     "jsonlines>=4.0.0",
#     "onnxscript>=0.5.7",
#     "rich>=14.2.0",
#     "sentence-transformers>=5.2.0",
#     "torchao>=0.15.0",
#     "typer>=0.21.0",
#     "scikit-learn>=1.4.0",
# ]
# ///
import pathlib as plb
import shutil
from typing import cast
from datetime import datetime

from torch.nn import Module
import torch
import torch.nn as nn
from torch.export import Dim
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
    Trainer,
    TrainingArguments,
)
import jsonlines
from datasets import Dataset
from torchao.quantization import (
    quantize_,
    Int8DynamicActivationIntxWeightConfig as Int8DynamicActivationInt4WeightConfig,
)
from torchao.quantization.qat import QATConfig, QATStep
from rich.console import Console
import typer

MODEL_ID = 'cross-encoder/ms-marco-MiniLM-L12-v2'
OUTPUT_DIR = plb.Path('./ms-marco-minilm-l12-hindsight-reranker')
DEVICE = 'cpu'

if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True)

console = Console()
app = typer.Typer(
    name='train-ranking',
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def _format_date(dt: datetime) -> str:
    """Format a datetime as 'Month DD, YYYY (YYYY-MM-DD)'."""
    return f'{dt.strftime("%B %d, %Y")} ({dt.strftime("%Y-%m-%d")})'


def format_hindsight_document(doc_data: dict) -> str:
    """
    Transforms the raw data dictionary into the model-ready string.
    Format: "[Start: ...] [End: ongoing|date] [Type] Context: Narrative Text"

    Date range cases:
    - Start + no end (ongoing): [Start: ...] [End: ongoing]
    - Start + end (completed):  [Start: ...] [End: ...]
    - No start + no end:        No date prefix (undated facts not penalized)
    - No start + end:           [End: ...]
    """
    raw_text = doc_data['text']
    context = doc_data.get('context', '')
    occurred_start_str = doc_data.get('occurred_start')
    occurred_end_str = doc_data.get('occurred_end')
    fact_type = doc_data.get('type', 'Unknown')

    # 1. Base Text
    final_text = raw_text

    # 2. Prepend Context (if exists)
    if context:
        final_text = f'{context}: {final_text}'

    # 3. Prepend Type
    f_type = fact_type.capitalize()
    final_text = f'[{f_type}] {final_text}'

    # 4. Prepend Date Range
    date_parts: list[str] = []
    if occurred_start_str:
        try:
            dt = datetime.fromisoformat(occurred_start_str)
            date_parts.append(f'[Start: {_format_date(dt)}]')
        except ValueError:
            pass

    if occurred_start_str or occurred_end_str:
        if occurred_end_str:
            try:
                dt = datetime.fromisoformat(occurred_end_str)
                date_parts.append(f'[End: {_format_date(dt)}]')
            except ValueError:
                date_parts.append('[End: ongoing]')
        else:
            date_parts.append('[End: ongoing]')

    if date_parts:
        final_text = f'{" ".join(date_parts)} {final_text}'

    return final_text


# -------------------------------------------------------------------------
# 1. Custom Data Collator for Triplets
# -------------------------------------------------------------------------
class TripletCollator:
    """
    Transforms a list of features into a batch where:
    - First half of batch = (Query, Positive)
    - Second half of batch = (Query, Negative)
    """

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def __call__(self, features):
        # Extract lists
        queries = [f['query'] for f in features]
        positives = [format_hindsight_document(f['positive']) for f in features]
        negatives = [format_hindsight_document(f['negative']) for f in features]

        # Construct the full list of pairs to tokenize
        # We put all Positives first, then all Negatives
        # This aligns with the Trainer logic: scores_pos = logits[:batch_size]
        all_queries = queries + queries
        all_passages = positives + negatives

        # Tokenize everything at once
        # This guarantees that input_ids will have the same sequence length (dim 1)
        batch = self.tokenizer(
            all_queries,
            all_passages,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors='pt',
        )

        return batch


# -------------------------------------------------------------------------
# 2. Custom Trainer for Margin Ranking Loss
# -------------------------------------------------------------------------
class RankingTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        # 1. Forward Pass
        outputs = model(**inputs)
        logits = outputs.logits.squeeze(-1)  # Shape: [2 * batch_size]

        # 2. Split batch back into Positive and Negative halves
        # Note: TripletCollator stacks [Positives, Negatives]
        batch_size = logits.size(0) // 2
        scores_pos = logits[:batch_size]
        scores_neg = logits[batch_size:]

        # 3. Margin Ranking Loss
        # We want: score_pos > score_neg + margin
        loss_fct = nn.MarginRankingLoss(margin=1.0)

        # Target = 1 means inputs_1 (pos) should be ranked higher than inputs_2 (neg)
        target = torch.ones_like(scores_pos)

        loss = loss_fct(scores_pos, scores_neg, target)

        return (loss, outputs) if return_outputs else loss

    # --- THIS METHOD IS NEW ---
    def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys=None):
        """
        Custom prediction step to force loss calculation during evaluation
        even if 'labels' are missing from inputs.
        """
        inputs = self._prepare_inputs(inputs)

        with torch.no_grad():
            # Force the use of our custom compute_loss logic
            loss, outputs = self.compute_loss(model, inputs, return_outputs=True)
            loss = loss.mean().detach()

        if prediction_loss_only:
            return (loss, None, None)

        # Return (loss, logits, labels)
        # We pass None for labels since we don't use them
        return (loss, outputs.logits, None)


# -------------------------------------------------------------------------
# 3. ONNX Wrapper (Same as before)
# -------------------------------------------------------------------------
class OnnxCrossEncoderWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    # CRITICAL FIX: Explicitly include token_type_ids in signature
    def forward(self, input_ids, attention_mask, token_type_ids):
        outputs = self.model(
            input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids
        )
        return outputs.logits.squeeze(-1)


# -------------------------------------------------------------------------
# 4. Pipeline
# -------------------------------------------------------------------------
def load_dataset(file_path: plb.Path) -> Dataset:
    console.print(f'Loading triplets from {str(file_path)}')
    with jsonlines.open(file_path, 'r') as reader:
        data = list(reader)
    return Dataset.from_list(data).shuffle()


def save_model_to_onnx(
    model: Module, output_path: plb.Path, config: Int8DynamicActivationInt4WeightConfig
):
    # Change min=1 to min=2 here
    batch = Dim('batch', min=2, max=1024)
    seq = Dim('seq', min=2, max=512)

    dynamic_shapes = {
        'input_ids': {0: batch, 1: seq},
        'attention_mask': {0: batch, 1: seq},
        'token_type_ids': {0: batch, 1: seq},
    }

    # Keep dummy input size > 1 (e.g. 128 is fine)
    dummy_input_ids = torch.ones((2, 128), dtype=torch.long)
    dummy_mask = torch.ones((2, 128), dtype=torch.long)
    dummy_types = torch.zeros((2, 128), dtype=torch.long)

    onnx_path = OUTPUT_DIR / 'model.onnx'

    console.print('Converting QAT model for export...')
    quantize_(cast(Module, model), QATConfig(config, step=QATStep.CONVERT))

    wrapper = OnnxCrossEncoderWrapper(model)

    console.print(f'Exporting model to {output_path}...')
    torch.onnx.export(
        wrapper,
        (dummy_input_ids, dummy_mask, dummy_types),  # Pass 3 args
        str(onnx_path),
        input_names=['input_ids', 'attention_mask', 'token_type_ids'],  # Name 3 args
        output_names=['logits'],
        dynamic_shapes=dynamic_shapes,
        opset_version=18,
        dynamo=True,  # Must be True for torchao
    )


def main():
    console.print(f'Loading model: {MODEL_ID}')
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, num_labels=1)

    # QAT Config
    base_config = Int8DynamicActivationInt4WeightConfig()
    console.print('Applying QAT Prepare...')
    quantize_(cast(Module, model), QATConfig(base_config, step=QATStep.PREPARE))

    # Data
    train_ds = load_dataset(plb.Path('./data/train_300.jsonl'))

    # Eval Data (Using simplified version for evaluating ranking loss)
    # Note: Accuracy metrics are harder to calculate in ranking loop without extra logic,
    # so we rely on Loss decreasing as primary signal for this script.
    eval_ds = load_dataset(plb.Path('./data/eval_70.jsonl'))

    collator = TripletCollator(tokenizer)

    args = TrainingArguments(
        output_dir='output/ms-marco-ranking',
        num_train_epochs=10,
        per_device_train_batch_size=8,  # Effective batch size is 8 (4 pos + 4 neg)
        learning_rate=6e-6,
        logging_steps=10,
        save_strategy='no',
        report_to='none',
        use_cpu=(DEVICE == 'cpu'),
        remove_unused_columns=False,  # Essential for custom collator
        dataloader_drop_last=True,  # Ensure equal split for pos/neg
        eval_strategy='steps',  # Evaluate during training, not just at the end
        eval_steps=50,  # Evaluate every 10 steps (since your dataset is small)
        load_best_model_at_end=False,
    )

    trainer = RankingTrainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=collator,
    )

    console.print('Starting Pairwise Ranking Training...')
    trainer.train()

    # Export Logic
    console.print('Applying QAT Convert...')

    save_model_to_onnx(
        model=model,
        output_path=OUTPUT_DIR,
        config=base_config,
    )

    tokenizer.save_pretrained(OUTPUT_DIR)
    model.config.save_pretrained(OUTPUT_DIR)
    console.print('[bold green]Done.[/bold green]')


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
