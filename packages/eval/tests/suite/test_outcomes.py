"""Tests for ExpectedOutcome subclasses against fake AgentAnswer inputs."""

from __future__ import annotations

from types import SimpleNamespace

from memex_eval.suite import (
    AgentAnswer,
    EntityCooccurs,
    EntityMentionContains,
    EntityResolves,
    ExcludedByDefault,
    GoldUnitIds,
    KeywordsAbsent,
    KeywordsPresent,
    KvRoundtrip,
    LintFindingPresent,
    LLMJudge,
    RankingOrder,
    Scenario,
    SummaryNonempty,
    ToolCallContains,
    UnitMetadataMatches,
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


class TestKeywordWordBoundary:
    """`Scala` (the language) must not match `scalability` (substring).

    Pre-fix substring matching caused the eval suite's vault-isolation
    scenario to false-fail because Project Gamma's text mentions
    'scalability' — and 'Scala' is a substring of 'scalability'.
    Word-boundary matching is the correct primitive."""

    def test_keywords_absent_does_not_match_scala_in_scalability(self) -> None:
        outcome = KeywordsAbsent(type='keywords_absent', keywords=['Scala'])
        ans = AgentAnswer(units=[_unit('platform supports scalability via BEAM VM')])
        # Scala-the-language is correctly absent; 'scalability' must not trip.
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_keywords_absent_still_catches_actual_word(self) -> None:
        outcome = KeywordsAbsent(type='keywords_absent', keywords=['Scala'])
        ans = AgentAnswer(units=[_unit('Project Delta uses Scala 3.4 with Akka')])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_keywords_present_handles_nonword_prefix(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=['$1000'])
        ans = AgentAnswer(units=[_unit('I paid $1000 yesterday')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_keywords_present_handles_apostrophe(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=["O'Connor"])
        ans = AgentAnswer(units=[_unit("Met O'Connor today")])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_keywords_present_multiword_token_still_matches(self) -> None:
        outcome = KeywordsPresent(type='keywords_present', keywords=['Project Alpha'])
        ans = AgentAnswer(units=[_unit('Project Alpha launched on March 15')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_ranking_order_text_mode_uses_word_boundary(self) -> None:
        # Text-mode branch (answer_text populated, units empty) — earlier code
        # used substring matching; verify it now uses word-boundary.
        outcome = RankingOrder(type='ranking_order', expected_keyword_order=['Scala', 'Akka'])
        # 'scalability' should NOT count as a Scala hit; both keywords absent → pass=0.
        ans = AgentAnswer(answer_text='The platform supports scalability and parallelism')
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_ranking_order_units_mode_uses_word_boundary(self) -> None:
        outcome = RankingOrder(type='ranking_order', expected_keyword_order=['Scala', 'Akka'])
        ans = AgentAnswer(
            units=[
                _unit('platform supports scalability'),  # not a Scala hit
                _unit('uses Scala 3.4'),  # real Scala hit at rank 1
                _unit('with Akka framework'),  # real Akka hit at rank 2
            ]
        )
        # Scala first hit at rank 1; Akka first hit at rank 2; ascending → pass=1.
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_excluded_by_default_word_boundary(self) -> None:
        outcome = ExcludedByDefault(type='excluded_by_default', forbidden_keywords=['Scala'])
        ans = AgentAnswer(units=[_unit('platform supports scalability via BEAM VM')])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


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
    """Cooccurrences come back as dicts from the server with
    ``entity_1_name`` / ``entity_2_name`` / ``entity_id_1`` / ``entity_id_2``
    keys (see ``packages/core/src/memex_core/server/entities.py``)."""

    def test_neighbor_present(self) -> None:
        outcome = EntityCooccurs(type='entity_cooccurs', expected_neighbors=['Raj Mehta'])
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Elena Vasquez', id='elena-id')],
            cooccurrences=[
                {
                    'entity_id_1': 'elena-id',
                    'entity_id_2': 'raj-id',
                    'entity_1_name': 'Elena Vasquez',
                    'entity_2_name': 'Raj Mehta',
                }
            ],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_neighbor_present_when_queried_is_entity_2(self) -> None:
        outcome = EntityCooccurs(type='entity_cooccurs', expected_neighbors=['Raj Mehta'])
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Elena Vasquez', id='elena-id')],
            cooccurrences=[
                {
                    'entity_id_1': 'raj-id',
                    'entity_id_2': 'elena-id',
                    'entity_1_name': 'Raj Mehta',
                    'entity_2_name': 'Elena Vasquez',
                }
            ],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_neighbor_missing(self) -> None:
        outcome = EntityCooccurs(type='entity_cooccurs', expected_neighbors=['Raj Mehta'])
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Elena Vasquez', id='elena-id')],
            cooccurrences=[
                {
                    'entity_id_1': 'elena-id',
                    'entity_id_2': 'lisa-id',
                    'entity_1_name': 'Elena Vasquez',
                    'entity_2_name': 'Lisa Chang',
                }
            ],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

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

    def test_match_mode_any_passes_when_one_listed_tool_called(self) -> None:
        """``match_mode='any'`` accepts any listed tool — used when several
        tools satisfy the same role (regression guard for review feedback M2)."""
        outcome = ToolCallContains(
            type='tool_call_contains',
            expected_tools=['memex_memory_search', 'memex_note_search'],
            min_count=1,
            match_mode='any',
        )
        # Only memex_note_search was called; under default 'all' mode this
        # would fail because memex_memory_search wasn't called.
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_note_search', 'input': {}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_match_mode_any_fails_when_no_listed_tool_called(self) -> None:
        outcome = ToolCallContains(
            type='tool_call_contains',
            expected_tools=['memex_memory_search', 'memex_note_search'],
            min_count=1,
            match_mode='any',
        )
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_list_entities', 'input': {}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class _FakeJudge:
    """Stub matching the Judge.consume_usage contract used by LLMJudge.score."""

    def __init__(self, score: float, usage: dict[str, float]) -> None:
        self._score = score
        self._usage = usage
        self._consumed = False

    def judge_graded_correctness(self, *_args, **_kwargs):
        return self._score, 'reasoning'

    def consume_usage(self) -> dict[str, float]:
        if self._consumed:
            return {'tokens_in': 0.0, 'tokens_out': 0.0, 'cost_usd': 0.0}
        self._consumed = True
        return dict(self._usage)


class TestLLMJudgeUsageAbsorption:
    def test_judge_usage_drained_into_answer(self) -> None:
        outcome = LLMJudge(type='llm_judge', rubric='rubric', threshold=0.5)
        ans = AgentAnswer(answer_text='answer body')
        judge = _FakeJudge(
            score=1.0,
            usage={'tokens_in': 123.0, 'tokens_out': 45.0, 'cost_usd': 0.00078},
        )
        result = outcome.score(ans, _scenario(), judge=judge)
        assert result == {'graded_score': 1.0, 'pass': 1.0}
        assert ans.tokens_in == 123
        assert ans.tokens_out == 45
        assert ans.cost_usd == 0.00078


class TestEntityMentionContains:
    def test_keyword_present_in_mention_text_passes(self) -> None:
        outcome = EntityMentionContains(
            type='entity_mention_contains',
            expected_name='Sarah Chen',
            expected_keywords=['Project Alpha'],
        )
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Sarah Chen', id='e1')],
            entity_mentions=[
                {'unit': SimpleNamespace(text='Sarah Chen leads Project Alpha at Acme.')}
            ],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_keyword_missing_fails(self) -> None:
        outcome = EntityMentionContains(
            type='entity_mention_contains',
            expected_name='Sarah Chen',
            expected_keywords=['Project Beta'],
        )
        ans = AgentAnswer(
            entities=[SimpleNamespace(name='Sarah Chen', id='e1')],
            entity_mentions=[{'unit': SimpleNamespace(text='Sarah Chen leads Project Alpha.')}],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_no_mentions_fails(self) -> None:
        outcome = EntityMentionContains(
            type='entity_mention_contains',
            expected_keywords=['anything'],
        )
        ans = AgentAnswer()
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_min_mentions_threshold(self) -> None:
        outcome = EntityMentionContains(
            type='entity_mention_contains',
            expected_keywords=['lead'],
            min_mentions=3,
        )
        ans = AgentAnswer(
            entity_mentions=[{'unit': SimpleNamespace(text='lead')}],
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestKvRoundtrip:
    def test_value_matches(self) -> None:
        outcome = KvRoundtrip(
            type='kv_roundtrip', kv_key='project:acme:vault', expected_value='engineering'
        )
        ans = AgentAnswer(kv_value='engineering')
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_value_mismatch(self) -> None:
        outcome = KvRoundtrip(
            type='kv_roundtrip', kv_key='project:acme:vault', expected_value='engineering'
        )
        ans = AgentAnswer(kv_value='marketing')
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_missing_value_fails(self) -> None:
        outcome = KvRoundtrip(type='kv_roundtrip', kv_key='missing', expected_value='x')
        ans = AgentAnswer(kv_value=None)
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestSummaryNonempty:
    def test_nonempty_passes(self) -> None:
        outcome = SummaryNonempty(type='summary_nonempty', entity_query='Sarah Chen')
        ans = AgentAnswer(summary_text='Sarah Chen is the engineering lead.')
        result = outcome.score(ans, _scenario())
        assert result['pass'] == 1.0
        assert result['summary_chars'] == 35.0

    def test_empty_fails(self) -> None:
        outcome = SummaryNonempty(type='summary_nonempty')
        ans = AgentAnswer(summary_text='')
        assert outcome.score(ans, _scenario())['pass'] == 0.0

    def test_below_min_chars_fails(self) -> None:
        outcome = SummaryNonempty(type='summary_nonempty', min_chars=50)
        ans = AgentAnswer(summary_text='too short')
        result = outcome.score(ans, _scenario())
        assert result['pass'] == 0.0
        assert result['summary_chars'] == 9.0


class TestUnitMetadataMatches:
    def test_metadata_matches_passes(self) -> None:
        outcome = UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'durable'},
        )
        ans = AgentAnswer(
            units=[
                SimpleNamespace(
                    text='roadmap',
                    id='u1',
                    metadata={'intent_class': 'durable', 'risk_class': 'low'},
                )
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_metadata_mismatch_fails(self) -> None:
        outcome = UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'durable'},
        )
        ans = AgentAnswer(
            units=[SimpleNamespace(text='standup', id='u1', metadata={'intent_class': 'ephemeral'})]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_finds_match_among_many(self) -> None:
        outcome = UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'permanent'},
        )
        ans = AgentAnswer(
            units=[
                SimpleNamespace(metadata={'intent_class': 'ephemeral'}),
                SimpleNamespace(metadata={'intent_class': 'durable'}),
                SimpleNamespace(metadata={'intent_class': 'permanent'}),
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_empty_units_fails(self) -> None:
        outcome = UnitMetadataMatches(type='unit_metadata_matches', expected_metadata={'k': 'v'})
        ans = AgentAnswer()
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}
