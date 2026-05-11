"""Tests for AnyOfOutcomes — OR-composition over children."""

from __future__ import annotations

from memex_eval.suite import (
    AgentAnswer,
    AnyOfOutcomes,
    CompositeOutcome,
    GoldUnitIds,
    KeywordsPresent,
    Scenario,
    ToolCallContains,
)


def _scenario() -> Scenario:
    return Scenario(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )


def _kw(keyword: str) -> KeywordsPresent:
    return KeywordsPresent(type='keywords_present', keywords=[keyword])


def _tc(tool: str) -> ToolCallContains:
    return ToolCallContains(type='tool_call_contains', expected_tools=[tool])


class TestBasicSemantics:
    def test_first_child_passes(self) -> None:
        outcome = AnyOfOutcomes(type='any_of', children=[_kw('python'), _kw('zzz')])
        ans = AgentAnswer(answer_text='python is the language')
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_second_child_passes(self) -> None:
        outcome = AnyOfOutcomes(type='any_of', children=[_kw('zzz'), _kw('python')])
        ans = AgentAnswer(answer_text='python is the language')
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_no_child_passes_fails(self) -> None:
        outcome = AnyOfOutcomes(type='any_of', children=[_kw('zzz'), _kw('yyy')])
        ans = AgentAnswer(answer_text='python is the language')
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestNested:
    def test_anyof_inside_composite_and(self) -> None:
        anyof = AnyOfOutcomes(
            type='any_of',
            children=[_tc('memex_survey'), _tc('memex_memory_search')],
        )
        composite = CompositeOutcome(
            type='composite',
            children=[anyof, _kw('alpha')],
        )
        ans = AgentAnswer(
            answer_text='Project Alpha covers leadership and tech stack',
            tool_calls=[{'tool': 'memex_memory_search', 'input': {}}],
        )
        result = composite.score(ans, _scenario())
        assert result['pass'] == 1.0

    def test_anyof_in_composite_fails_if_anyof_fails(self) -> None:
        anyof = AnyOfOutcomes(
            type='any_of',
            children=[_tc('memex_survey'), _tc('memex_memory_search')],
        )
        composite = CompositeOutcome(
            type='composite',
            children=[anyof, _kw('alpha')],
        )
        ans = AgentAnswer(
            answer_text='Project Alpha covers leadership',
            tool_calls=[{'tool': 'memex_get_vault_summary', 'input': {}}],
        )
        result = composite.score(ans, _scenario())
        assert result['pass'] == 0.0


def test_model_rebuild_resolves_forward_refs() -> None:
    """Forward-ref children coerce as concrete subclasses at load time."""
    data = {
        'type': 'any_of',
        'children': [
            {'type': 'keywords_present', 'keywords': ['x']},
            {'type': 'tool_call_contains', 'expected_tools': ['memex_x']},
        ],
    }
    outcome = AnyOfOutcomes.model_validate(data)
    assert isinstance(outcome.children[0], KeywordsPresent)
    assert isinstance(outcome.children[1], ToolCallContains)


def test_referenced_note_keys_unioned() -> None:
    a = GoldUnitIds(type='gold_unit_ids', note_keys=['note-a'])
    b = GoldUnitIds(type='gold_unit_ids', note_keys=['note-b'])
    outcome = AnyOfOutcomes(type='any_of', children=[a, b])
    assert outcome.referenced_note_keys() == {'note-a', 'note-b'}
