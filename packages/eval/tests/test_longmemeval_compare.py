"""Unit tests for longmemeval_compare multi-mode comparison report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex_eval.external.longmemeval_compare import (
    _dedupe_by_qid,
    _summarize_run,
    generate_comparison_report,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')


def _judgment(qid: str, correct: bool, retrieval: bool, interp: str) -> dict:
    return {
        'question_id': qid,
        'correct': correct,
        'retrieval_contains_answer': retrieval,
        'interpretation': interp,
    }


def _hypothesis(
    qid: str, latency_ms: float = 30_000, tokens: int = 80_000, cost: float = 0.10
) -> dict:
    return {
        'question_id': qid,
        'hypothesis': 'test',
        'latency_ms': latency_ms,
        'input_tokens': tokens // 2,
        'output_tokens': tokens - (tokens // 2),
        'cost_usd': cost,
    }


def test_dedupe_by_qid_keeps_first() -> None:
    records = [
        {'question_id': 'a', 'x': 1},
        {'question_id': 'b', 'x': 2},
        {'question_id': 'a', 'x': 99},  # duplicate — should be ignored
    ]
    out = _dedupe_by_qid(records)
    assert out['a']['x'] == 1
    assert out['b']['x'] == 2
    assert len(out) == 2


def test_dedupe_ignores_missing_qid() -> None:
    out = _dedupe_by_qid([{'no_id': 1}, {'question_id': 'a', 'x': 1}])
    assert list(out.keys()) == ['a']


def test_summarize_run_computes_stats(tmp_path: Path) -> None:
    run = tmp_path / 'run-agent'
    _write_jsonl(
        run / 'judgments.jsonl',
        [
            _judgment('q1', True, True, 'correct'),
            _judgment('q2', False, True, 'model_error'),
            _judgment('q3', False, False, 'retr_fail'),
        ],
    )
    _write_jsonl(
        run / 'hypotheses.jsonl',
        [
            _hypothesis('q1', latency_ms=10_000, tokens=50_000, cost=0.05),
            _hypothesis('q2', latency_ms=20_000, tokens=70_000, cost=0.10),
            _hypothesis('q3', latency_ms=30_000, tokens=90_000, cost=0.15),
        ],
    )

    summary = _summarize_run('agent', run)

    assert summary['n_questions'] == 3
    assert summary['correct'] == 1
    assert summary['accuracy'] == pytest.approx(1 / 3)
    assert summary['retrieval_found'] == 2
    assert summary['retrieval_recall'] == pytest.approx(2 / 3)
    assert summary['interpretations'] == {'correct': 1, 'model_error': 1, 'retr_fail': 1}
    assert summary['avg_latency_s'] == pytest.approx(20.0)
    assert summary['median_latency_s'] == pytest.approx(20.0)
    assert summary['avg_total_tokens'] == pytest.approx(70_000)
    assert summary['total_cost_usd'] == pytest.approx(0.30)
    assert summary['avg_cost_usd'] == pytest.approx(0.10)


def test_summarize_run_handles_missing_files(tmp_path: Path) -> None:
    summary = _summarize_run('empty', tmp_path / 'nope')
    assert summary['n_questions'] == 0
    assert summary['accuracy'] == 0.0
    assert summary['total_cost_usd'] == 0.0


def test_generate_comparison_report_produces_markdown(tmp_path: Path) -> None:
    # Two runs with partially overlapping results
    agent_run = tmp_path / 'agent'
    _write_jsonl(
        agent_run / 'judgments.jsonl',
        [
            _judgment('q1', True, True, 'correct'),
            _judgment('q2', False, True, 'model_error'),
            _judgment('q3', True, True, 'correct'),
        ],
    )
    _write_jsonl(
        agent_run / 'hypotheses.jsonl',
        [_hypothesis('q1'), _hypothesis('q2'), _hypothesis('q3')],
    )

    note_run = tmp_path / 'note-only'
    _write_jsonl(
        note_run / 'judgments.jsonl',
        [
            _judgment('q1', True, True, 'correct'),
            _judgment('q2', False, False, 'retr_fail'),
            _judgment('q3', False, False, 'retr_fail'),
        ],
    )
    _write_jsonl(
        note_run / 'hypotheses.jsonl',
        [_hypothesis('q1', cost=0.01), _hypothesis('q2', cost=0.01), _hypothesis('q3', cost=0.01)],
    )

    out = tmp_path / 'report'
    summary = generate_comparison_report(
        [('agent', agent_run), ('note-only', note_run)],
        out,
    )

    # Report files exist
    assert (out / 'comparison_report.md').exists()
    assert (out / 'comparison.json').exists()

    # Summary structure
    assert len(summary['modes']) == 2
    assert summary['n_questions_total'] == 3
    assert summary['agreement']['all_correct'] == 1  # q1: both correct
    assert summary['agreement']['all_wrong'] == 1  # q2: both wrong
    assert summary['agreement']['mixed'] == 1  # q3: agent correct, note wrong

    # Markdown content sanity
    md = (out / 'comparison_report.md').read_text()
    assert '# LongMemEval Mode Comparison' in md
    assert '| agent |' in md
    assert '| note-only |' in md
    assert 'q1' in md
    assert 'model-fail' in md or 'retr-fail' in md


def test_comparison_marks_all_wrong_correctly(tmp_path: Path) -> None:
    """When every mode gets a question wrong, it's classified as all_wrong."""
    run_a = tmp_path / 'a'
    _write_jsonl(
        run_a / 'judgments.jsonl',
        [_judgment('q1', False, False, 'retr_fail')],
    )
    _write_jsonl(run_a / 'hypotheses.jsonl', [_hypothesis('q1')])

    run_b = tmp_path / 'b'
    _write_jsonl(
        run_b / 'judgments.jsonl',
        [_judgment('q1', False, True, 'model_error')],
    )
    _write_jsonl(run_b / 'hypotheses.jsonl', [_hypothesis('q1')])

    summary = generate_comparison_report([('a', run_a), ('b', run_b)], tmp_path / 'report')
    assert summary['agreement']['all_wrong'] == 1
    assert summary['agreement']['all_correct'] == 0
    assert summary['agreement']['mixed'] == 0


def test_comparison_missing_question_renders_dash(tmp_path: Path) -> None:
    """A question present in one run but not another gets a dash in the table."""
    run_a = tmp_path / 'a'
    _write_jsonl(
        run_a / 'judgments.jsonl',
        [_judgment('q1', True, True, 'correct'), _judgment('q2', True, True, 'correct')],
    )
    _write_jsonl(run_a / 'hypotheses.jsonl', [_hypothesis('q1'), _hypothesis('q2')])

    run_b = tmp_path / 'b'
    _write_jsonl(run_b / 'judgments.jsonl', [_judgment('q1', False, False, 'retr_fail')])
    _write_jsonl(run_b / 'hypotheses.jsonl', [_hypothesis('q1')])

    out = tmp_path / 'report'
    generate_comparison_report([('a', run_a), ('b', run_b)], out)

    md = (out / 'comparison_report.md').read_text()
    # q2 exists in a but not b — b column should show "—"
    assert '| q2 | ✓ | — |' in md or 'q2' in md and '—' in md
