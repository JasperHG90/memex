"""Tests for ToolCallCountAcross — sum over a set of tool names."""

from __future__ import annotations

from memex_eval.suite import AgentAnswer, KeywordsPresent, Scenario, ToolCallCountAcross


def _scenario() -> Scenario:
    return Scenario(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )


def _outcome(**overrides) -> ToolCallCountAcross:
    base = dict(
        type='tool_call_count_across',
        expected_tools=['memex_memory_search', 'memex_note_search'],
        min_total=3,
    )
    base.update(overrides)
    return ToolCallCountAcross(**base)


def test_sum_meets_threshold() -> None:
    outcome = _outcome()
    ans = AgentAnswer(
        tool_calls=[
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_note_search', 'input': {}},
        ]
    )
    assert outcome.score(ans, _scenario()) == {'pass': 1.0}


def test_sum_below_threshold() -> None:
    outcome = _outcome()
    ans = AgentAnswer(
        tool_calls=[
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_note_search', 'input': {}},
        ]
    )
    assert outcome.score(ans, _scenario()) == {'pass': 0.0}


def test_unrelated_tools_ignored() -> None:
    outcome = _outcome()
    ans = AgentAnswer(
        tool_calls=[
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_get_vault_summary', 'input': {}},
            {'tool': 'memex_list_entities', 'input': {}},
        ]
    )
    assert outcome.score(ans, _scenario()) == {'pass': 0.0}


def test_single_tool_repeated_counts() -> None:
    outcome = _outcome(expected_tools=['memex_memory_search'], min_total=3)
    ans = AgentAnswer(
        tool_calls=[
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_memory_search', 'input': {}},
            {'tool': 'memex_memory_search', 'input': {}},
        ]
    )
    assert outcome.score(ans, _scenario()) == {'pass': 1.0}
