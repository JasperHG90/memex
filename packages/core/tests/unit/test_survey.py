"""Unit tests for the memex_survey feature (Task #6).

Tests cover:
- SurveyDecomposer: decomposition, clamping [3,5], fallback on LLM failure
- SearchService.survey(): dedup by memory unit ID, grouping by note
- Token budget truncation and `truncated` flag
- Empty results handling
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import dspy
import pytest

from memex_common.schemas import SurveyFact, SurveyResponse, SurveyTopic


# ---------------------------------------------------------------------------
# SurveyDecomposer tests
# ---------------------------------------------------------------------------


class TestSurveyDecomposer:
    """Tests for SurveyDecompositionSignature and SurveyDecomposer."""

    @pytest.fixture
    def decomposer(self):
        from memex_core.memory.retrieval.expansion import SurveyDecomposer

        lm = MagicMock(spec=dspy.LM)
        return SurveyDecomposer(lm)

    async def test_decompose_returns_sub_questions(self, decomposer):
        """Normal case: LLM returns 3-5 sub-questions."""
        result = SimpleNamespace(
            sub_questions=[
                'What tools does the team use?',
                'What processes are in place?',
                'Who are the key stakeholders?',
            ]
        )
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=result,
        ):
            questions = await decomposer.decompose('What do you know about the team?')
        assert len(questions) == 3
        assert questions[0] == 'What tools does the team use?'

    async def test_decompose_clamps_to_max_5(self, decomposer):
        """If LLM returns >5 sub-questions, truncate to 5."""
        result = SimpleNamespace(sub_questions=[f'Question {i}' for i in range(8)])
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=result,
        ):
            questions = await decomposer.decompose('topic')
        assert len(questions) == 5

    async def test_decompose_pads_to_min_3(self, decomposer):
        """If LLM returns <3 sub-questions, pad with rephrases to 3."""
        result = SimpleNamespace(sub_questions=['Only one question'])
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=result,
        ):
            questions = await decomposer.decompose('broad topic')
        assert len(questions) == 3
        assert questions[0] == 'Only one question'
        # Padded questions should contain original topic
        assert 'broad topic' in questions[1]
        assert 'broad topic' in questions[2]

    async def test_decompose_fallback_on_failure(self, decomposer):
        """If LLM fails, fall back to [original_query]."""
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            side_effect=RuntimeError('LLM unavailable'),
        ):
            questions = await decomposer.decompose('my query')
        assert questions == ['my query']

    async def test_decompose_fallback_on_empty_result(self, decomposer):
        """If LLM returns empty list, fall back to [original_query]."""
        result = SimpleNamespace(sub_questions=[])
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=result,
        ):
            questions = await decomposer.decompose('my query')
        assert questions == ['my query']

    async def test_decompose_filters_empty_strings(self, decomposer):
        """Empty strings in LLM output should be filtered before clamping."""
        result = SimpleNamespace(sub_questions=['Q1', '', '  ', 'Q2'])
        with patch(
            'memex_core.memory.retrieval.expansion.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=result,
        ):
            questions = await decomposer.decompose('topic')
        # 2 valid questions, padded to 3
        assert len(questions) == 3
        assert questions[0] == 'Q1'
        assert questions[1] == 'Q2'


# ---------------------------------------------------------------------------
# SearchService.survey() tests
# ---------------------------------------------------------------------------


def _make_memory_unit(
    unit_id: UUID | None = None,
    note_id: UUID | None = None,
    text: str = 'some fact',
    fact_type: str = 'world',
    score: float = 0.5,
) -> MagicMock:
    """Create a mock MemoryUnit-like object."""
    unit = MagicMock()
    unit.id = unit_id or uuid4()
    unit.note_id = note_id
    unit.text = text
    unit.fact_type = fact_type
    unit.score = score
    return unit


class TestSearchServiceSurvey:
    """Tests for SearchService.survey() method."""

    @pytest.fixture
    def search_service(self, mock_metastore, mock_config):
        from memex_core.services.search import SearchService

        lm = MagicMock(spec=dspy.LM)
        memory = AsyncMock()
        doc_search = AsyncMock()
        vaults = AsyncMock()
        vaults.resolve_vault_identifier = AsyncMock(return_value=uuid4())

        svc = SearchService(
            metastore=mock_metastore,
            config=mock_config,
            lm=lm,
            memory=memory,
            doc_search=doc_search,
            vaults=vaults,
        )
        return svc

    async def test_survey_dedup_by_unit_id(self, search_service, mock_session):
        """Duplicate memory units across sub-queries are deduplicated by ID."""
        shared_id = uuid4()
        note_id = uuid4()

        unit1 = _make_memory_unit(unit_id=shared_id, note_id=note_id, score=0.6)
        unit2 = _make_memory_unit(unit_id=shared_id, note_id=note_id, score=0.8)
        unit3 = _make_memory_unit(note_id=note_id, score=0.5)

        # Mock decomposer to return 3 sub-queries
        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            # Each sub-query returns overlapping units
            search_service.memory.recall = AsyncMock(
                side_effect=[
                    ([unit1, unit3], None),
                    ([unit2], None),
                    ([], None),
                ]
            )

            # Mock note title resolution
            mock_result = MagicMock()
            mock_result.all.return_value = [(note_id, 'Test Note')]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='broad topic',
                vault_ids=[uuid4()],
            )

        # Should have 2 unique units (shared_id kept with higher score + unit3)
        assert result.total_facts == 2
        assert result.total_notes == 1

    async def test_survey_groups_by_note(self, search_service, mock_session):
        """Facts are grouped by their source note_id."""
        note_a = uuid4()
        note_b = uuid4()

        units = [
            _make_memory_unit(note_id=note_a, text='fact A1', score=0.9),
            _make_memory_unit(note_id=note_a, text='fact A2', score=0.7),
            _make_memory_unit(note_id=note_b, text='fact B1', score=0.8),
        ]

        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(
                side_effect=[
                    (units, None),
                    ([], None),
                    ([], None),
                ]
            )

            mock_result = MagicMock()
            mock_result.all.return_value = [
                (note_a, 'Note A'),
                (note_b, 'Note B'),
            ]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='broad topic',
                vault_ids=[uuid4()],
            )

        assert result.total_notes == 2
        assert result.total_facts == 3

        # Topics are sorted by total score descending
        # note_a has 0.9 + 0.7 = 1.6, note_b has 0.8
        assert result.topics[0].note_id == note_a
        assert result.topics[0].fact_count == 2
        assert result.topics[1].note_id == note_b
        assert result.topics[1].fact_count == 1

    async def test_survey_token_budget_truncation(self, search_service, mock_session):
        """Token budget truncation drops low-score facts and sets truncated=True."""
        note_id = uuid4()

        # Create units with known text lengths
        units = [
            _make_memory_unit(note_id=note_id, text='a' * 400, score=0.9),  # ~100 tokens
            _make_memory_unit(note_id=note_id, text='b' * 400, score=0.7),  # ~100 tokens
            _make_memory_unit(note_id=note_id, text='c' * 400, score=0.5),  # ~100 tokens
        ]

        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(
                side_effect=[
                    (units, None),
                    ([], None),
                    ([], None),
                ]
            )

            mock_result = MagicMock()
            mock_result.all.return_value = [(note_id, 'Test Note')]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='broad topic',
                vault_ids=[uuid4()],
                token_budget=150,  # Only fits ~1.5 units
            )

        assert result.truncated is True
        # Should have kept only the first unit (100 tokens fits in 150)
        assert result.total_facts == 1

    async def test_survey_no_truncation_without_budget(self, search_service, mock_session):
        """Without token_budget, all results are returned and truncated=False."""
        note_id = uuid4()
        units = [_make_memory_unit(note_id=note_id, text='fact', score=0.5)]

        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(
                side_effect=[
                    (units, None),
                    ([], None),
                    ([], None),
                ]
            )

            mock_result = MagicMock()
            mock_result.all.return_value = [(note_id, 'Test')]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='topic',
                vault_ids=[uuid4()],
            )

        assert result.truncated is False
        assert result.total_facts == 1

    async def test_survey_empty_results(self, search_service, mock_session):
        """Gracefully handles empty results from all sub-queries."""
        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(return_value=([], None))

            result = await search_service.survey(
                query='unknown topic',
                vault_ids=[uuid4()],
            )

        assert result.topics == []
        assert result.total_notes == 0
        assert result.total_facts == 0
        assert result.truncated is False
        assert result.query == 'unknown topic'
        assert len(result.sub_queries) == 3

    async def test_survey_keeps_higher_score_on_dedup(self, search_service, mock_session):
        """When deduplicating, the unit with the higher score is kept."""
        shared_id = uuid4()
        note_id = uuid4()

        unit_low = _make_memory_unit(unit_id=shared_id, note_id=note_id, score=0.3)
        unit_high = _make_memory_unit(unit_id=shared_id, note_id=note_id, score=0.9)

        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(
                side_effect=[
                    ([unit_low], None),
                    ([unit_high], None),
                    ([], None),
                ]
            )

            mock_result = MagicMock()
            mock_result.all.return_value = [(note_id, 'Test')]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='topic',
                vault_ids=[uuid4()],
            )

        assert result.total_facts == 1
        assert result.topics[0].facts[0].score == 0.9

    async def test_survey_response_schema(self, search_service, mock_session):
        """Verify the complete output schema matches spec."""
        note_id = uuid4()
        unit = _make_memory_unit(note_id=note_id, text='a fact', fact_type='world', score=0.8)

        with patch('memex_core.services.search.SurveyDecomposer') as mock_decomposer_cls:
            mock_decomposer = AsyncMock()
            mock_decomposer.decompose = AsyncMock(return_value=['q1', 'q2', 'q3'])
            mock_decomposer_cls.return_value = mock_decomposer

            search_service.memory.recall = AsyncMock(
                side_effect=[
                    ([unit], None),
                    ([], None),
                    ([], None),
                ]
            )

            mock_result = MagicMock()
            mock_result.all.return_value = [(note_id, 'My Note')]
            mock_session.exec.return_value = mock_result

            result = await search_service.survey(
                query='data and analytics',
                vault_ids=[uuid4()],
            )

        # Verify full schema
        assert isinstance(result, SurveyResponse)
        assert result.query == 'data and analytics'
        assert result.sub_queries == ['q1', 'q2', 'q3']
        assert len(result.topics) == 1

        topic = result.topics[0]
        assert isinstance(topic, SurveyTopic)
        assert topic.note_id == note_id
        assert topic.title == 'My Note'
        assert topic.fact_count == 1

        fact = topic.facts[0]
        assert isinstance(fact, SurveyFact)
        assert fact.text == 'a fact'
        assert fact.fact_type == 'world'
        assert fact.score == 0.8

        # Serializable
        data = result.model_dump()
        assert 'query' in data
        assert 'sub_queries' in data
        assert 'topics' in data
        assert 'total_notes' in data
        assert 'total_facts' in data
        assert 'truncated' in data


# ---------------------------------------------------------------------------
# Schema model tests
# ---------------------------------------------------------------------------


class TestSurveySchemas:
    """Tests for the survey schema models."""

    def test_survey_request_validation(self):
        from memex_common.schemas import SurveyRequest

        req = SurveyRequest(query='my topic')
        assert req.query == 'my topic'
        assert req.limit_per_query == 10
        assert req.token_budget is None
        assert req.vault_ids is None

    def test_survey_response_empty(self):
        resp = SurveyResponse(
            query='nothing',
            sub_queries=['q1', 'q2', 'q3'],
        )
        assert resp.topics == []
        assert resp.total_notes == 0
        assert resp.total_facts == 0
        assert resp.truncated is False

    def test_survey_fact_model(self):
        fact = SurveyFact(
            id=uuid4(),
            text='test fact',
            fact_type='world',
            score=0.95,
        )
        assert fact.text == 'test fact'
        assert fact.score == 0.95

    def test_survey_topic_model(self):
        topic = SurveyTopic(
            note_id=uuid4(),
            title='My Note',
            fact_count=0,
        )
        assert topic.facts == []
        assert topic.fact_count == 0
