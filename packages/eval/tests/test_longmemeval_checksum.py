"""Tests for the LongMemEval dataset-checksum pin enforcement (C2).

The loader must refuse to proceed when a variant has no pinned
SHA-256 unless ``allow_unpinned=True`` is explicitly set. A pinned
value that matches the file hash must pass silently; a mismatched
pin must raise — with no override available.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memex_eval.external import longmemeval_common as lmc_mod
from memex_eval.external.longmemeval_common import (
    DATASET_FILENAMES,
    DatasetChecksumMismatchError,
    DatasetChecksumUnpinnedError,
    _sha256_file,
    load_longmemeval_oracle,
)


def _write_min_dataset(tmp_path: Path) -> Path:
    out = tmp_path / DATASET_FILENAMES['oracle']
    out.write_text(
        json.dumps(
            [
                {
                    'question_id': 'q-001',
                    'question_type': 'single-session-user',
                    'question': 'Why?',
                    'answer': 'Because.',
                    'haystack_sessions': [],
                }
            ]
        )
    )
    return out


def test_unpinned_checksum_raises_without_override(tmp_path: Path) -> None:
    path = _write_min_dataset(tmp_path)
    # Ensure oracle pin is None for this test.
    with patch.dict(lmc_mod.DATASET_SHA256, {'oracle': None}, clear=False):
        with pytest.raises(DatasetChecksumUnpinnedError):
            load_longmemeval_oracle(path)


def test_unpinned_checksum_with_override_loads(tmp_path: Path) -> None:
    path = _write_min_dataset(tmp_path)
    with patch.dict(lmc_mod.DATASET_SHA256, {'oracle': None}, clear=False):
        questions = load_longmemeval_oracle(path, allow_unpinned=True)
    assert len(questions) == 1


def test_matching_pin_loads_without_override(tmp_path: Path) -> None:
    path = _write_min_dataset(tmp_path)
    actual = _sha256_file(path)
    with patch.dict(lmc_mod.DATASET_SHA256, {'oracle': actual}, clear=False):
        questions = load_longmemeval_oracle(path)
    assert len(questions) == 1


def test_mismatched_pin_raises(tmp_path: Path) -> None:
    path = _write_min_dataset(tmp_path)
    with patch.dict(lmc_mod.DATASET_SHA256, {'oracle': 'deadbeef' * 8}, clear=False):
        with pytest.raises(DatasetChecksumMismatchError):
            load_longmemeval_oracle(path)
        # Override does not help for a mismatch — integrity takes priority.
        with pytest.raises(DatasetChecksumMismatchError):
            load_longmemeval_oracle(path, allow_unpinned=True)
