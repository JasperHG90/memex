"""Tests for ExpectedOutcome subclasses against fake AgentAnswer inputs."""

from __future__ import annotations

from types import SimpleNamespace

from memex_eval.suite import (
    AgentAnswer,
    EntityCooccurs,
    EntityResolves,
    ExcludedByDefault,
    GoldUnitIds,
    KeywordsAbsent,
    KeywordsPresent,
    LintFindingPresent,
    RankingOrder,
    Scenario,
    ToolCallContains,
)


def _scenario(query: str = 'q') -> Scenario:
    return Scenario(
        id='s1',
        description='d',
        query=query,
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )


def _unit(text: str, uid: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(text=text, id=uid or 'u-1')


class TestKeywordsPresent:
    def test_present_via_units(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=['Sarah Chen'])
        ans = AgentAnswer(units=[_unit('Sarah Chen leads Alpha')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_present_via_answer_text(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=['Sarah Chen'])
        ans = AgentAnswer(answer_text='The lead is Sarah Chen.')
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_absent(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=['Sarah Chen'])
        ans = AgentAnswer(units=[_unit('Marcus Rivera leads Beta')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestKeywordsAbsent:
    def test_absent_passes(self) -> None:
        outcome = KeywordsAbsent(type='keywords_absent', keywords=['secret'])
        ans = AgentAnswer(units=[_unit('public information')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_present_fails(self) -> None:
        outcome = KeywordsAbsent(type='keywords_absent', keywords=['secret'])
        ans = AgentAnswer(units=[_unit('this is secret')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestGoldUnitIds:
    def test_recall_via_retrieved_unit_ids(self) -> None:
        outcome = GoldUnitIds(
            type='gold_unit_ids',
            note_keys=['note-a'],
            metrics_to_compute=['recall_at_k', 'mrr'],
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3'])
        scenario = _scenario()
        result = outcome.score(ans, scenario, note_key_to_unit_ids={'note-a': ['u2']})
        assert result['recall_at_5'] == 1.0
        assert result['mrr'] == 0.5  # u2 is rank 2

    def test_recall_via_units_fallback(self) -> None:
        outcome = GoldUnitIds(
            type='gold_unit_ids',
            note_keys=['note-a'],
            metrics_to_compute=['recall_at_k'],
        )
        ans = AgentAnswer(units=[_unit('x', 'u1'), _unit('y', 'u2')])
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids={'note-a': ['u1']})
        assert result['recall_at_5'] == 1.0


class TestRankingOrder:
    def test_correct_order_via_units(self) -> None:
        outcome = RankingOrder(
            type='ranking_order', expected_keyword_order=['Python 3.12', 'Python 3.11']
        )
        ans = AgentAnswer(units=[_unit('migrated to Python 3.12'), _unit('was on Python 3.11')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_inverted_fails(self) -> None:
        outcome = RankingOrder(type='ranking_order', expected_keyword_order=['new', 'old'])
        ans = AgentAnswer(units=[_unit('this is old'), _unit('this is new')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestExcludedByDefault:
    def test_passes_when_forbidden_absent(self) -> None:
        outcome = ExcludedByDefault(type='excluded_by_default', forbidden_keywords=['deprecated'])
        ans = AgentAnswer(units=[_unit('current best practice')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_fails_when_present(self) -> None:
        outcome = ExcludedByDefault(type='excluded_by_default', forbidden_keywords=['deprecated'])
        ans = AgentAnswer(units=[_unit('this is deprecated')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestEntityResolves:
    def test_match(self) -> None:
        outcome = EntityResolves(type='entity_resolves', expected_names=['Elena Vasquez'])
        ans = AgentAnswer(entities=[SimpleNamespace(name='Elena Vasquez', type='person')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_type_mismatch(self) -> None:
        outcome = EntityResolves(
            type='entity_resolves',
            expected_names=['Elena Vasquez'],
            expected_type='organization',
        )
        ans = AgentAnswer(entities=[SimpleNamespace(name='Elena Vasquez', type='person')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestEntityCooccurs:
    def test_neighbor_present(self) -> None:
        outcome = EntityCooccurs(type='entity_cooccurs', expected_neighbors=['Raj Mehta'])
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Elena')],
            cooccurrences=[SimpleNamespace(name='Raj Mehta')],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_no_entity(self) -> None:
        outcome = EntityCooccurs(type='entity_cooccurs', expected_neighbors=['x'])
        ans = AgentAnswer(entities=[])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestLintFindingPresent:
    def test_match(self) -> None:
        outcome = LintFindingPresent(
            type='lint_finding_present', expected_rule_name='surprise_gate_llm'
        )
        ans = AgentAnswer(lint_findings=[SimpleNamespace(rule_name='surprise_gate_llm')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_no_match(self) -> None:
        outcome = LintFindingPresent(type='lint_finding_present', expected_rule_name='other_rule')
        ans = AgentAnswer(lint_findings=[SimpleNamespace(rule_name='surprise_gate_llm')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestToolCallContains:
    def test_required_tool_called(self) -> None:
        outcome = ToolCallContains(
            type='tool_call_contains', expected_tools=['memex_memory_search']
        )
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_memory_search', 'input': {}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_missing_tool(self) -> None:
        outcome = ToolCallContains(
            type='tool_call_contains', expected_tools=['memex_memory_search']
        )
        ans = AgentAnswer(tool_calls=[{'tool': 'something_else', 'input': {}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_min_count(self) -> None:
        outcome = ToolCallContains(
            type='tool_call_contains',
            expected_tools=['memex_memory_search'],
            min_count=2,
        )
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_memory_search', 'input': {}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}
