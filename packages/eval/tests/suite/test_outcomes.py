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
    HasContradictionLink,
    KeywordsAbsent,
    KeywordsPresent,
    KvRoundtrip,
    LintFindingPresent,
    LLMJudge,
    NewestUnitContains,
    NoteAssetsContain,
    NoteAttribution,
    RankingOrder,
    Scenario,
    SummaryNonempty,
    TemporalOrdering,
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

    def test_fallback_prefers_unit_id_over_source_note_id(self) -> None:
        """``MemoryUnitDTO`` carries BOTH ``id`` (unit UUID) and
        ``note_id`` (source-note UUID). The fallback in
        ``_aggregate_unit_ids`` must return the unit UUID — returning
        the source-note UUID would silently invalidate every unit-id-
        keyed baseline. Round-4 swapped the priority; this test pins
        the contract so a future revert is caught.
        """
        from memex_eval.suite.base import _aggregate_unit_ids

        ans = AgentAnswer(
            units=[
                SimpleNamespace(text='x', id='unit-uuid-1', note_id='source-note-uuid-A'),
                SimpleNamespace(text='y', id='unit-uuid-2', note_id='source-note-uuid-A'),
            ]
        )
        assert _aggregate_unit_ids(ans) == ['unit-uuid-1', 'unit-uuid-2']

    def test_fallback_falls_through_to_note_id_when_id_absent(self) -> None:
        """``NoteSearchResult`` has only ``note_id`` (no ``id``).
        The fallback must still return that ID so note-search outcomes
        keep working when ``retrieved_unit_ids`` happens to be empty.
        """
        from memex_eval.suite.base import _aggregate_unit_ids

        ans = AgentAnswer(
            units=[
                SimpleNamespace(text='', note_id='note-uuid-1'),
                SimpleNamespace(text='', note_id='note-uuid-2'),
            ]
        )
        assert _aggregate_unit_ids(ans) == ['note-uuid-1', 'note-uuid-2']


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


# ---------------------------------------------------------------------------
# P5: new outcome classes
# ---------------------------------------------------------------------------

import datetime as _dt


def _u_with_note(
    text: str, uid: str, note_id: str, ts: _dt.datetime | None = None
) -> SimpleNamespace:
    return SimpleNamespace(text=text, id=uid, note_id=note_id, mentioned_at=ts, occurred_start=None)


class TestTemporalOrdering:
    """P5 #1 — datetime-level ordering replaces brittle keyword-position
    check. Validates that the temporal retrieval strategy actually returns
    the newer note's units before the older note's."""

    def _ctx(self, mapping: dict[str, str]) -> dict:
        return {'_note_id_by_key': mapping}

    def test_descending_order_passes(self) -> None:
        outcome = TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=['q2', 'q1'],
        )
        q1_ts = _dt.datetime(2025, 3, 15, tzinfo=_dt.timezone.utc)
        q2_ts = _dt.datetime(2025, 6, 20, tzinfo=_dt.timezone.utc)
        ans = AgentAnswer(
            units=[
                _u_with_note('q2 highlights', 'u1', 'note-q2', q2_ts),
                _u_with_note('q1 highlights', 'u2', 'note-q1', q1_ts),
            ]
        )
        ctx = self._ctx({'q2': 'note-q2', 'q1': 'note-q1'})
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 1.0
        assert result['notes_retrieved'] == 1.0
        assert result['pairs_compared'] == 1.0

    def test_ascending_order_fails(self) -> None:
        outcome = TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=['q2', 'q1'],
        )
        q1_ts = _dt.datetime(2025, 3, 15, tzinfo=_dt.timezone.utc)
        q2_ts = _dt.datetime(2025, 6, 20, tzinfo=_dt.timezone.utc)
        ans = AgentAnswer(
            units=[
                _u_with_note('q1 actually newer somehow', 'u1', 'note-q1', q2_ts),
                _u_with_note('q2 actually older somehow', 'u2', 'note-q2', q1_ts),
            ]
        )
        ctx = self._ctx({'q2': 'note-q2', 'q1': 'note-q1'})
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 0.0
        # Both notes present but ordered wrong → pairs_compared=1.
        assert result['notes_retrieved'] == 1.0
        assert result['pairs_compared'] == 1.0

    def test_missing_note_key_fails(self) -> None:
        outcome = TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=['q2', 'q1'],
        )
        ans = AgentAnswer(units=[])
        ctx = self._ctx({'q2': 'note-q2'})  # q1 absent
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 0.0
        # Surface "Q1 not even retrieved" via notes_retrieved < 1.0.
        assert result['notes_retrieved'] == 0.0

    def test_no_units_with_timestamp_fails(self) -> None:
        outcome = TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=['q2', 'q1'],
        )
        # Units exist but mentioned_at is None.
        ans = AgentAnswer(
            units=[
                _u_with_note('text', 'u1', 'note-q2', None),
                _u_with_note('text', 'u2', 'note-q1', None),
            ]
        )
        ctx = self._ctx({'q2': 'note-q2', 'q1': 'note-q1'})
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 0.0
        assert result['notes_retrieved'] == 0.0  # no usable timestamps

    def test_uses_occurred_start_when_mentioned_at_missing(self) -> None:
        outcome = TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=['q2', 'q1'],
        )
        q1_ts = _dt.datetime(2025, 3, 15, tzinfo=_dt.timezone.utc)
        q2_ts = _dt.datetime(2025, 6, 20, tzinfo=_dt.timezone.utc)
        u_q2 = SimpleNamespace(
            text='q2', id='u1', note_id='note-q2', mentioned_at=None, occurred_start=q2_ts
        )
        u_q1 = SimpleNamespace(
            text='q1', id='u2', note_id='note-q1', mentioned_at=None, occurred_start=q1_ts
        )
        ans = AgentAnswer(units=[u_q2, u_q1])
        ctx = self._ctx({'q2': 'note-q2', 'q1': 'note-q1'})
        assert outcome.score(ans, _scenario(), context=ctx)['pass'] == 1.0


class TestHasContradictionLink:
    """P5 #2 — assert the contradiction edge exists between newer/older
    units. Uses superseded_by populated on the search-engine path."""

    def _u(self, text: str, superseded_by_data=None) -> SimpleNamespace:
        from memex_common.schemas import SupersessionInfo

        ss = None
        if superseded_by_data:
            ss = [SupersessionInfo(**d) for d in superseded_by_data]
        return SimpleNamespace(text=text, id='u-1', superseded_by=ss)

    def test_has_link_passes(self) -> None:
        outcome = HasContradictionLink(
            type='has_contradiction_link',
            newer_keyword='Ruby Martinez',
            older_keyword='Alex Chen',
        )
        from uuid import uuid4

        ans = AgentAnswer(
            units=[
                self._u(
                    'Ruby Martinez leads Engineering',
                    superseded_by_data=[
                        {
                            'unit_id': str(uuid4()),
                            'unit_text': 'Alex Chen led Engineering',
                            'note_title': 'old',
                            'relation': 'contradicts',
                        },
                    ],
                ),
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_no_superseded_by_fails(self) -> None:
        outcome = HasContradictionLink(
            type='has_contradiction_link',
            newer_keyword='Ruby Martinez',
            older_keyword='Alex Chen',
        )
        ans = AgentAnswer(units=[self._u('Ruby Martinez leads', superseded_by_data=None)])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_relation_filter(self) -> None:
        from uuid import uuid4

        outcome = HasContradictionLink(
            type='has_contradiction_link',
            newer_keyword='Ruby Martinez',
            older_keyword='Alex Chen',
            relation='contradicts',
        )
        # Has a 'weakens' link but expected 'contradicts' — fails.
        ans = AgentAnswer(
            units=[
                self._u(
                    'Ruby Martinez leads',
                    superseded_by_data=[
                        {
                            'unit_id': str(uuid4()),
                            'unit_text': 'Alex Chen led',
                            'note_title': 'old',
                            'relation': 'weakens',
                        },
                    ],
                ),
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_relation_any_matches_anything(self) -> None:
        from uuid import uuid4

        outcome = HasContradictionLink(
            type='has_contradiction_link',
            newer_keyword='Ruby Martinez',
            older_keyword='Alex Chen',
            relation='any',
        )
        ans = AgentAnswer(
            units=[
                self._u(
                    'Ruby Martinez leads',
                    superseded_by_data=[
                        {
                            'unit_id': str(uuid4()),
                            'unit_text': 'Alex Chen led',
                            'note_title': 'old',
                            'relation': 'weakens',
                        },
                    ],
                ),
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


class TestNewestUnitContains:
    """P5 #2 — assert the newest unit's text contains the expected keywords."""

    def test_newest_contains_passes(self) -> None:
        outcome = NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
        )
        old_ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        new_ts = _dt.datetime(2025, 6, 1, tzinfo=_dt.timezone.utc)
        ans = AgentAnswer(
            units=[
                _u_with_note('Alex Chen led', 'u1', 'n1', old_ts),
                _u_with_note('Ruby Martinez leads now', 'u2', 'n2', new_ts),
            ]
        )
        assert outcome.score(ans, _scenario())['pass'] == 1.0

    def test_newest_lacks_keyword_fails(self) -> None:
        outcome = NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
        )
        old_ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        new_ts = _dt.datetime(2025, 6, 1, tzinfo=_dt.timezone.utc)
        ans = AgentAnswer(
            units=[
                _u_with_note('Ruby Martinez was hired', 'u1', 'n1', old_ts),
                _u_with_note('Alex Chen is here today', 'u2', 'n2', new_ts),
            ]
        )
        assert outcome.score(ans, _scenario())['pass'] == 0.0

    def test_no_timestamps_fails(self) -> None:
        outcome = NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
        )
        ans = AgentAnswer(units=[_u_with_note('Ruby Martinez', 'u1', 'n1', None)])
        assert outcome.score(ans, _scenario())['pass'] == 0.0

    def test_subject_filter_picks_subject_relevant_newest(self) -> None:
        """F3 fix: subject_filter restricts the "newest" search to units
        matching at least one subject keyword. Without the filter, the
        latest-ingested unrelated unit wins; with it, the newest unit
        ABOUT the subject wins."""
        outcome = NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
            subject_filter=['Engineering'],
        )
        old_ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        mid_ts = _dt.datetime(2025, 6, 1, tzinfo=_dt.timezone.utc)
        new_ts = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
        ans = AgentAnswer(
            units=[
                _u_with_note('Alex Chen led Engineering', 'u1', 'n1', old_ts),
                _u_with_note('Ruby Martinez leads Engineering', 'u2', 'n2', mid_ts),
                _u_with_note(
                    'Yuki Tanaka leads Security', 'u3', 'n3', new_ts
                ),  # newest but off-topic
            ]
        )
        # Without subject_filter the newest unit is the Security one → fail.
        # With subject_filter=['Engineering'] only the first two are eligible
        # and the newest of those is Ruby's → pass.
        result = outcome.score(ans, _scenario())
        assert result['pass'] == 1.0
        assert result['subject_units'] == 2.0

    def test_empty_units_fails(self) -> None:
        outcome = NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
        )
        assert outcome.score(AgentAnswer(), _scenario()) == {'pass': 0.0}


class TestNoteAttribution:
    """P5 #3 — assert all units from top_note_key rank above all from
    lower_note_key. Robust to extraction paraphrasing — uses note_id, not
    text substrings."""

    def _ctx(self, mapping: dict[str, str]) -> dict:
        return {'_note_id_by_key': mapping}

    def test_top_above_lower_passes(self) -> None:
        outcome = NoteAttribution(
            type='note_attribution',
            top_note_key='ach',
            lower_note_key='inc',
        )
        ans = AgentAnswer(
            units=[
                _u_with_note('ach1', 'u1', 'note-ach', None),
                _u_with_note('ach2', 'u2', 'note-ach', None),
                _u_with_note('inc1', 'u3', 'note-inc', None),
                _u_with_note('inc2', 'u4', 'note-inc', None),
            ]
        )
        ctx = self._ctx({'ach': 'note-ach', 'inc': 'note-inc'})
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 1.0
        # Mean ranks: top {0,1} → 0.5; low {2,3} → 2.5. top dominates.
        assert result['top_mean_rank'] == 0.5
        assert result['low_mean_rank'] == 2.5
        assert result['top_count'] == 2.0
        assert result['low_count'] == 2.0

    def test_interleaved_passes_when_top_mean_dominates(self) -> None:
        """Round-9 fix: mean-rank dominance instead of strict partition.
        One interleaved low unit no longer fails the test."""
        outcome = NoteAttribution(
            type='note_attribution',
            top_note_key='ach',
            lower_note_key='inc',
        )
        ans = AgentAnswer(
            units=[
                _u_with_note('ach1', 'u1', 'note-ach', None),
                _u_with_note('inc1', 'u2', 'note-inc', None),  # interleaved
                _u_with_note('ach2', 'u3', 'note-ach', None),
                _u_with_note('inc2', 'u4', 'note-inc', None),
            ]
        )
        ctx = self._ctx({'ach': 'note-ach', 'inc': 'note-inc'})
        result = outcome.score(ans, _scenario(), context=ctx)
        # top mean = (0+2)/2 = 1.0; low mean = (1+3)/2 = 2.0. top < low → pass.
        assert result['pass'] == 1.0
        assert result['top_mean_rank'] == 1.0
        assert result['low_mean_rank'] == 2.0

    def test_top_below_lower_fails(self) -> None:
        outcome = NoteAttribution(
            type='note_attribution',
            top_note_key='ach',
            lower_note_key='inc',
        )
        ans = AgentAnswer(
            units=[
                _u_with_note('inc1', 'u1', 'note-inc', None),
                _u_with_note('inc2', 'u2', 'note-inc', None),
                _u_with_note('ach1', 'u3', 'note-ach', None),
            ]
        )
        ctx = self._ctx({'ach': 'note-ach', 'inc': 'note-inc'})
        result = outcome.score(ans, _scenario(), context=ctx)
        assert result['pass'] == 0.0  # top mean=2.0 vs low mean=0.5

    def test_missing_note_id_fails(self) -> None:
        outcome = NoteAttribution(
            type='note_attribution',
            top_note_key='ach',
            lower_note_key='inc',
        )
        ans = AgentAnswer(units=[])
        ctx = self._ctx({})  # neither resolved
        assert outcome.score(ans, _scenario(), context=ctx)['pass'] == 0.0

    def test_uuid_note_id_normalises_via_str(self) -> None:
        from uuid import uuid4 as _uuid

        outcome = NoteAttribution(
            type='note_attribution',
            top_note_key='ach',
            lower_note_key='inc',
        )
        # Mix UUID-typed note_id (DTO shape) and str (map shape) — both must
        # match via str() coercion.
        ach_uuid = _uuid()
        inc_uuid = _uuid()
        u1 = SimpleNamespace(
            text='ach', id='u1', note_id=ach_uuid, mentioned_at=None, occurred_start=None
        )
        u2 = SimpleNamespace(
            text='inc', id='u2', note_id=inc_uuid, mentioned_at=None, occurred_start=None
        )
        ans = AgentAnswer(units=[u1, u2])
        ctx = self._ctx({'ach': str(ach_uuid), 'inc': str(inc_uuid)})
        assert outcome.score(ans, _scenario(), context=ctx)['pass'] == 1.0


class TestNoteAssetsContain:
    """``NoteDTO.assets`` returns FileStore-relative paths
    ``assets/<vault>/<note-id>/<filename>``, NOT bare filenames. The
    score must compare basenames so the scenario contract
    (``expected_filenames=['system-diagram.png']``) works regardless
    of where the server places the bytes."""

    def _ctx(self, by_key: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
        return {'_note_assets_by_key': by_key}

    def test_basename_match_when_server_returns_full_paths(self) -> None:
        outcome = NoteAssetsContain(
            type='note_assets_contain',
            note_key='architecture-overview',
            expected_filenames=['system-diagram.png'],
        )
        ctx = self._ctx(
            {
                'architecture-overview': [
                    'assets/eval-suite-acme_corp-da7cd973/'
                    'fb2f7398b6fe1fc848dc38fdd1b5ebb7/system-diagram.png',
                ]
            }
        )
        result = outcome.score(AgentAnswer(units=[]), _scenario(), context=ctx)
        assert result == {'pass': 1.0, 'assets_found': 1.0}

    def test_basename_match_when_server_returns_bare_filenames(self) -> None:
        # Belt-and-braces: the score should also handle the legacy /
        # alternate shape where ``NoteDTO.assets`` returns bare filenames.
        outcome = NoteAssetsContain(
            type='note_assets_contain',
            note_key='nk',
            expected_filenames=['diagram.png'],
        )
        ctx = self._ctx({'nk': ['diagram.png']})
        result = outcome.score(AgentAnswer(units=[]), _scenario(), context=ctx)
        assert result == {'pass': 1.0, 'assets_found': 1.0}

    def test_missing_asset_fails(self) -> None:
        outcome = NoteAssetsContain(
            type='note_assets_contain',
            note_key='nk',
            expected_filenames=['missing.png'],
        )
        ctx = self._ctx({'nk': ['assets/v/n/other.png']})
        result = outcome.score(AgentAnswer(units=[]), _scenario(), context=ctx)
        assert result == {'pass': 0.0, 'assets_found': 0.0}

    def test_partial_match_fails_overall(self) -> None:
        outcome = NoteAssetsContain(
            type='note_assets_contain',
            note_key='nk',
            expected_filenames=['a.png', 'b.png'],
        )
        ctx = self._ctx({'nk': ['assets/v/n/a.png']})
        result = outcome.score(AgentAnswer(units=[]), _scenario(), context=ctx)
        # 1 of 2 found → fails the all-or-nothing pass, but assets_found tracks the partial.
        assert result == {'pass': 0.0, 'assets_found': 1.0}
