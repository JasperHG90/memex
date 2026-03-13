"""Shared utilities for LoCoMo benchmarks.

Constants, dataset loading, and JSONL helpers used across
locomo.py, locomo_agent.py, and the three-phase pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger('memex_eval.locomo_common')

VAULT_NAME = 'locomo-bench'

CATEGORY_MAP: dict[int, str] = {
    1: 'single_hop',
    2: 'multi_hop',
    3: 'open_domain',
    4: 'temporal',
    5: 'adversarial',
}
CATEGORY_NAMES: dict[str, str] = {
    'single_hop': 'Single-Hop',
    'multi_hop': 'Multi-Hop',
    'open_domain': 'Open Domain',
    'temporal': 'Temporal',
    'adversarial': 'Adversarial',
}
QUESTION_TYPES = list(CATEGORY_NAMES.keys())

# Image-dependent questions excluded from scoring (content only visible in images).
EXCLUDED_QUESTION_IDS: dict[str, str] = {
    'q-018': 'Book title only visible in shared image (book cover)',
    'q-027': 'Precautionary sign content only visible in shared photo',
    'q-037': 'References a photo not available to the memory system',
}


def load_dataset(dataset_path: str) -> list[dict]:
    """Load the LoCoMo dataset from a directory or file path."""
    path = Path(dataset_path)
    for candidate in ['locomo.json', 'locomo10.json']:
        f = path / candidate
        if f.exists():
            return json.loads(f.read_text())
    if path.is_file() and path.suffix == '.json':
        return json.loads(path.read_text())
    raise FileNotFoundError(f'No LoCoMo dataset found in {path}.')


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL file."""
    p = Path(path)
    if not p.exists():
        return []
    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file."""
    with open(path, 'a') as f:
        f.write(json.dumps(record, default=str) + '\n')


def read_completed_ids(path: str | Path) -> set[str]:
    """Read IDs of already-completed records from a JSONL file."""
    records = read_jsonl(path)
    return {r['id'] for r in records if 'id' in r}
