# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "datasets>=4.4.2",
#     "jsonlines>=4.0.0",
#     "numpy>=2.4.0",
#     "onnxruntime>=1.23.2",
#     "rich>=14.2.0",
#     "sentence-transformers>=5.2.0",
#     "tokenizers>=0.22.1",
#     "typer>=0.21.0",
# ]
# ///
"""
Evaluates the all-MiniLM-L12-v2 model using a small dataset to optimizes
for observations, facts, opinions and experience matching.
"""

import pathlib as plb
from typing import cast
import hashlib

import jsonlines
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from rich.table import Table
from rich.console import Console
import typer
from datasets import Dataset
from sentence_transformers.evaluation import InformationRetrievalEvaluator
from sentence_transformers import util


console = Console()

app = typer.Typer(
    name='eval',
    help='Evaluate the all-MiniLM-L12-v2 model using ONNX runtime.',
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=True,
    pretty_exceptions_short=True,
)


def fmt_doc(doc: dict[str, str]) -> str:
    # 1. Extract Type (World, Experience, Observation, Opinion)
    # This is the primary signal for Epistemic Separation.
    mem_type = doc.get('type', 'Unknown')

    # 2. Extract Context (Optional but highly recommended)
    # Adds specific semantic flavor (e.g., "Instruction" vs "History")
    context = doc.get('context', '')

    # 3. The Narrative Text
    text = doc.get('text', '')

    # Format: "Type (Context): Text"
    # Example: "Experience (Maintenance): I changed the oil."
    if context:
        return f'{mem_type} ({context}): {text}'
    else:
        return f'{mem_type}: {text}'


def load_dataset(file_path: plb.Path):
    console.print(f'Loading dataset from {str(file_path)}')
    with jsonlines.open(file_path, 'r') as reader:
        data = list(reader)
    data_ = [
        dict(
            row,
            positive=fmt_doc(row['positive']),
            negative=fmt_doc(row['negative']),
            query=row['query'],
        )
        for row in data
    ]
    return Dataset.from_list(data_).shuffle()


def get_evaluator(ds: Dataset):
    # 1. Prepare data structures for the evaluator
    queries = {str(i): cast(dict, row['query']) for i, row in enumerate(ds)}
    corpus = {
        hashlib.md5(row['positive'].encode('utf-8')).hexdigest(): row['positive'] for row in ds
    }
    corpus.update(
        {hashlib.md5(row['negative'].encode('utf-8')).hexdigest(): row['negative'] for row in ds}
    )

    # Map each query ID to the set of relevant document IDs (just the positive one)
    relevant_docs = {
        str(i): {hashlib.md5(row['positive'].encode('utf-8')).hexdigest()}
        for i, row in enumerate(ds)
    }

    # 2. Create the Evaluator
    return InformationRetrievalEvaluator(
        queries,
        corpus,
        relevant_docs,
        name='skill-retrieval-eval',
    )


def print_metrics(evaluator: InformationRetrievalEvaluator, model: 'ONNXTransformerWrapper'):
    table = Table('Metric', 'Score')
    baseline = evaluator(model)
    for metric, score in baseline.items():
        table.add_row(metric, f'{score:.4f}')
    console.print(table)


class ONNXTransformerWrapper:
    def __init__(self, model_dir, model_name='model.onnx'):
        # Load tokenizer
        self.tokenizer = Tokenizer.from_file(str(plb.Path(model_dir) / 'tokenizer.json'))
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        self.tokenizer.enable_truncation(max_length=512)

        self.similarity_fn_name = 'cosine'
        self.similarity = util.cos_sim

        # Load ONNX session
        self.session = ort.InferenceSession(
            str(plb.Path(model_dir) / model_name), providers=['CPUExecutionProvider']
        )

        class MockModelCardData:
            def set_evaluation_metrics(self, *args, **kwargs):
                pass

        self.model_card_data = MockModelCardData()

    def encode_query(self, sentences, **kwargs):
        return self.encode(sentences, **kwargs)

    def encode_document(self, sentences, **kwargs):
        return self.encode(sentences, **kwargs)

    def encode(
        self, sentences, batch_size=32, show_progress_bar=False, convert_to_tensor=False, **kwargs
    ):
        all_embeddings = []

        # Process in batches
        for i in range(0, len(sentences), batch_size):
            batch_texts = sentences[i : i + batch_size]

            # Tokenize
            encodings = self.tokenizer.encode_batch(batch_texts)

            # Prepare inputs for ONNX (convert lists to numpy arrays)
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)

            # Most ONNX models exported from HF require: input_ids, attention_mask, token_type_ids
            onnx_inputs = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
            }

            # If your specific model requires token_type_ids:
            if 'token_type_ids' in [inputs.name for inputs in self.session.get_inputs()]:
                onnx_inputs['token_type_ids'] = np.array(
                    [e.type_ids for e in encodings], dtype=np.int64
                )

            # Run Inference
            # output[0] is usually the 'last_hidden_state' (batch, seq_len, hidden_dim)
            outputs = self.session.run(None, onnx_inputs)
            last_hidden_state = outputs[0]

            # Perform Mean Pooling
            # embeddings = self.mean_pooling(last_hidden_state, attention_mask)
            all_embeddings.append(last_hidden_state)

        final_embeddings = np.vstack(all_embeddings)

        # Sentence-Transformers evaluator handles numpy arrays fine
        return final_embeddings

    def mean_pooling(self, last_hidden_state, attention_mask):
        # last_hidden_state: [batch, seq_len, hidden_dim]
        # attention_mask: [batch, seq_len]

        input_mask_expanded = np.expand_dims(attention_mask, -1).astype(float)
        sum_embeddings = np.sum(last_hidden_state * input_mask_expanded, axis=1)
        sum_mask = np.clip(input_mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)

        return sum_embeddings / sum_mask


def main():
    console.print('Loading model ..')
    wraps = ONNXTransformerWrapper(
        model_dir='minilm-l12-v2-hindsight-embeddings', model_name='model.onnx'
    )
    console.print('Loading datasets...')
    eval_ds = load_dataset(plb.Path('data/eval_50.jsonl'))
    test_ds = load_dataset(plb.Path('data/test_50.jsonl'))
    console.print('Evaluating on eval set...')
    print_metrics(evaluator=get_evaluator(eval_ds), model=wraps)
    console.print('Evaluating on test set...')
    print_metrics(evaluator=get_evaluator(test_ds), model=wraps)


@app.command()
def run():
    """Evaluate the all-MiniLM-L12-v2 model for observations, facts, opinions and experience matching."""
    main()


if __name__ == '__main__':
    app()
