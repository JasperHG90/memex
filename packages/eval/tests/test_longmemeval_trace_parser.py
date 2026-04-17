"""Tests for LongMemEval trace parser and retrieval recall computation."""

from __future__ import annotations

import json
from pathlib import Path


from memex_eval.external.longmemeval_trace_parser import (
    RecallMetrics,
    TraceAnalysis,
    ToolCall,
    _session_id_from_note_title,
    compute_batch_recall,
    compute_recall,
    format_question_breakdown,
    parse_trace,
    parse_traces_dir,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trace_jsonl(
    tool_calls: list[dict],
    tool_results: dict[str, str],
) -> str:
    """Build a minimal Claude Code session trace JSONL string.

    Args:
        tool_calls: List of {name, id, input} dicts for assistant tool_use blocks.
        tool_results: Mapping of tool_use_id -> result content string.
    """
    lines: list[str] = []

    # Assistant message with tool_use blocks
    assistant_msg = {
        'type': 'assistant',
        'message': {
            'content': [
                {
                    'type': 'tool_use',
                    'name': tc['name'],
                    'id': tc['id'],
                    'input': tc.get('input', {}),
                }
                for tc in tool_calls
            ]
        },
    }
    lines.append(json.dumps(assistant_msg))

    # User message with tool_result blocks
    user_msg = {
        'type': 'user',
        'message': {
            'content': [
                {
                    'type': 'tool_result',
                    'tool_use_id': tid,
                    'content': content,
                }
                for tid, content in tool_results.items()
            ]
        },
    }
    lines.append(json.dumps(user_msg))

    return '\n'.join(lines)


def _make_memory_search_result(note_ids_titles: list[tuple[str, str]]) -> str:
    """Build a memory_search result JSON string."""
    results = []
    for nid, ntitle in note_ids_titles:
        results.append(
            {
                'id': f'unit-{nid[:8]}',
                'text': 'Some fact.',
                'note_id': nid,
                'note_title': ntitle,
            }
        )
    return json.dumps({'result': results})


# ---------------------------------------------------------------------------
# Unit tests: session ID extraction
# ---------------------------------------------------------------------------


def test_session_id_from_note_title_standard() -> None:
    assert _session_id_from_note_title('e47becba \u2014 answer_280352e9') == 'answer_280352e9'


def test_session_id_from_note_title_complex() -> None:
    assert _session_id_from_note_title('abc123 \u2014 sharegpt_yywfIrx_0') == 'sharegpt_yywfIrx_0'


def test_session_id_from_note_title_no_match() -> None:
    assert _session_id_from_note_title('just a plain title') is None


# ---------------------------------------------------------------------------
# Unit tests: trace parsing
# ---------------------------------------------------------------------------


def test_parse_trace_empty_file(tmp_path: Path) -> None:
    trace_file = tmp_path / 'empty.jsonl'
    trace_file.write_text('')
    result = parse_trace(trace_file)
    assert result.question_id == 'empty'
    assert result.calls_by_tool == {}


def test_parse_trace_single_memory_search(tmp_path: Path) -> None:
    content = _make_trace_jsonl(
        tool_calls=[
            {
                'name': 'mcp__memex__memex_memory_search',
                'id': 'call-1',
                'input': {'query': 'test'},
            },
        ],
        tool_results={
            'call-1': _make_memory_search_result(
                [
                    ('note-aaa', 'q1 \u2014 session_a'),
                    ('note-bbb', 'q1 \u2014 session_b'),
                ]
            ),
        },
    )
    trace_file = tmp_path / 'q1.jsonl'
    trace_file.write_text(content)

    result = parse_trace(trace_file)
    assert result.question_id == 'q1'
    calls = result.calls_by_tool.get('mcp__memex__memex_memory_search', [])
    assert len(calls) == 1
    assert len(calls[0].result_note_ids) == 2
    assert 'note-aaa' in calls[0].result_note_ids
    assert calls[0].result_unit_count == 2


def test_parse_trace_caps_at_3(tmp_path: Path) -> None:
    """Verify that capped_calls returns at most 3."""
    call_specs: list[tuple[str, str]] = []
    tool_results: dict[str, str] = {}
    for i in range(5):
        tid = f'call-{i}'
        call_specs.append((tid, f'q{i}'))
        tool_results[tid] = _make_memory_search_result(
            [
                (f'note-{i}', f'q1 \u2014 session_{i}'),
            ]
        )

    # Build trace with multiple assistant messages
    lines: list[str] = []
    for tid, query in call_specs:
        lines.append(
            json.dumps(
                {
                    'type': 'assistant',
                    'message': {
                        'content': [
                            {
                                'type': 'tool_use',
                                'name': 'mcp__memex__memex_memory_search',
                                'id': tid,
                                'input': {'query': query},
                            }
                        ]
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    'type': 'user',
                    'message': {
                        'content': [
                            {
                                'type': 'tool_result',
                                'tool_use_id': tid,
                                'content': tool_results[tid],
                            }
                        ],
                    },
                }
            )
        )

    trace_file = tmp_path / 'q1.jsonl'
    trace_file.write_text('\n'.join(lines))

    result = parse_trace(trace_file)
    all_calls = result.calls_by_tool['mcp__memex__memex_memory_search']
    assert len(all_calls) == 5
    capped = result.capped_calls('mcp__memex__memex_memory_search')
    assert len(capped) == 3


def test_parse_trace_ignores_non_retrieval_tools(tmp_path: Path) -> None:
    content = _make_trace_jsonl(
        tool_calls=[
            {
                'name': 'mcp__memex__memex_get_page_indices',
                'id': 'call-1',
                'input': {},
            },
        ],
        tool_results={
            'call-1': json.dumps({'result': []}),
        },
    )
    trace_file = tmp_path / 'q1.jsonl'
    trace_file.write_text(content)

    result = parse_trace(trace_file)
    assert result.calls_by_tool == {}


# ---------------------------------------------------------------------------
# Unit tests: recall computation
# ---------------------------------------------------------------------------


def test_compute_recall_full_hit() -> None:
    trace = TraceAnalysis(
        question_id='q1',
        calls_by_tool={
            'mcp__memex__memex_memory_search': [
                ToolCall(
                    tool_name='mcp__memex__memex_memory_search',
                    tool_use_id='c1',
                    result_note_ids=['n1'],
                    result_note_titles=['q1 \u2014 answer_abc'],
                    result_unit_count=1,
                ),
            ],
        },
    )
    metrics = compute_recall(trace, ['answer_abc'], category='single-session-user')
    assert metrics.recall == 1.0
    assert metrics.found_session_ids == ['answer_abc']


def test_compute_recall_partial_hit() -> None:
    trace = TraceAnalysis(
        question_id='q2',
        calls_by_tool={
            'mcp__memex__memex_memory_search': [
                ToolCall(
                    tool_name='mcp__memex__memex_memory_search',
                    tool_use_id='c1',
                    result_note_ids=['n1'],
                    result_note_titles=['q2 \u2014 session_a'],
                    result_unit_count=1,
                ),
            ],
        },
    )
    metrics = compute_recall(trace, ['session_a', 'session_b'])
    assert metrics.recall == 0.5
    assert 'session_a' in metrics.found_session_ids
    assert 'session_b' not in metrics.found_session_ids


def test_compute_recall_no_gold() -> None:
    trace = TraceAnalysis(question_id='q3', calls_by_tool={})
    metrics = compute_recall(trace, [])
    assert metrics.recall == 1.0


def test_compute_recall_miss() -> None:
    trace = TraceAnalysis(
        question_id='q4',
        calls_by_tool={
            'mcp__memex__memex_memory_search': [
                ToolCall(
                    tool_name='mcp__memex__memex_memory_search',
                    tool_use_id='c1',
                    result_note_ids=['n1'],
                    result_note_titles=['q4 \u2014 wrong_session'],
                    result_unit_count=1,
                ),
            ],
        },
    )
    metrics = compute_recall(trace, ['correct_session'])
    assert metrics.recall == 0.0
    assert metrics.found_session_ids == []


# ---------------------------------------------------------------------------
# Unit tests: batch recall
# ---------------------------------------------------------------------------


def test_compute_batch_recall() -> None:
    traces = [
        TraceAnalysis(
            question_id='q1',
            calls_by_tool={
                'mcp__memex__memex_memory_search': [
                    ToolCall(
                        tool_name='mcp__memex__memex_memory_search',
                        tool_use_id='c1',
                        result_note_ids=['n1'],
                        result_note_titles=['q1 \u2014 s1'],
                        result_unit_count=1,
                    ),
                ],
            },
        ),
        TraceAnalysis(
            question_id='q2',
            calls_by_tool={},
        ),
    ]
    gold_map = {'q1': ['s1'], 'q2': ['s2']}
    results = compute_batch_recall(traces, gold_map)
    assert len(results) == 2
    assert results[0].recall == 1.0
    assert results[1].recall == 0.0


# ---------------------------------------------------------------------------
# Unit tests: formatting
# ---------------------------------------------------------------------------


def test_format_question_breakdown_with_metrics() -> None:
    trace = TraceAnalysis(
        question_id='q1',
        calls_by_tool={
            'mcp__memex__memex_memory_search': [
                ToolCall(
                    tool_name='mcp__memex__memex_memory_search',
                    tool_use_id='c1',
                    result_note_ids=['note-aaa'],
                    result_note_titles=['q1 \u2014 s1'],
                    result_unit_count=3,
                ),
            ],
        },
    )
    metrics = RecallMetrics(
        question_id='q1',
        category='single-session-user',
        gold_session_ids=['s1'],
        found_session_ids=['s1'],
        recall=1.0,
    )
    output = format_question_breakdown(trace, metrics)
    assert 'Question q1 (single-session-user)' in output
    assert 'memory_search (1/1 calls used for recall)' in output
    assert 'Recall@3: 1/1 gold sessions found (1.000)' in output


def test_format_question_breakdown_no_metrics() -> None:
    trace = TraceAnalysis(question_id='q2', calls_by_tool={})
    output = format_question_breakdown(trace, None)
    assert 'Question q2' in output
    assert 'no gold session IDs' in output


# ---------------------------------------------------------------------------
# Integration: parse_traces_dir
# ---------------------------------------------------------------------------


def test_parse_traces_dir(tmp_path: Path) -> None:
    # Create two trace files
    for qid in ('q1', 'q2'):
        content = _make_trace_jsonl(
            tool_calls=[
                {
                    'name': 'mcp__memex__memex_memory_search',
                    'id': f'call-{qid}',
                    'input': {'query': 'test'},
                },
            ],
            tool_results={
                f'call-{qid}': _make_memory_search_result(
                    [
                        (f'note-{qid}', f'{qid} \u2014 sid_{qid}'),
                    ]
                ),
            },
        )
        (tmp_path / f'{qid}.jsonl').write_text(content)

    traces = parse_traces_dir(tmp_path)
    assert len(traces) == 2
    assert traces[0].question_id == 'q1'
    assert traces[1].question_id == 'q2'
