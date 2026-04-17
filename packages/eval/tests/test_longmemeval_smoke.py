"""Smoke test for the LongMemEval pipeline.

Exercises judge + report end-to-end against a synthetic dataset using the
cached-judge fixture so the test runs offline. Validates pipeline shape
(file layout, JSON schema), not answer quality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unittest.mock import patch

from memex_eval.external.longmemeval_common import (
    DATASET_FILENAMES,
    LongMemEvalCategory,
    LongMemEvalJudgment,
    LongMemEvalReport,
    append_jsonl,
)
from memex_eval.external.longmemeval_judge import judge_hypotheses
from memex_eval.external.longmemeval_report import aggregate_for_test, generate_report

FIXTURE_CACHE = Path(__file__).parent / 'fixtures' / 'longmemeval_smoke_cache.json'


def _write_synthetic_dataset(tmp_path: Path) -> Path:
    """Write a synthetic dataset matching the smoke cache question_ids."""
    records = [
        {
            'question_id': 'smoke-q-001',
            'question_type': 'single-session-user',
            'question': 'What did the user say first?',
            'answer': 'Hello.',
            'haystack_sessions': [],
        },
        {
            'question_id': 'smoke-q-002',
            'question_type': 'multi-session',
            'question': 'When was the meeting scheduled?',
            'answer': 'Tuesday at 3pm.',
            'haystack_sessions': [],
        },
        {
            'question_id': 'smoke-q-003',
            'question_type': 'temporal-reasoning',
            'question': 'How long ago was that?',
            'answer': 'Two weeks ago.',
            'haystack_sessions': [],
        },
        {
            'question_id': 'smoke-q-004_abs',
            'question_type': 'knowledge-update',
            'question': "What is the user's middle name?",
            'answer': None,
            'haystack_sessions': [],
        },
        {
            'question_id': 'smoke-q-005',
            'question_type': 'single-session-preference',
            'question': 'What does the user prefer?',
            'answer': 'Tea over coffee.',
            'haystack_sessions': [],
        },
    ]
    p = tmp_path / DATASET_FILENAMES['oracle']
    p.write_text(json.dumps(records))
    return p


def _write_hypotheses(path: Path) -> None:
    rows = [
        {'question_id': 'smoke-q-001', 'hypothesis': 'Hello.'},
        {'question_id': 'smoke-q-002', 'hypothesis': 'Tuesday at 3pm.'},
        {'question_id': 'smoke-q-003', 'hypothesis': 'A while back.'},
        {
            'question_id': 'smoke-q-004_abs',
            'hypothesis': 'I do not know based on the available memory.',
        },
        {'question_id': 'smoke-q-005', 'hypothesis': 'Tea over coffee.'},
    ]
    for r in rows:
        append_jsonl(path, r)


@pytest.mark.asyncio
async def test_smoke_judge_then_report(tmp_path: Path) -> None:
    dataset = _write_synthetic_dataset(tmp_path)
    hypotheses = tmp_path / 'hypotheses.jsonl'
    judgments = tmp_path / 'judgments.jsonl'
    _write_hypotheses(hypotheses)

    written = await judge_hypotheses(
        dataset_path=str(dataset),
        variant='oracle',
        hypotheses_path=str(hypotheses),
        output_path=str(judgments),
        cache_path=str(FIXTURE_CACHE),
        allow_unpinned_checksum=True,
    )
    assert written == 5

    # Validate the JSONL shape.
    records = [json.loads(line) for line in judgments.read_text().splitlines() if line]
    assert len(records) == 5
    parsed = [LongMemEvalJudgment(**r) for r in records]
    assert sum(1 for p in parsed if p.correct) == 4

    # Generate and validate the report.
    out_dir = tmp_path / 'report'
    md_path = generate_report(
        judgments_path=str(judgments),
        output_dir=str(out_dir),
        run_id='smoke',
        variant='oracle',
        dataset_path=str(dataset),
    )
    assert Path(md_path).exists()
    results = json.loads((out_dir / 'results.json').read_text())
    report = LongMemEvalReport(**results)
    assert report.total_questions == 5
    assert 0.0 <= report.overall_accuracy <= 1.0
    assert report.overall_accuracy == 0.8
    assert report.dataset_sha256 != ''


def _write_dataset_with_live_session(tmp_path: Path) -> Path:
    """Synthetic dataset where smoke-q-002 carries a real session so the
    ingest/answer formatting paths are exercised end-to-end, not just the
    report glue."""
    records = [
        {
            'question_id': 'smoke-q-002',
            'question_type': 'multi-session',
            'question': 'When was the meeting scheduled?',
            'answer': 'Tuesday at 3pm.',
            'haystack_sessions': [
                {
                    'session_id': 'sess-1',
                    'session_date': '2024-02-01T10:00:00+00:00',
                    'turns': [
                        {
                            'role': 'user',
                            'content': 'Schedule the meeting for Tuesday at 3pm.',
                            'timestamp': '2024-02-01T10:00:00+00:00',
                        },
                        {
                            'role': 'assistant',
                            'content': 'Meeting scheduled for Tuesday 3pm.',
                            'timestamp': '2024-02-01T10:00:05+00:00',
                        },
                    ],
                }
            ],
        },
    ]
    p = tmp_path / DATASET_FILENAMES['oracle']
    p.write_text(json.dumps(records))
    return p


class _FlipJudge:
    """Test double for ``Judge`` that returns a verdict driven by the
    hypothesis content — NOT the question_id — so the discriminator test
    observes different metrics when a hypothesis changes.
    """

    class _LM:
        model = 'flipjudge-lm'

    def __init__(self, model: str | None = None) -> None:
        self.lm = self._LM()

    def judge_correctness(self, question: str, expected: str, response: str) -> tuple[bool, str]:
        # Trivial content-sensitive rule: mark correct iff the expected
        # answer appears verbatim in the response (case-insensitive).
        ok = expected.lower().strip('.') in response.lower()
        return ok, f'flipjudge: expected-in-response={ok}'

    def judge_abstention_correctness(self, question: str, response: str) -> tuple[bool, str]:
        # Correct iff response explicitly abstains.
        ok = 'do not know' in response.lower() or "don't know" in response.lower()
        return ok, f'flipjudge abstention: {ok}'

    def classify_abstention(self, response: str) -> tuple[bool, str]:
        ok = 'do not know' in response.lower() or "don't know" in response.lower()
        return ok, 'flipjudge classifier'


@pytest.mark.asyncio
async def test_smoke_discriminator_flipping_hypothesis_changes_metrics(tmp_path: Path) -> None:
    """Drive the judge against a ``_FlipJudge`` LM (NOT the cache) and
    verify that flipping one hypothesis flips the overall accuracy. The
    pre-existing smoke test could not catch this because the fixture
    cache keyed on question_id, not hypothesis content.
    """
    dataset = _write_synthetic_dataset(tmp_path)

    # Round 1: hypotheses all match expected answers verbatim.
    hypotheses_a = tmp_path / 'hypotheses-a.jsonl'
    judgments_a = tmp_path / 'judgments-a.jsonl'
    for row in [
        {'question_id': 'smoke-q-001', 'hypothesis': 'Hello.'},
        {'question_id': 'smoke-q-002', 'hypothesis': 'Tuesday at 3pm.'},
        {'question_id': 'smoke-q-003', 'hypothesis': 'Two weeks ago.'},
        {
            'question_id': 'smoke-q-004_abs',
            'hypothesis': 'I do not know based on the available memory.',
        },
        {'question_id': 'smoke-q-005', 'hypothesis': 'Tea over coffee.'},
    ]:
        append_jsonl(hypotheses_a, row)

    # Round 2: hypothesis for smoke-q-003 is flipped to something wrong.
    hypotheses_b = tmp_path / 'hypotheses-b.jsonl'
    judgments_b = tmp_path / 'judgments-b.jsonl'
    for row in [
        {'question_id': 'smoke-q-001', 'hypothesis': 'Hello.'},
        {'question_id': 'smoke-q-002', 'hypothesis': 'Tuesday at 3pm.'},
        {'question_id': 'smoke-q-003', 'hypothesis': 'Fish on Fridays.'},
        {
            'question_id': 'smoke-q-004_abs',
            'hypothesis': 'I do not know based on the available memory.',
        },
        {'question_id': 'smoke-q-005', 'hypothesis': 'Tea over coffee.'},
    ]:
        append_jsonl(hypotheses_b, row)

    # Patch the Judge class used inside longmemeval_judge (not the cache).
    with patch('memex_eval.external.longmemeval_judge.Judge', _FlipJudge):
        await judge_hypotheses(
            dataset_path=str(dataset),
            variant='oracle',
            hypotheses_path=str(hypotheses_a),
            output_path=str(judgments_a),
            cache_path=None,  # force LM path
            allow_unpinned_checksum=True,
        )
        await judge_hypotheses(
            dataset_path=str(dataset),
            variant='oracle',
            hypotheses_path=str(hypotheses_b),
            output_path=str(judgments_b),
            cache_path=None,
            allow_unpinned_checksum=True,
        )

    rows_a = [
        LongMemEvalJudgment(**json.loads(line))
        for line in judgments_a.read_text().splitlines()
        if line
    ]
    rows_b = [
        LongMemEvalJudgment(**json.loads(line))
        for line in judgments_b.read_text().splitlines()
        if line
    ]

    # Every judgment has ``is_abstention_hypothesis`` populated.
    assert all(hasattr(r, 'is_abstention_hypothesis') for r in rows_a + rows_b)

    correct_a = sum(1 for r in rows_a if r.correct)
    correct_b = sum(1 for r in rows_b if r.correct)
    assert correct_a == 5  # all match
    assert correct_b == 4  # smoke-q-003 fails under the flip
    assert correct_a != correct_b  # the discriminator actually discriminates

    # And the abstention classifier ran for every hypothesis — including
    # non-abstention ones — so the field is populated for both classes.
    non_abs = [r for r in rows_a if not r.is_abstention]
    abs_ = [r for r in rows_a if r.is_abstention]
    assert any(r.is_abstention_hypothesis for r in abs_)
    assert all(not r.is_abstention_hypothesis for r in non_abs)


def test_smoke_dataset_with_live_session_parses(tmp_path: Path) -> None:
    """A non-empty session round-trips through the dataset loader with
    turns and timestamps intact, and through ``build_note_payloads`` so
    the ingest formatter path is exercised end-to-end."""
    import base64

    from memex_eval.external.longmemeval_common import load_longmemeval_oracle
    from memex_eval.external.longmemeval_ingest import build_note_payloads

    path = _write_dataset_with_live_session(tmp_path)
    questions = load_longmemeval_oracle(path, allow_unpinned=True)
    assert len(questions) == 1
    q = questions[0]
    assert len(q.sessions) == 1
    assert len(q.sessions[0].turns) == 2
    assert q.sessions[0].turns[0].content.startswith('Schedule')

    # Drive the ingest note-builder against the live session — this is
    # the authored code path the smoke test previously failed to
    # exercise (pre-M6 it used an in-process answer formatter; post-M6
    # the subagent does formatting, so ingest is the real check).
    payloads = build_note_payloads(q, variant='oracle', vault_id='v-1')
    assert len(payloads) == 1
    md = base64.b64decode(payloads[0].content).decode('utf-8')
    assert 'Schedule the meeting for Tuesday at 3pm.' in md
    assert 'publish_date: 2024-02-01T10:00:00+00:00' in md


def test_aggregate_per_category() -> None:
    judgments = [
        LongMemEvalJudgment(
            question_id='a',
            category=LongMemEvalCategory.MULTI_SESSION,
            is_abstention=False,
            hypothesis='x',
            expected='x',
            correct=True,
            judge_reasoning='ok',
            judge_model_fingerprint='test',
        ),
        LongMemEvalJudgment(
            question_id='b',
            category=LongMemEvalCategory.MULTI_SESSION,
            is_abstention=False,
            hypothesis='x',
            expected='y',
            correct=False,
            judge_reasoning='ok',
            judge_model_fingerprint='test',
        ),
        LongMemEvalJudgment(
            question_id='c_abs',
            category=LongMemEvalCategory.KNOWLEDGE_UPDATE,
            is_abstention=True,
            hypothesis='I do not know based on the available memory.',
            expected=None,
            correct=True,
            judge_reasoning='ok',
            judge_model_fingerprint='test',
            is_abstention_hypothesis=True,
        ),
    ]
    agg = aggregate_for_test(judgments)
    assert agg['overall_accuracy'] == round(2 / 3, 4)
    assert agg['abstention_recall'] == 1.0
    assert agg['abstention_precision'] == 1.0
