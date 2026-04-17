"""Unit tests for the LongMemEval dataset loaders.

These exercise the pure-Python parsing path only — no network, no database,
no LLM calls. A synthetic fixture mimicking the upstream JSON shape is
written to a tmp path and loaded through the public loader API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memex_eval.external.longmemeval_common import (
    DATASET_FILENAMES,
    LongMemEvalCategory,
    LongMemEvalQuestion,
    dataset_sha256,
    load_longmemeval_oracle,
    load_longmemeval_s,
)


def _make_turn(role: str, content: str, ts: str) -> dict:
    return {'role': role, 'content': content, 'timestamp': ts}


def _make_question(
    qid: str,
    category: str,
    answer: str | None = 'ground-truth',
) -> dict:
    return {
        'question_id': qid,
        'question_type': category,
        'question': f'What about {qid}?',
        'answer': answer,
        'haystack_sessions': [
            {
                'session_id': f'{qid}-s1',
                'session_date': '2024-01-01T08:00:00+00:00',
                'turns': [
                    _make_turn('user', 'Hi.', '2024-01-01T08:00:00+00:00'),
                    _make_turn('assistant', 'Hello!', '2024-01-01T08:00:10+00:00'),
                ],
            }
        ],
    }


@pytest.fixture()
def oracle_fixture_path(tmp_path: Path) -> Path:
    """Write a synthetic oracle JSON with one question per category + one _abs."""
    categories = [c.value for c in LongMemEvalCategory]
    records = [_make_question(f'q-{i:03d}', cat) for i, cat in enumerate(categories)]
    # Abstention variant
    records.append(_make_question('q-abs-001_abs', 'temporal-reasoning', answer=None))
    out = tmp_path / DATASET_FILENAMES['oracle']
    out.write_text(json.dumps(records))
    return out


def test_load_oracle_returns_questions(oracle_fixture_path: Path) -> None:
    questions = load_longmemeval_oracle(oracle_fixture_path, allow_unpinned=True)
    assert len(questions) == 7
    assert all(isinstance(q, LongMemEvalQuestion) for q in questions)
    categories = {q.category for q in questions}
    assert categories == {
        LongMemEvalCategory.SINGLE_SESSION_USER,
        LongMemEvalCategory.SINGLE_SESSION_ASSISTANT,
        LongMemEvalCategory.SINGLE_SESSION_PREFERENCE,
        LongMemEvalCategory.TEMPORAL_REASONING,
        LongMemEvalCategory.KNOWLEDGE_UPDATE,
        LongMemEvalCategory.MULTI_SESSION,
    }


def test_load_oracle_detects_abstention_suffix(oracle_fixture_path: Path) -> None:
    questions = load_longmemeval_oracle(oracle_fixture_path, allow_unpinned=True)
    abs_qs = [q for q in questions if q.is_abstention]
    assert len(abs_qs) == 1
    assert abs_qs[0].question_id.endswith('_abs')
    assert abs_qs[0].answer is None


def test_load_oracle_parses_turn_timestamps(oracle_fixture_path: Path) -> None:
    questions = load_longmemeval_oracle(oracle_fixture_path, allow_unpinned=True)
    q = questions[0]
    assert q.sessions, 'expected at least one session'
    turns = q.sessions[0].turns
    assert len(turns) == 2
    assert turns[0].timestamp == datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert turns[1].timestamp == datetime(2024, 1, 1, 8, 0, 10, tzinfo=timezone.utc)


def test_load_from_directory_path(tmp_path: Path, oracle_fixture_path: Path) -> None:
    # Passing the containing directory should locate the canonical filename.
    questions = load_longmemeval_oracle(oracle_fixture_path.parent, allow_unpinned=True)
    assert len(questions) == 7


def test_load_s_variant_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_longmemeval_s(tmp_path)


def test_dataset_sha256_is_stable(oracle_fixture_path: Path) -> None:
    h1 = dataset_sha256(oracle_fixture_path, 'oracle')
    h2 = dataset_sha256(oracle_fixture_path.parent, 'oracle')
    assert h1 == h2
    assert len(h1) == 64


def test_unknown_category_raises(tmp_path: Path) -> None:
    bad = [{'question_id': 'q-bad', 'question_type': 'nonsense', 'question': 'x'}]
    path = tmp_path / DATASET_FILENAMES['oracle']
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='Unknown LongMemEval category'):
        load_longmemeval_oracle(path, allow_unpinned=True)
