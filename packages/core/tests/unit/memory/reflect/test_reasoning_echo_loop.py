import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import dspy
from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.prompts import FormedOpinion
from memex_core.memory.reflect.models import OpinionFormationRequest
from memex_core.memory.sql_models import MemoryUnit
from datetime import datetime


@pytest.fixture
def mock_lm():
    return MagicMock(spec=dspy.LM)


@pytest.fixture
def mock_session():
    mock = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock.exec = AsyncMock(return_value=mock_result)
    mock.add = MagicMock()
    return mock


@pytest.mark.asyncio
async def test_context_echo_prevention(mock_session, mock_lm):
    """
    Test that the system prioritizes current interaction over historical context.
    Scenario:
    - Context: "Entity resolution is perfect." (Old Belief)
    - Query: "Entity resolution is actually failing." (New Contradiction)
    - Expected: Formed opinion should reflect the failure, NOT the perfection.
    """

    with (
        patch('memex_core.memory.reflect.reasoning.get_retrieval_engine') as mock_get_retrieval,
        patch(
            'memex_core.memory.reflect.reasoning.storage.insert_facts_batch', new_callable=AsyncMock
        ) as mock_insert,
    ):
        # Mock dependencies
        mock_embed_instance = MagicMock()
        mock_array = MagicMock()
        mock_array.tolist.return_value = [0.1, 0.2, 0.3]
        mock_embed_instance.encode.return_value = [mock_array]

        mock_retrieval = AsyncMock()
        mock_retrieval.retrieve.return_value = []
        mock_get_retrieval.return_value = mock_retrieval
        mock_insert.return_value = ['new-unit-id']

        engine = ReasoningEngine(
            mock_session,
            mock_lm,
            embedding_model=mock_embed_instance,
            retrieval_engine=mock_retrieval,
        )
        # Mock the DSPy ChainOfThought to simulate the LLM's response
        with patch('memex_core.memory.reflect.reasoning.dspy.ChainOfThought') as MockCoT:
            mock_predictor = MagicMock()

            # Simulate the LLM correctly ignoring the context echo
            mock_formed_opinion = FormedOpinion(
                statement='Entity resolution is failing.',
                reasoning='User explicitly stated it is failing, contradicting the old belief.',
                evidence_indices=[],  # No index from context as it contradicts it
                confidence_score=0.9,
                entities=['Entity Resolution'],
            )

            mock_result = MagicMock()
            mock_result.formed_opinions = [mock_formed_opinion]
            mock_predictor.acall = AsyncMock(return_value=mock_result)
            MockCoT.return_value = mock_predictor

            # Context contains the OLD belief
            old_belief = MemoryUnit(
                id='11111111-1111-1111-1111-111111111111',
                text='Entity resolution is perfect and bug-free.',
                event_date=datetime.now(),
                fact_type='opinion',
            )
            old_belief.occurred_start = datetime(2025, 1, 1)

            request = OpinionFormationRequest(
                query='Entity resolution is actually failing badly.',
                context=[old_belief],
                answer="I understand, let's fix the entity resolution failure.",
            )

            # Execution
            await engine.form_opinions(request)

            # Verification 1: Check that the input to the LLM had the [Historical Context] tag
            # We need to inspect the 'context' argument passed to predictor.acall
            call_kwargs = mock_predictor.acall.call_args[1]
            context_arg = call_kwargs['context']

            assert len(context_arg) == 1
            assert '[Historical Context]:' in context_arg[0]
            assert 'Entity resolution is perfect' in context_arg[0]

            # Verification 2: Check that the correct opinion was attempted to be stored
            # The engine should process the 'failing' opinion
            mock_insert.assert_called_once()
            facts_to_store = mock_insert.call_args[0][1]
            assert facts_to_store[0].fact_text == 'Entity resolution is failing.'


@pytest.mark.asyncio
async def test_relationship_contradiction_logic(mock_session, mock_lm):
    """
    Test that the relationship logic correctly identifies contradictions
    when the prompt logic works as intended.
    """
    with (
        patch('memex_core.memory.reflect.reasoning.get_retrieval_engine') as mock_get_retrieval,
        patch(
            'memex_core.memory.reflect.reasoning.ConfidenceEngine.apply_custom_update',
            new_callable=AsyncMock,
        ) as mock_confidence,
        patch(
            'memex_core.memory.reflect.reasoning.storage.insert_facts_batch', new_callable=AsyncMock
        ),
    ):
        mock_embed_instance = MagicMock()
        mock_array = MagicMock()
        mock_array.tolist.return_value = [0.1]
        mock_embed_instance.encode.return_value = [mock_array]

        # Scenario: We form a new opinion "Sky is Green"
        # Database has "Sky is Blue"
        existing_unit = MemoryUnit(
            id='22222222-2222-2222-2222-222222222222',
            text='The sky is blue.',
            event_date=datetime.now(),
            fact_type='opinion',
        )

        mock_retrieval = AsyncMock()
        mock_retrieval.retrieve.return_value = [existing_unit]
        mock_get_retrieval.return_value = mock_retrieval

        engine = ReasoningEngine(
            mock_session,
            mock_lm,
            embedding_model=mock_embed_instance,
            retrieval_engine=mock_retrieval,
        )
        # Mock the OPINION FORMATION step
        with patch('memex_core.memory.reflect.reasoning.dspy.ChainOfThought') as MockCoT:
            mock_predictor = MagicMock()
            mock_op = FormedOpinion(
                statement='The sky is green.',
                reasoning='I saw it.',
                evidence_indices=[],
                confidence_score=0.9,
                entities=[],
            )
            mock_result = MagicMock()
            mock_result.formed_opinions = [mock_op]
            mock_predictor.acall = AsyncMock(return_value=mock_result)
            MockCoT.return_value = mock_predictor

            # Mock the RELATIONSHIP CHECK step
            # We must mock dspy.Predict separately since it's instantiated inside the method loop
            # BUT dspy.Predict is a class. We can patch it.
            with patch('memex_core.memory.reflect.reasoning.dspy.Predict') as MockPredict:
                mock_rel_predictor = MagicMock()
                mock_rel_result = MagicMock()
                # Crucial: The LLM says it CONTRADICTS
                mock_rel_result.relationship = 'contradicts'
                mock_rel_result.reasoning = 'Green opposes Blue'
                mock_rel_predictor.acall = AsyncMock(return_value=mock_rel_result)
                MockPredict.return_value = mock_rel_predictor

                request = OpinionFormationRequest(query='q', context=[], answer='a')
                await engine.form_opinions(request)

                # Verification:
                # Should have called confidence update with beta_delta > 0 (contradiction)
                mock_confidence.assert_called_once()
                call_kwargs = mock_confidence.call_args[1]
                assert call_kwargs['evidence_type'] == 'opinion_contradicted'
                assert call_kwargs['alpha_delta'] == 0
                assert call_kwargs['beta_delta'] > 0  # Should add mass to beta
