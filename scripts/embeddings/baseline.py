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
#     "onnx>=1.14.0",
#     "onnxruntime>=1.16.0",
# ]
# ///
import pathlib as plb
import shutil
import torch
import torch.nn as nn
from transformers import AutoTokenizer, AutoModel
from rich.console import Console
import typer
import onnx

MODEL_ID = 'sentence-transformers/all-MiniLM-L12-v2'
OUTPUT_DIR = plb.Path('./minilm-l12-v2-hindsight-embeddings-fp32')
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True)

console = Console()
app = typer.Typer(name='export-embedder', no_args_is_help=True)


class OnnxEmbedderWrapper(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, input_ids, attention_mask):
        out = self.model(input_ids=input_ids, attention_mask=attention_mask)
        last_hidden_state = out.last_hidden_state

        # --- Manual Mean Pooling via BMM (Batch Matrix Multiplication) ---
        # We replace torch.sum with BMM to prevent ONNX Opset version conflicts regarding
        # whether 'axes' are attributes or inputs. BMM is universally supported.

        # 1. Expand Mask: (Batch, Seq) -> (Batch, Seq, 1)
        mask_expanded = attention_mask.unsqueeze(-1).float()

        # 2. Weighted Sum: (Batch, Hidden, Seq) @ (Batch, Seq, 1) -> (Batch, Hidden, 1)
        # effectively summing (Hidden * Mask) over Seq dimension
        sum_embeddings = torch.bmm(last_hidden_state.transpose(1, 2), mask_expanded).squeeze(-1)

        # 3. Sum Mask: (Batch, 1, Seq) @ (Batch, Seq, 1) -> (Batch, 1, 1)
        # effectively summing the mask
        ones = torch.ones_like(mask_expanded)
        sum_mask = torch.bmm(ones.transpose(1, 2), mask_expanded).squeeze(-1)

        # Clamp to avoid division by zero
        sum_mask = torch.clamp(sum_mask, min=1e-9)

        # 4. Divide
        sentence_embeddings = sum_embeddings / sum_mask

        # 5. L2 Norm
        sentence_embeddings = torch.nn.functional.normalize(sentence_embeddings, p=2, dim=1)

        return sentence_embeddings


def main():
    console.print(f'Loading model: {MODEL_ID}')
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID)

    console.print('Exporting to ONNX (FP32)...')
    model.to('cpu')
    model.eval()

    wrapper = OnnxEmbedderWrapper(model)

    dummy_inputs = tokenizer(
        ['Test sentence A', 'Test sentence B'],
        return_tensors='pt',
        padding='max_length',
        max_length=128,
    )

    input_ids = dummy_inputs['input_ids']
    attention_mask = dummy_inputs['attention_mask']

    onnx_path = OUTPUT_DIR / 'model.onnx'

    # OPSET 13 + NO CONSTANT FOLDING
    # This combination is the most "Raw" export that prevents the optimizer
    # from trying to be clever and failing on the version conversion.
    torch.onnx.export(
        wrapper,
        (input_ids, attention_mask),
        str(onnx_path),
        input_names=['input_ids', 'attention_mask'],
        output_names=['sentence_embedding'],
        dynamic_axes={
            'input_ids': {0: 'batch_size', 1: 'seq_len'},
            'attention_mask': {0: 'batch_size', 1: 'seq_len'},
            'sentence_embedding': {0: 'batch_size'},
        },
        opset_version=13,
        do_constant_folding=False,  # Critical to stop the converter from eating the BMM inputs
    )

    # 4. Save Configs
    tokenizer.save_pretrained(OUTPUT_DIR)
    model.config.save_pretrained(OUTPUT_DIR)

    # 5. VERIFICATION STEP
    # We explicitly verify the model to prove it works despite any stderr noise.
    try:
        onnx_model = onnx.load(str(onnx_path))
        onnx.checker.check_model(onnx_model)
        console.print(
            '[bold green]Verification Successful: Model is valid ONNX Opset 13.[/bold green]'
        )
        console.print(f'[bold green]Model saved to {OUTPUT_DIR}[/bold green]')
    except Exception as e:
        console.print(f'[bold red]Verification Failed: {e}[/bold red]')
        exit(1)


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
