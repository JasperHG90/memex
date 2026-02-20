# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "datasets>=2.14.0",
# ]
# ///

import json
import pathlib as plb
from datasets import load_dataset

# Output path matching your script
OUTPUT_FILE = plb.Path('data/train_300.jsonl')
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

print(f'Streaming CoNLL-2003 dataset to {OUTPUT_FILE}...')

# 1. Stream the dataset (no massive download)
# We take 300 examples from the 'train' split
dataset = load_dataset('ag_news', split='train', streaming=True).take(300)

if __name__ == '__main__':
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for row in dataset:
            # 2. Reconstruct the sentence
            # CoNLL provides a list of tokens: ["EU", "rejects", "German", "call", ...]
            # We join them with spaces to create a raw string for calibration.
            # (Simple space joining is sufficient for calibration statistics)
            text = row['text']

            # 3. Write to JSONL
            # Your script looks for a "text" key
            f.write(json.dumps({'text': text}) + '\n')

    print('Done! You can now run your quantization script.')
