import pytest
from unittest.mock import MagicMock
import dspy
import datetime
from uuid import uuid4
from sqlmodel import select, col
from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.models import OpinionFormationRequest
from memex_core.memory.sql_models import MemoryUnit, EvidenceLog, Vault
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.asyncio
@pytest.mark.integration
async def test_opinion_revision_loop(session):
    """
    Test the Semantic Belief Revision loop:
    1. Reinforcement (Merge)
    2. Contradiction (Update Beta)

    CRITICAL: This test uses REAL vector search (pgvector) and REAL embeddings.
    It only mocks the LLM decision step (dspy).
    """

    # Setup: Create Vault
    vault_id = uuid4()
    vault = Vault(name='test_revision', id=vault_id)
    session.add(vault)
    await session.commit()

    # Setup: Create Initial Opinion
    embedder = await get_embedding_model()
    text = 'Confidence intervals are the primary tool for Frequentist uncertainty.'
    reasoning = 'Initial observation.'
    # Important: ReasoningEngine embeds "Statement\nReasoning: ..."
    text_to_embed = f'{text}\nReasoning: {reasoning}'
    emb = embedder.encode([text_to_embed])[0].tolist()

    op1 = MemoryUnit(
        id=uuid4(),
        vault_id=vault_id,
        text=text,
        embedding=emb,
        fact_type=FactTypes.OPINION,
        confidence_alpha=2.0,
        confidence_beta=0.0,
        event_date=datetime.datetime.now(datetime.timezone.utc),
        mentioned_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(op1)
    await session.commit()
    op1_id = op1.id

    # ------------------------------------------------------------------
    # TEST 1: REINFORCEMENT
    # ------------------------------------------------------------------

    from unittest.mock import patch

    class MockRelResult:
        relationship = 'reinforces'
        reasoning = 'Same core belief.'

    # We mock ONLY the LLM decision. Retrieval is REAL.
    with patch('memex_core.memory.reflect.reasoning.run_dspy_operation') as mock_dspy:

        class MockFormedOpinion:
            # Identical text to ensure high similarity (>0.60)
            statement = 'Confidence intervals are the primary tool for Frequentist uncertainty.'
            reasoning = 'User confirmation.'
            confidence_score = 0.9
            entities = []
            evidence_indices = []

        class MockFormationResponse:
            formed_opinions = [MockFormedOpinion()]

        async def side_effect(lm, predictor, input_kwargs, session, context_metadata, **kwargs):
            op_type = context_metadata.get('operation')
            if op_type == 'form_opinions':
                return MockFormationResponse(), {}
            if op_type == 'opinion_rel':
                # Check inputs
                new_stmt = input_kwargs.get('new_statement', '')
                if 'Confidence' in new_stmt:
                    res = MockRelResult()
                    res.relationship = 'reinforces'
                    return res, {}
            return None, {}

        mock_dspy.side_effect = side_effect
        mock_lm = MagicMock(spec=dspy.LM)

        from memex_core.memory.retrieval.engine import RetrievalEngine

        retrieval_engine = RetrievalEngine(embedder=embedder)

        engine = ReasoningEngine(
            session, mock_lm, embedding_model=embedder, retrieval_engine=retrieval_engine
        )
        req = OpinionFormationRequest(query='test', context=[], answer='test', vault_id=vault_id)

        await engine.form_opinions(req)

        # Verify Log
        logs = (
            await session.exec(select(EvidenceLog).where(col(EvidenceLog.unit_id) == op1_id))
        ).all()
        assert len(logs) == 1
        assert logs[0].evidence_type == 'opinion_reinforced'

        # Verify Alpha Increased
        await session.refresh(op1)
        assert (op1.confidence_alpha or 0.0) > 2.0

    # ------------------------------------------------------------------
    # TEST 2: CONTRADICTION
    # ------------------------------------------------------------------

    # We define the contradiction text.
    # Known similarity to op1 is ~0.65 (calculated via script).
    # Threshold is now 0.60, so this MUST be retrieved by pgvector.
    contradiction_text = 'Credible intervals are actually better.'

    with patch('memex_core.memory.reflect.reasoning.run_dspy_operation') as mock_dspy:

        class MockFormedOpinionContradict:
            statement = contradiction_text
            reasoning = 'User correction.'
            confidence_score = 0.9
            entities = []
            evidence_indices = []

        class MockFormationResponseContradict:
            formed_opinions = [MockFormedOpinionContradict()]

        async def side_effect_contradict(
            lm, predictor, input_kwargs, session, context_metadata, **kwargs
        ):
            op_type = context_metadata.get('operation')
            if op_type == 'form_opinions':
                return MockFormationResponseContradict(), {}
            if op_type == 'opinion_rel':
                # The ReasoningEngine should have retrieved op1 and passed its text here.
                # We verify that correct "Contradicts" logic is triggered.
                res = MockRelResult()
                res.relationship = 'contradicts'
                return res, {}
            return None, {}

        mock_dspy.side_effect = side_effect_contradict

        await engine.form_opinions(req)

        # Verify Log
        logs = (
            await session.exec(
                select(EvidenceLog)
                .where(col(EvidenceLog.unit_id) == op1_id)
                .order_by(col(EvidenceLog.created_at))
            )
        ).all()
        assert len(logs) == 2
        assert logs[1].evidence_type == 'opinion_contradicted'

        # Verify Beta Increased
        await session.refresh(op1)
        assert (op1.confidence_beta or 0.0) >= 1.8
