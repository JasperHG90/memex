# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "accelerate>=0.26.0",
#     "datasets>=4.4.2",
#     "jsonlines>=4.0.0",
#     "rich>=14.2.0",
#     "sentence-transformers>=5.2.0",
#     "typer>=0.21.0",
#     "scikit-learn>=1.4.0",
#     "onnx>=1.14.0",
# ]
# ///
import pathlib as plb
import shutil

import torch
import torch.nn as nn
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer
import jsonlines
from datasets import Dataset
from rich.console import Console
import typer
from datetime import datetime

MODEL_ID = 'cross-encoder/ms-marco-MiniLM-L12-v2'
OUTPUT_DIR = plb.Path('./ms-marco-minilm-l12-hindsight-reranker-fp32')
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True)

console = Console()
app = typer.Typer(name='train-ranking', no_args_is_help=True)


def format_hindsight_document(doc_data: dict) -> str:
    """
    Transforms the raw data dictionary into the model-ready string.
    Format: "[Date: Month DD, YYYY (YYYY-MM-DD)] [Type] Context: Narrative Text"
    """
    raw_text = doc_data['text']
    context = doc_data.get('context', '')
    mem_type = doc_data.get('type', 'Unknown')
    occurred_start_str = doc_data.get('occurred_start')

    # 1. Base Text
    final_text = raw_text

    # 2. Prepend Context (if exists)
    if context:
        final_text = f'{context}: {final_text}'

    # 3. Prepend Type (New Standard)
    final_text = f'[{mem_type}] {final_text}'

    # 4. Prepend Temporal Anchor (CRITICAL)
    if occurred_start_str:
        try:
            # Parse ISO string back to datetime object
            dt = datetime.fromisoformat(occurred_start_str)

            # Format: January 14, 2026
            date_readable = dt.strftime('%B %d, %Y')
            # Format: 2026-01-14
            date_iso = dt.strftime('%Y-%m-%d')

            # Exact Hindsight format
            final_text = f'[Date: {date_readable} ({date_iso})] {final_text}'
        except ValueError:
            pass  # Fallback if date is malformed

    return final_text


class TripletCollator:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def __call__(self, features):
        queries = [f['query'] for f in features]
        positives = [format_hindsight_document(f['positive']) for f in features]
        negatives = [format_hindsight_document(f['negative']) for f in features]

        all_queries = queries + queries
        all_passages = positives + negatives

        batch = self.tokenizer(
            all_queries,
            all_passages,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors='pt',
        )
        return batch


class RankingTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        outputs = model(**inputs)
        logits = outputs.logits.squeeze(-1)

        batch_size = logits.size(0) // 2
        scores_pos = logits[:batch_size]
        scores_neg = logits[batch_size:]

        loss_fct = nn.MarginRankingLoss(margin=1.0)
        target = torch.ones_like(scores_pos)
        loss = loss_fct(scores_pos, scores_neg, target)

        return (loss, outputs) if return_outputs else loss

    def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys=None):
        inputs = self._prepare_inputs(inputs)
        with torch.no_grad():
            loss, outputs = self.compute_loss(model, inputs, return_outputs=True)
            loss = loss.mean().detach()
        if prediction_loss_only:
            return (loss, None, None)
        return (loss, outputs.logits, None)


class OnnxCrossEncoderWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    # CRITICAL FIX: Explicitly list token_type_ids in arguments
    def forward(self, input_ids, attention_mask, token_type_ids):
        outputs = self.model(
            input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids
        )
        return outputs.logits.squeeze(-1)


def load_dataset(file_path: plb.Path):
    console.print(f'Loading triplets from {str(file_path)}')
    with jsonlines.open(file_path, 'r') as reader:
        data = list(reader)
    return Dataset.from_list(data).shuffle()


def main():
    console.print(f'Loading model: {MODEL_ID}')
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, num_labels=1)

    console.print('Exporting to ONNX...')
    model.cpu()
    model.eval()

    # 1. Initialize Wrapper
    wrapper = OnnxCrossEncoderWrapper(model)

    # 2. Create Dummy Input using Tokenizer
    # We use Batch Size 2 to force dynamic shapes (fixes LayerNorm issue)
    # Tokenizer automatically generates 'token_type_ids' for sentence pairs
    dummy_inputs = tokenizer(
        ['Query A', 'Query B'],
        ['Doc A', 'Doc B'],
        return_tensors='pt',
        padding='max_length',
        max_length=128,
    )

    input_ids = dummy_inputs['input_ids']
    attention_mask = dummy_inputs['attention_mask']
    token_type_ids = dummy_inputs['token_type_ids']

    # 3. Standard TorchScript Export (Safe for FP32)
    torch.onnx.export(
        wrapper,
        (input_ids, attention_mask, token_type_ids),  # Pass all 3 args
        str(OUTPUT_DIR / 'model.onnx'),
        input_names=['input_ids', 'attention_mask', 'token_type_ids'],  # Name all 3 inputs
        output_names=['logits'],
        dynamic_axes={
            'input_ids': {0: 'batch_size', 1: 'seq_len'},
            'attention_mask': {0: 'batch_size', 1: 'seq_len'},
            'token_type_ids': {0: 'batch_size', 1: 'seq_len'},
        },
        opset_version=17,
    )

    tokenizer.save_pretrained(OUTPUT_DIR)
    model.config.save_pretrained(OUTPUT_DIR)
    console.print(f'[bold green]Model saved to {OUTPUT_DIR}[/bold green]')


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
