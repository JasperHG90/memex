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
# ]
# ///
"""
Fine-tunes the all-MiniLM-L6-v2 model using a small dataset to optimizes
for matching related facts, opinions, observations.
"""

import pathlib as plb
import sys
from typing import cast
import hashlib
import shutil
import typer

# Add the project root to sys.path to allow importing from memex
project_root = plb.Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from src.memex.memory.formatting import format_for_embedding

from torch.nn import Module
import torch
from torch.export import Dim
from transformers import AutoTokenizer
from sentence_transformers import SentenceTransformer, models
import jsonlines
from datasets import Dataset
from torchao.quantization import (
    quantize_,
    Int8DynamicActivationIntxWeightConfig as Int8DynamicActivationInt4WeightConfig,
)  #  Int8DynamicActivationInt4WeightConfig
from torchao.quantization.qat import QATConfig, QATStep
from sentence_transformers.evaluation import InformationRetrievalEvaluator
from sentence_transformers import SentenceTransformerTrainer, SentenceTransformerTrainingArguments
from sentence_transformers.losses import MultipleNegativesRankingLoss
from rich.table import Table
from rich.console import Console

MODEL_ID = 'sentence-transformers/all-MiniLM-L12-v2'
OUTPUT_DIR = plb.Path('./minilm-l12-v2-hindsight-embeddings')
DEVICE = 'cpu'

if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True)

console = Console()

app = typer.Typer(
    name='train',
    help='Train the all-MiniLM-L6-v2 model for matching related facts, opinions, observations.',
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=True,
    pretty_exceptions_short=True,
)


def fmt_doc(doc: dict[str, str]) -> str:
    """
    Format a document for embedding.
    """
    # Logic matched to src/memex/memory/formatting.py
    return format_for_embedding(
        text=doc.get('text', ''),
        fact_type=doc.get('type', 'Unknown'),
        context=doc.get('context', ''),
    )


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


def quantize_transformer(model: Module, config: Int8DynamicActivationInt4WeightConfig):
    console.print('Applying QAT Config (Prepare)...')
    quantize_(cast(Module, model), QATConfig(config, step=QATStep.PREPARE))


def construct_quantized_model(model_id: str, transformer: Module) -> SentenceTransformer:
    modelq8 = models.Transformer(model_id)
    modelq8.auto_model = transformer
    pooling_model = models.Pooling(
        modelq8.get_word_embedding_dimension(),
        pooling_mode_mean_tokens=True,
        pooling_mode_cls_token=False,
        pooling_mode_max_tokens=False,
    )
    norm = models.Normalize()
    modelq8 = SentenceTransformer(modules=[modelq8, pooling_model, norm])
    return modelq8


def get_evaluator(ds: Dataset):
    # 1. Prepare data structures for the evaluator
    queries = {str(i): cast(dict, row['query']) for i, row in enumerate(ds)}  # type: ignore
    corpus = {
        hashlib.md5(row['positive'].encode('utf-8')).hexdigest(): row['positive']  # type: ignore
        for row in ds
    }
    corpus.update(
        {hashlib.md5(row['negative'].encode('utf-8')).hexdigest(): row['negative'] for row in ds}  # type: ignore
    )

    # Map each query ID to the set of relevant document IDs (just the positive one)
    relevant_docs = {
        str(i): {hashlib.md5(row['positive'].encode('utf-8')).hexdigest()}  # type: ignore
        for i, row in enumerate(ds)
    }

    # 2. Create the Evaluator
    return InformationRetrievalEvaluator(
        queries,  # type: ignore
        corpus,
        relevant_docs,
        name='retrieval-eval',
    )


class OnnxWrapper(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        # We must return exactly the last_hidden_state tensor
        # so that your code's 'outputs[0]' is valid.
        out = self.model(input_ids=input_ids, attention_mask=attention_mask)
        last_hidden_state = out.last_hidden_state

        # Compute mean pooling
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
        sum_embeddings = torch.sum(last_hidden_state * input_mask_expanded, 1)
        sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
        sentence_embedding = sum_embeddings / sum_mask

        sentence_embedding = torch.nn.functional.normalize(sentence_embedding, p=2, dim=1)

        return sentence_embedding


def get_trainer(
    model: SentenceTransformer,
    train_ds: Dataset,
    eval_ds: Dataset,
    ir_evaluator: InformationRetrievalEvaluator,
    epochs: int = 16,
    lr: float = 3e-6,
) -> SentenceTransformerTrainer:
    loss = MultipleNegativesRankingLoss(model)
    args = SentenceTransformerTrainingArguments(
        output_dir='output/miniLM-memex-ft',
        num_train_epochs=epochs,
        per_device_train_batch_size=8,
        learning_rate=lr,
        warmup_ratio=0.5,
        logging_steps=train_ds.num_rows,
        report_to='none',
        metric_for_best_model='eval_retrieval-eval_cosine_mrr@10',
        lr_scheduler_type='reduce_lr_on_plateau',
        lr_scheduler_kwargs={'factor': 0.5, 'patience': 2},
        save_strategy='no',
        eval_strategy='steps',
        eval_steps=50,
        dataloader_pin_memory=False,
        use_cpu=True,
        load_best_model_at_end=False,
    )
    return SentenceTransformerTrainer(
        model=model,
        args=args,
        loss=loss,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        evaluator=ir_evaluator,
    )


def print_metrics(trainer: SentenceTransformerTrainer, ds: Dataset | None = None):
    table = Table('Metric', 'Score')
    baseline = trainer.evaluate() if not ds else trainer.evaluate(ds)
    for metric, score in baseline.items():
        table.add_row(metric, f'{score:.4f}')
    console.print(table)


def save_model_to_onnx(
    model: OnnxWrapper, output_path: plb.Path, config: Int8DynamicActivationInt4WeightConfig
):
    batch = Dim('batch', min=2, max=1024)
    seq = Dim('seq', min=2, max=512)

    # 2. Map the dimensions to the argument names in OnnxWrapper.forward
    dynamic_shapes = {'input_ids': {0: batch, 1: seq}, 'attention_mask': {0: batch, 1: seq}}

    # 3. Use a dummy input with size 2 to match the constraints
    dummy_input_ids = torch.ones((2, 128), dtype=torch.long)
    dummy_mask = torch.ones((2, 128), dtype=torch.long)

    onnx_path = OUTPUT_DIR / 'model.onnx'

    quantize_(cast(Module, model), QATConfig(config, step=QATStep.CONVERT))

    console.print(f'Exporting model to {output_path}...')
    torch.onnx.export(
        model,
        (dummy_input_ids, dummy_mask),
        str(onnx_path),
        input_names=['input_ids', 'attention_mask'],
        output_names=['sentence_embedding'],
        dynamic_shapes=dynamic_shapes,
        opset_version=18,
        dynamo=True,
    )


def main():
    console.print('Loading pre-trained model and tokenizer...')
    model = SentenceTransformer(MODEL_ID).to(device=DEVICE)
    base_config = Int8DynamicActivationInt4WeightConfig()
    console.print('Loading datasets...')
    train_ds = load_dataset(plb.Path('data/train_300.jsonl'))
    eval_ds = load_dataset(plb.Path('data/eval_50.jsonl'))
    transformer_model = cast(Module, model[0].auto_model)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    quantize_transformer(model, config=base_config)
    console.print('Constructing quantized model...')
    quantized_model = construct_quantized_model(MODEL_ID, transformer_model).to(device=DEVICE)
    console.print('Preparing evaluator...')
    evaluator = get_evaluator(eval_ds)
    console.print('Preparing trainer...')
    trainer = get_trainer(quantized_model, train_ds, eval_ds, evaluator, epochs=8, lr=1e-6)
    console.print('Baseline evaluation on eval set:')
    print_metrics(trainer)
    console.print('Starting training...')
    trainer.train()
    console.print('Converting model to ONNX format...')
    save_model_to_onnx(OnnxWrapper(transformer_model), OUTPUT_DIR, config=base_config)
    console.print('Save tokenizer and config')
    tokenizer.save_pretrained(OUTPUT_DIR)
    cast(Module, transformer_model).config.save_pretrained(OUTPUT_DIR)  # type: ignore
    console.print('Conversion complete.')


@app.command()
def run():
    """Train the all-MiniLM-L6-v2 model for observations, facts, opinions and experience matching."""
    main()


if __name__ == '__main__':
    app()
