import logging
import pathlib as plb
import json
import string

import httpx
import numpy as np
import onnxruntime as ort
from platformdirs import user_cache_dir
from tokenizers import Tokenizer

from async_lru import alru_cache
from memex_core.memory.models.base import ModelDownloader, options

logger = logging.getLogger('memex.core.memory.models.ner')


@alru_cache(maxsize=1)
async def get_ner_model() -> 'FastNERModel':
    """Get the NER model

    Args:
        model_dir (str | plb.Path | None, optional): Location of the model directory. Defaults to None.
        model_name (str, optional): Name of the model file. Defaults to 'model.onnx'.

    Returns:
        FastNERModel: NER model instance.
    """
    repo_id = 'JasperHG90/distilbert-hindsight-ner'
    path = plb.Path(user_cache_dir('memex')) / repo_id.replace('/', '__') / 'main'

    if not path.exists():
        logger.warning(f'NER model not found at {path}. Downloading from Hugging Face Hub...')
        downloader = ModelDownloader(repo_id=repo_id)
        await downloader.download_async(client=httpx.AsyncClient(), force=False)

    return FastNERModel(model_dir=str(path), model_name='model.onnx')


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


class FastNERModel:
    def __init__(self, model_dir: str | plb.Path, model_name: str = 'model.onnx'):
        self.model_path = plb.Path(model_dir)
        self.tokenizer: Tokenizer = Tokenizer.from_file(str(self.model_path / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=128)
        self.tokenizer.enable_padding(direction='right', pad_id=0, pad_token='[PAD]', length=128)
        self.id2label = {
            int(k): v for k, v in json.loads((self.model_path / 'config.json').read_text()).items()
        }

        self.session = ort.InferenceSession(
            str(self.model_path / model_name),
            providers=['CPUExecutionProvider'],
            sess_options=options,
        )

    def predict(self, text: str):
        # 1. Tokenize
        # This returns an 'Encoding' object containing ids, offsets, word_ids, etc.
        encoding = self.tokenizer.encode(text)

        # 2. Prepare Inputs for ONNX
        # Convert lists to numpy and add Batch Dimension [1, 128]
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask], dtype=np.int64)

        ort_inputs = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
        }

        # 3. Inference
        logits = np.array(self.session.run(None, ort_inputs))[0]
        predictions = np.argmax(logits, axis=2)[0]

        # 4. Extraction
        # 'encoding' object has .word_ids and .offsets properties directly
        word_ids = encoding.word_ids
        offsets = encoding.offsets

        raw_entities = []
        for idx, word_idx in enumerate(word_ids):
            if word_idx is None:
                continue  # Skip CLS/SEP/PAD

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

        # 5. Merge
        merged = merge_entities(raw_entities)

        # 6. Sanitize
        final = []
        for m in merged:
            clean_word = sanitize_entity(m['word'])
            if len(clean_word) > 0:
                m['word'] = clean_word
                final.append(m)

        return final
