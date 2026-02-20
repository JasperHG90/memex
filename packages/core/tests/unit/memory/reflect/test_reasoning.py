import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import dspy
from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.prompts import FormedOpinion
from memex_core.memory.reflect.models import OpinionFormationRequest


@pytest.fixture
def mock_lm():
    return MagicMock(spec=dspy.LM)


@pytest.fixture
def mock_session():
    mock = AsyncMock()
    # Create a MagicMock for the result object (synchronous .all())
    mock_result = MagicMock()
    mock_result.all.return_value = [('unit-uuid-1',)]

    # session.exec is async, so return_value is what 'await session.exec()' returns
    mock.exec = AsyncMock(return_value=mock_result)

    # session.add is synchronous
    mock.add = MagicMock()
    return mock


@pytest.mark.asyncio
async def test_form_opinions_success(mock_session, mock_lm):
    """Test successful opinion formation."""

    # Mock embedding model
    with (
        patch('memex_core.memory.reflect.reasoning.get_retrieval_engine') as mock_get_retrieval,
        patch(
            'memex_core.memory.reflect.reasoning.storage.insert_facts_batch', new_callable=AsyncMock
        ) as mock_insert,
    ):
        mock_embed_instance = MagicMock()
        # Mock numpy array behavior
        mock_array = MagicMock()
        mock_array.tolist.return_value = [0.1, 0.2, 0.3]
        mock_embed_instance.encode.return_value = [mock_array]

        mock_retrieval = AsyncMock()
        mock_retrieval.retrieve.return_value = []  # No duplicates
        mock_get_retrieval.return_value = mock_retrieval
        mock_insert.return_value = ['unit-uuid-1']

        engine = ReasoningEngine(
            mock_session,
            mock_lm,
            embedding_model=mock_embed_instance,
            retrieval_engine=mock_retrieval,
        )
        # Mock dspy.ChainOfThought
        with patch('memex_core.memory.reflect.reasoning.dspy.ChainOfThought') as MockCoT:
            mock_predictor = MagicMock()

            mock_opinion = FormedOpinion(
                statement='Test Opinion',
                reasoning='Because logic',
                evidence_indices=[0],
                confidence_score=0.8,
                entities=['EntityA'],
            )

            # The predictor instance has an .acall method that returns the result
            mock_result = MagicMock()
            mock_result.formed_opinions = [mock_opinion]
            mock_predictor.acall = AsyncMock(return_value=mock_result)

            MockCoT.return_value = mock_predictor

            from memex_core.memory.sql_models import MemoryUnit
            from datetime import datetime

            u1 = MemoryUnit(
                id='00000000-0000-0000-0000-000000000001', text='Fact 1', event_date=datetime.now()
            )
            # formatted_fact_text property uses occurred_start or event_date logic?
            # Looking at sql_models.py: formatted_fact_text uses occurred_start if available.
            # u1.formatted_fact_text property needs to work.
            # Let's mock it or set occurred_start.
            u1.occurred_start = datetime(2023, 1, 1)

            request = OpinionFormationRequest(query='query', context=[u1], answer='answer')
            unit_ids = await engine.form_opinions(request)
            assert unit_ids == ['unit-uuid-1']
            mock_insert.assert_called_once()

            # Verify evidence resolution (UUID restoration)
            # The FormedOpinion passed to mock_insert should have real UUIDs
            # But mock_insert receives ProcessedFact.
            # We can inspect the calls to mock_insert to see if payload has resolved IDs
            call_args = mock_insert.call_args[0][1]  # list of facts
            assert len(call_args) == 1
            fact = call_args[0]
            assert fact.payload['evidence_indices'] == ['00000000-0000-0000-0000-000000000001']

            # Check embedding was called with statement ONLY
            mock_embed_instance.encode.assert_called()
            args, _ = mock_embed_instance.encode.call_args
            assert args[0][0] == 'Test Opinion'
            assert 'Because logic' not in args[0][0]


@pytest.mark.asyncio
async def test_form_opinions_no_opinions(mock_session, mock_lm):
    """Test when no opinions are formed."""

    with (
        patch('memex_core.memory.reflect.reasoning.get_retrieval_engine') as mock_get_retrieval,
    ):
        mock_embed_instance = MagicMock()
        mock_retrieval = AsyncMock()
        mock_get_retrieval.return_value = mock_retrieval

        engine = ReasoningEngine(
            mock_session,
            mock_lm,
            embedding_model=mock_embed_instance,
            retrieval_engine=mock_retrieval,
        )
        with patch('memex_core.memory.reflect.reasoning.dspy.ChainOfThought') as MockCoT:
            mock_predictor = MagicMock()
            mock_result = MagicMock()
            mock_result.formed_opinions = []
            mock_predictor.acall = AsyncMock(return_value=mock_result)
            MockCoT.return_value = mock_predictor

            request = OpinionFormationRequest(query='query', context=[], answer='answer')
            unit_ids = await engine.form_opinions(request)
            assert unit_ids == []
            mock_session.exec.assert_not_called()
