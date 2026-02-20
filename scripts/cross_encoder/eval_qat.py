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
"""
Evaluates the Cross-Encoder ONNX model using Reranking metrics.
It simulates retrieval by scoring the query against ALL documents in the dataset.
"""

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

app = typer.Typer(
    name='eval-ce',
    help='Evaluate the Cross-Encoder ONNX model.',
    no_args_is_help=True,
    pretty_exceptions_enable=True,
)


def load_dataset(file_path: plb.Path):
    console.print(f'Loading dataset from {str(file_path)}')
    with jsonlines.open(file_path, 'r') as reader:
        data = list(reader)
    data_ = []
    for item in data:
        item['positive'] = format_hindsight_document(item['positive'])
        item['negative'] = format_hindsight_document(item['negative'])
        data_.append(item)
    return Dataset.from_list(data_)  # No shuffle needed for eval


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


class ONNXCrossEncoderWrapper:
    def __init__(self, model_dir, model_name='model.onnx'):
        self.tokenizer = Tokenizer.from_file(str(plb.Path(model_dir) / 'tokenizer.json'))
        # Enable truncation/padding settings consistent with training
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')

        self.session = ort.InferenceSession(
            str(plb.Path(model_dir) / model_name), providers=['CPUExecutionProvider']
        )

    def predict(self, pairs: List[Tuple[str, str]], batch_size=32) -> np.ndarray:
        """
        Scoring a list of (Query, Doc) pairs.
        Returns a numpy array of logits (scores).
        """
        all_scores = []

        for i in range(0, len(pairs), batch_size):
            batch = pairs[i : i + batch_size]
            queries = [p[0] for p in batch]
            docs = [p[1] for p in batch]

            # Tokenize as pairs
            encodings = self.tokenizer.encode_batch(list(zip(queries, docs)))

            # Prepare Inputs
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)

            onnx_inputs = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
                'token_type_ids': token_type_ids,
            }

            # Run Inference
            # Output is [batch_size] (logits)
            logits = self.session.run(None, onnx_inputs)[0]
            all_scores.append(logits)

        return np.concatenate(all_scores)


def evaluate_reranking(model: ONNXCrossEncoderWrapper, ds: Dataset):
    """
    Simulates retrieval evaluation.
    1. Builds a corpus of ALL unique Positives + Negatives in the dataset.
    2. For each Query, scores it against the ENTIRE corpus.
    3. Calculates Rank-based metrics.
    """
    # 1. Build Corpus (Deduplicated)
    # Map: DocHash -> Document Text
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

    # 4. Average Metrics
    total = len(ds)
    final_metrics = {k: v / total for k, v in metrics.items()}
    return final_metrics


def print_metrics_table(metrics: dict):
    table = Table('Metric', 'Score')
    for k, v in metrics.items():
        table.add_row(k, f'{v:.4f}')
    console.print(table)


def main():
    console.print('Loading model...')
    # Update directory to your Cross-Encoder output path
    wraps = ONNXCrossEncoderWrapper(
        model_dir='./ms-marco-minilm-l12-hindsight-reranker', model_name='model.onnx'
    )

    console.print('Loading datasets...')
    eval_ds = load_dataset(plb.Path('./data/eval_70.jsonl'))
    test_ds = load_dataset(plb.Path('./data/test_60.jsonl'))

    console.print('[bold]Evaluating on Eval Set (Ranking against full corpus)[/bold]...')
    eval_metrics = evaluate_reranking(wraps, eval_ds)
    print_metrics_table(eval_metrics)

    console.print('[bold]Evaluating on Test Set (Ranking against full corpus)[/bold]...')
    test_metrics = evaluate_reranking(wraps, test_ds)
    print_metrics_table(test_metrics)


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
