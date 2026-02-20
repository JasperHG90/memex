# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "datasets>=2.14.0",
#     "jsonlines>=4.0.0",
#     "numpy>=1.26.0",
#     "onnxruntime>=1.17.0",
#     "rich>=13.0.0",
#     "transformers>=4.36.0",
#     "typer>=0.9.0",
# ]
# ///
import pathlib as plb
import json
import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer
from rich.table import Table
from rich.console import Console
from rich import box
import typer
import string
import jsonlines

console = Console()
app = typer.Typer(name='eval-ner', no_args_is_help=True)


def sanitize_entity(text: str) -> str:
    """
    Cleans artifacts like leading/trailing punctuation.
    Example: ". Peterson" -> "Peterson"
    Example: "Amazon," -> "Amazon"
    """
    return text.strip(string.punctuation + string.whitespace)


def merge_entities(entities):
    """
    Merges adjacent entities and cleans WordPiece artifacts ('##').
    """
    if not entities:
        return []

    # 1. Normalize and Sort
    normalized = []
    for e in entities:
        item = e.copy()
        # Ensure type exists
        if 'type' not in item:
            raw = item.get('entity', item.get('label', 'O'))
            item['type'] = raw.split('-')[-1] if '-' in raw else raw
        normalized.append(item)

    entities = sorted(normalized, key=lambda x: x['start'])

    merged = []

    # Initialize current, ensuring we clean any existing '##' from the start
    current = entities[0]
    current['word'] = current['word'].replace('##', '')

    for next_ent in entities[1:]:
        # Clean the next word before checking logic
        next_word_clean = next_ent['word'].replace('##', '')

        # Calculate Gap
        current_end = int(current['end'])
        next_start = int(next_ent['start'])
        gap = next_start - current_end

        # types_match = current['type'] == next_ent['type']

        # Logic: Merge if types match AND (gap is small OR it was a subword)
        # Note: If next_word started with '##', it implies gap=0 physically
        is_subword = next_ent['word'].startswith('##')
        is_adjacent = gap <= 1

        # if types_match and (is_adjacent or is_subword):
        if is_adjacent or is_subword:
            # MERGE
            # Use space only if there was a real gap and it wasn't a subword
            separator = ' ' if (gap == 1 and not is_subword) else ''

            current['word'] = current['word'] + separator + next_word_clean
            current['end'] = next_ent['end']

            if 'score' in current:
                current['score'] = (float(current['score']) + float(next_ent['score'])) / 2
        else:
            # NO MERGE
            merged.append(current)
            current = next_ent
            current['word'] = next_word_clean  # Ensure new start is clean

    merged.append(current)

    final = []
    for m in merged:
        clean_word = sanitize_entity(m['word'])
        if len(clean_word) > 0:  # Filter out entities that became empty (e.g. just ".")
            m['word'] = clean_word
            final.append(m)

    return final


class NEROnnxRunner:
    def __init__(self, model_dir: plb.Path):
        self.model_dir = model_dir
        with open(model_dir / 'config.json', 'r') as f:
            self.id2label = {int(k): v for k, v in json.load(f).items()}
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir), use_fast=True)
        self.session = ort.InferenceSession(
            str(model_dir / 'model.onnx'), providers=['CPUExecutionProvider']
        )

    def predict(self, text: str):
        # 1. Tokenize
        encoding = self.tokenizer(
            text,
            return_tensors='np',
            padding='max_length',
            truncation=True,
            max_length=128,
            return_offsets_mapping=True,
        )

        # 2. Inference
        ort_inputs = {
            'input_ids': encoding['input_ids'].astype(np.int64),
            'attention_mask': encoding['attention_mask'].astype(np.int64),
        }
        logits = self.session.run(None, ort_inputs)[0]
        predictions = np.argmax(logits, axis=2)[0]

        # 3. Extract Raw Entities
        batch_encoding = self.tokenizer(
            text, truncation=True, max_length=128, return_offsets_mapping=True
        )
        word_ids = batch_encoding.word_ids()
        offsets = batch_encoding['offset_mapping']

        raw_entities = []
        for idx, word_idx in enumerate(word_ids):
            if word_idx is None:
                continue

            label_id = predictions[idx]
            label = self.id2label.get(label_id, 'O')

            if label != 'O':
                start, end = offsets[idx]
                if start == end:
                    continue

                raw_entities.append(
                    {
                        'word': text[start:end],
                        'type': label.split('-')[-1],
                        'start': start,
                        'end': end,
                        'score': 1.0,
                    }
                )

        # 4. Merge
        return merge_entities(raw_entities)


def compare(predicted, expected):
    """
    Compares entities purely by text (Ignoring Type).
    """
    # Create sets of just the strings
    pred_set = set(e['word'].strip() for e in predicted)
    exp_set = set(e['word'].strip() for e in expected)

    matches = pred_set.intersection(exp_set)
    missed = exp_set - pred_set
    extra = pred_set - exp_set

    return matches, missed, extra


def process_file(runner: NEROnnxRunner, file_path: plb.Path):
    table = Table(title='NER Evaluation (Recall Focused)', box=box.ROUNDED, padding=(0, 1))
    table.add_column('Stat', justify='center', width=4)
    table.add_column('Original Text', style='cyan', no_wrap=False)
    table.add_column('Expected', style='dim')
    table.add_column('Predicted', no_wrap=False)

    total_rows = 0
    perfect_rows = 0

    with jsonlines.open(file_path) as reader:
        for line in reader:
            text = line.get('text', '')
            expected = line.get('expected', [])
            if not text:
                continue

            predicted = runner.predict(text)
            matches, missed, extra = compare(predicted, expected)

            # Formatting
            exp_str = ', '.join([f'{e["word"]}' for e in expected])

            pred_parts = []
            for m in sorted(matches):
                pred_parts.append(f'[green]{m}[/green]')
            for e in sorted(extra):
                # Mark extras as dim (found but not requested) instead of strike-through error
                pred_parts.append(f'[dim]{e}[/dim]')
            for m in sorted(missed):
                pred_parts.append(f'[bold red]MISSING: {m}[/bold red]')

            pred_str = ', '.join(pred_parts) if pred_parts else '[dim]-[/dim]'

            # SUCCESS CONDITION: NO MISSING ENTITIES
            # We allow extra entities (hallucinations) without failing the row
            is_success = len(missed) == 0
            status_icon = '✅' if is_success else '❌'

            if is_success:
                perfect_rows += 1
            total_rows += 1

            table.add_row(status_icon, text, exp_str, pred_str)

    console.print(table)

    # Stats
    recall = (perfect_rows / total_rows) * 100 if total_rows > 0 else 0
    color = 'green' if recall > 80 else 'yellow' if recall > 50 else 'red'

    console.print(f'\n[bold]Results:[/bold] {perfect_rows}/{total_rows} Rows with Full Recall')
    console.print(f'Row Recall: [{color}]{recall:.2f}%[/{color}]')


def main():
    model_paths = ['./distilbert-hindsight-ner', './distilbert-ner-onnx']
    model_dir = next((plb.Path(p) for p in model_paths if plb.Path(p).exists()), None)
    if not model_dir:
        console.print('[red]No model found.[/red]')
        return
    runner = NEROnnxRunner(model_dir)
    process_file(runner, plb.Path('data/eval_ner.jsonl'))


@app.command()
def run():
    main()


if __name__ == '__main__':
    app()
