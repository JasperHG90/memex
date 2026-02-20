# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "datasets>=4.4.2",
#     "jsonlines>=4.0.0",
#     "numpy>=2.4.0",
#     "onnxruntime>=1.23.2",
#     "rich>=14.2.0",
#     "tokenizers>=0.22.1",
#     "typer>=0.21.0",
# ]
# ///
import pathlib as plb
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
import jsonlines
from rich.console import Console
from rich.table import Table
import typer
from datetime import datetime

console = Console()
app = typer.Typer()


class ONNXCrossEncoderWrapper:
    def __init__(self, model_dir):
        model_path = plb.Path(model_dir) / 'model.onnx'
        tokenizer_path = plb.Path(model_dir) / 'tokenizer.json'

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')

        self.session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])

        # Check inputs
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.use_token_type_ids = 'token_type_ids' in self.input_names

    def predict(self, text_a, text_b):
        # Tokenize single pair
        enc = self.tokenizer.encode(text_a, text_b)

        inputs = {
            'input_ids': np.array([enc.ids], dtype=np.int64),
            'attention_mask': np.array([enc.attention_mask], dtype=np.int64),
        }
        if self.use_token_type_ids:
            inputs['token_type_ids'] = np.array([enc.type_ids], dtype=np.int64)

        return self.session.run(None, inputs)[0].item()


def format_hindsight_document(doc_data: dict) -> str:
    """
    Transforms the raw data dictionary into the model-ready string.
    Format: "[Date: Month DD, YYYY (YYYY-MM-DD)] Context: Narrative Text"
    """
    raw_text = doc_data['text']
    context = doc_data.get('context', '')
    occurred_start_str = doc_data.get('occurred_start')

    # 1. Base Text
    final_text = raw_text

    # 2. Prepend Context (if exists)
    if context:
        final_text = f'{context}: {final_text}'

    # 3. Prepend Temporal Anchor (CRITICAL)
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


@app.command()
def run():
    model_dir = './ms-marco-minilm-l12-hindsight-reranker'
    eval_file = './data/eval_70.jsonl'

    console.print(f'Loading model from {model_dir}...')
    model = ONNXCrossEncoderWrapper(model_dir)

    console.print(f'Loading data from {eval_file}...')
    with jsonlines.open(eval_file) as reader:
        data = list(reader)

    correct = 0
    total = len(data)

    table = Table(title='Pairwise Check (Random Sample)')
    table.add_column('Query', style='cyan', no_wrap=False)
    table.add_column('Positive', style='cyan', no_wrap=False)
    table.add_column('Negative', style='cyan', no_wrap=False)
    table.add_column('Pos Score', style='green')
    table.add_column('Neg Score', style='red')
    table.add_column('Result')

    for i, row in enumerate(data):
        query = row['query']
        pos = format_hindsight_document(row['positive'])
        neg = format_hindsight_document(row['negative'])

        s_pos = model.predict(query, pos)
        s_neg = model.predict(query, neg)

        is_correct = s_pos > s_neg
        if is_correct:
            correct += 1

        # Print first 10 for sanity check
        table.add_row(query, pos, neg, f'{s_pos:.4f}', f'{s_neg:.4f}', '✅' if is_correct else '❌')

    console.print(table)

    accuracy = correct / total
    console.print(f'\n[bold]Pairwise Accuracy: {accuracy:.2%}[/bold]')
    console.print('(This metric matches your Training Loss objectives)')


if __name__ == '__main__':
    app()
