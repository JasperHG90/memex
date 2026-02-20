# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "accelerate>=0.26.0",
#     "datasets>=2.14.0",
#     "rich>=13.0.0",
#     "torchao>=0.3.0",
#     "typer>=0.9.0",
#     "transformers>=4.36.0",
#     "torch>=2.4.0",
#     "onnxscript>=0.1.0"
# ]
# ///
"""
NER Quantization using TorchAO QAT (Same approach as embedding script).
Uses Int4 Weights + Int8 Dynamic Activations.
"""

import pathlib as plb
import shutil
import typer
import torch
import json
from torch.nn import Module
from torch.export import Dim
from transformers import AutoTokenizer, AutoModelForTokenClassification
from datasets import load_dataset
from rich.console import Console
from rich.progress import track

# TorchAO imports matching your embedding script approach
from torchao.quantization import (
    quantize_,
    Int8DynamicActivationInt4WeightConfig,  # This config works with QAT wrapper
)
from torchao.quantization.qat import QATConfig, QATStep

MODEL_ID = 'dslim/distilbert-NER'
OUTPUT_DIR = plb.Path('./distilbert-hindsight-ner')
DEVICE = 'cpu'  # QAT preparation often safer on CPU for export consistency, can use CUDA if needed

if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True)

console = Console()

app = typer.Typer(name='ner-qat')


def load_calibration_data(limit: int = 300):
    """
    Load real text data for calibration.
    Using ag_news to avoid legacy script issues with CoNLL.
    """
    console.print('Loading calibration data (AG News)...')
    ds = load_dataset('ag_news', split='train', streaming=True).take(limit)
    texts = [row['text'] for row in ds]
    return texts


def prepare_qat(model: Module, config):
    """
    Step 1: Insert QAT observers.
    This prepares the model to 'learn' the quantization ranges.
    """
    console.print('Applying QAT Config (Step: PREPARE)...')
    # We cast to Module to satisfy type checkers, though it already is one
    quantize_(model, QATConfig(config, step=QATStep.PREPARE))


def calibrate_one_epoch(model: Module, texts: list[str], tokenizer, batch_size: int = 8):
    """
    The 'Training' Loop.
    Since we have no labels, we run a forward pass to update QAT observers.
    """
    console.print('Running calibration loop...')
    model.train()  # Must be in train mode for QAT observers to update

    # We use no_grad because we are not updating weights via SGD,
    # we are only updating the QAT observer statistics.
    with torch.no_grad():
        for i in track(range(0, len(texts), batch_size), description='Calibrating...'):
            batch_texts = texts[i : i + batch_size]
            if not batch_texts:
                continue

            inputs = tokenizer(
                batch_texts,
                padding='max_length',
                truncation=True,
                max_length=128,
                return_tensors='pt',
            ).to(DEVICE)

            model(**inputs)


class NEROnnxWrapper(torch.nn.Module):
    """
    Wraps the model to ensure strict output format for ONNX.
    """

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        # We strictly want logits
        return self.model(input_ids=input_ids, attention_mask=attention_mask).logits


def save_model_to_onnx(model: Module, tokenizer, output_dir: plb.Path, config):
    """
    Step 3: Convert QAT observers to actual fake-quantized weights and Export.
    """
    onnx_path = output_dir / 'model.onnx'

    console.print('Converting QAT observers (Step: CONVERT)...')
    quantize_(model, QATConfig(config, step=QATStep.CONVERT))
    model.eval()

    # Wrap for export
    wrapper = NEROnnxWrapper(model)

    # Dynamic shapes
    batch = Dim('batch', min=1, max=1024)
    seq = Dim('seq', min=1, max=512)
    dynamic_shapes = {'input_ids': {0: batch, 1: seq}, 'attention_mask': {0: batch, 1: seq}}

    dummy_input = tokenizer('Calibration is complete.', return_tensors='pt')
    dummy_ids = dummy_input['input_ids'].to(DEVICE)
    dummy_mask = dummy_input['attention_mask'].to(DEVICE)

    console.print(f'Exporting to {onnx_path} using TorchDynamo...')

    # dynamo=True is CRITICAL for torchao quantized models
    torch.onnx.export(
        wrapper,
        (dummy_ids, dummy_mask),
        str(onnx_path),
        input_names=['input_ids', 'attention_mask'],
        output_names=['logits'],
        dynamic_shapes=dynamic_shapes,
        opset_version=18,
        dynamo=True,
        fallback=True,  # Helps with some ops that might not perfectly map
    )


def main():
    console.print(f'Loading model: {MODEL_ID}...')
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForTokenClassification.from_pretrained(MODEL_ID).to(DEVICE)

    # Use the config from your embedding script (Int4 Weight / Int8 Dynamic Act)
    # This config is supported by the QAT wrapper.
    qat_config = Int8DynamicActivationInt4WeightConfig()

    # 1. Prepare
    prepare_qat(model, qat_config)

    # 2. Calibrate
    calibration_texts = load_calibration_data(limit=300)
    calibrate_one_epoch(model, calibration_texts, tokenizer)

    # 3. Export
    model.eval()
    save_model_to_onnx(model, tokenizer, OUTPUT_DIR, qat_config)

    # Save artifacts
    tokenizer.save_pretrained(OUTPUT_DIR)
    with open(OUTPUT_DIR / 'config.json', 'w') as f:
        json.dump(model.config.id2label, f, indent=2)

    console.print('[bold green]Process Complete.[/bold green]')


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
