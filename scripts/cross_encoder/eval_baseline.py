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
#     "tqdm>=4.66.0",
# ]
# ///
import pathlib as plb
import hashlib
from typing import List, Tuple

import jsonlines
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from rich.table import Table
from rich.console import Console
from rich.progress import track
import typer
from datasets import Dataset
from datetime import datetime

console = Console()
app = typer.Typer(name='eval-ce-fp32', no_args_is_help=True)


def _format_date(dt: datetime) -> str:
    """Format a datetime as 'Month DD, YYYY (YYYY-MM-DD)'."""
    return f'{dt.strftime("%B %d, %Y")} ({dt.strftime("%Y-%m-%d")})'


def format_hindsight_document(doc_data: dict) -> str:
    """
    Transforms the raw data dictionary into the model-ready string.
    Format: "[Start: ...] [End: ongoing|date] [Type] Context: Narrative Text"
    """
    raw_text = doc_data['text']
    context = doc_data.get('context', '')
    mem_type = doc_data.get('type', 'Unknown')
    occurred_start_str = doc_data.get('occurred_start')
    occurred_end_str = doc_data.get('occurred_end')

    # 1. Base Text
    final_text = raw_text

    # 2. Prepend Context (if exists)
    if context:
        final_text = f'{context}: {final_text}'

    # 3. Prepend Type
    final_text = f'[{mem_type}] {final_text}'

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


def load_dataset(file_path: plb.Path):
    console.print(f'Loading dataset from {str(file_path)}')
    with jsonlines.open(file_path, 'r') as reader:
        data = list(reader)
    data_ = [
        dict(
            row,
            positive=format_hindsight_document(row['positive']),
            negative=format_hindsight_document(row['negative']),
            query=row['query'],
        )
        for row in data
    ]
    return Dataset.from_list(data_)


class ONNXCrossEncoderWrapper:
    def __init__(self, model_dir, model_name='model.onnx'):
        model_path = plb.Path(model_dir) / model_name
        tokenizer_path = plb.Path(model_dir) / 'tokenizer.json'

        if not model_path.exists():
            raise FileNotFoundError(f'Model not found at {model_path}')

        # Load Tokenizer
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')

        # Load ONNX Model
        self.session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])

        # Inspect Input Requirements (Adaptive Handling)
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.use_token_type_ids = 'token_type_ids' in self.input_names

    def predict(self, pairs: List[Tuple[str, str]], batch_size=32) -> np.ndarray:
        all_scores = []

        for i in range(0, len(pairs), batch_size):
            batch = pairs[i : i + batch_size]
            queries = [p[0] for p in batch]
            docs = [p[1] for p in batch]

            # Tokenize pairs
            encodings = self.tokenizer.encode_batch(list(zip(queries, docs)))

            # Build Input Dictionary
            onnx_inputs = {
                'input_ids': np.array([e.ids for e in encodings], dtype=np.int64),
                'attention_mask': np.array([e.attention_mask for e in encodings], dtype=np.int64),
            }

            # Add token_type_ids if the model expects them (Common in FP32 BERT)
            if self.use_token_type_ids:
                onnx_inputs['token_type_ids'] = np.array(
                    [e.type_ids for e in encodings], dtype=np.int64
                )

            # Run Inference
            logits = self.session.run(None, onnx_inputs)[0]

            # Flatten to 1D array to handle [Batch, 1] or [Batch] output shapes
            all_scores.append(logits.flatten())

        return np.concatenate(all_scores)


def evaluate_reranking(model: ONNXCrossEncoderWrapper, ds: Dataset):
    # 1. Build Corpus
    corpus = {}
    for row in ds:
        corpus[hashlib.md5(row['positive'].encode('utf-8')).hexdigest()] = row['positive']
        corpus[hashlib.md5(row['negative'].encode('utf-8')).hexdigest()] = row['negative']

    corpus_ids = list(corpus.keys())
    corpus_texts = list(corpus.values())

    # 2. Metrics Accumulators
    metrics = {
        'accuracy@1': 0,
        'accuracy@3': 0,
        'accuracy@5': 0,
        'accuracy@10': 0,
        'accuracy@15': 0,
        'accuracy@20': 0,
        'mrr@10': 0,
        'mrr@20': 0,
    }

    # 3. Evaluate each query
    # This is O(N^2) but fine for N=50 or N=100
    for row in track(ds, description='Evaluating...'):
        query = row['query']
        target_id = hashlib.md5(row['positive'].encode('utf-8')).hexdigest()

        # Prepare pairs: (Query, Doc) for EVERY doc in corpus
        pairs = [(query, doc) for doc in corpus_texts]

        # Get Scores
        scores = model.predict(pairs, batch_size=16)

        # Zip IDs with scores
        scored_results = list(zip(corpus_ids, scores))

        # Sort descending by score
        ranked_results = sorted(scored_results, key=lambda x: x[1], reverse=True)

        # Find rank of the True Positive
        # Rank is 1-based index
        try:
            rank = next(i for i, (did, _) in enumerate(ranked_results) if did == target_id) + 1
        except StopIteration:
            rank = float('inf')  # Should not happen if corpus is built correctly

        # Update Metrics
        if rank <= 1:
            metrics['accuracy@1'] += 1
        if rank <= 3:
            metrics['accuracy@3'] += 1
        if rank <= 5:
            metrics['accuracy@5'] += 1
        if rank <= 10:
            metrics['accuracy@10'] += 1
            metrics['mrr@10'] += 1.0 / rank
        if rank <= 15:
            metrics['accuracy@15'] += 1
        if rank <= 20:
            metrics['accuracy@20'] += 1
            metrics['mrr@20'] += 1.0 / rank

    total = len(ds)
    return {k: v / total for k, v in metrics.items()}


def print_metrics_table(metrics: dict):
    table = Table('Metric', 'Score')
    for k, v in metrics.items():
        table.add_row(k, f'{v:.4f}')
    console.print(table)


def main():
    # Update this to your FP32 output directory
    model_dir = './ms-marco-minilm-l12-hindsight-reranker-fp32'

    console.print(f'Loading FP32 model from [bold]{model_dir}[/bold]...')
    try:
        wraps = ONNXCrossEncoderWrapper(model_dir=model_dir)
    except Exception as e:
        console.print(f'[red]Error loading model:[/red] {e}')
        return

    console.print('Loading datasets...')
    eval_ds = load_dataset(plb.Path('./data/eval_70.jsonl'))
    test_ds = load_dataset(plb.Path('./data/test_60.jsonl'))

    console.print('[bold]Evaluating on Eval Set[/bold]...')
    print_metrics_table(evaluate_reranking(wraps, eval_ds))

    console.print('[bold]Evaluating on Test Set[/bold]...')
    print_metrics_table(evaluate_reranking(wraps, test_ds))


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
