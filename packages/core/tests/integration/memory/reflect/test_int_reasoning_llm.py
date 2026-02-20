import pytest
import os
import dspy
import re
from uuid import uuid4, UUID
from datetime import datetime
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, col

from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.models import OpinionFormationRequest
from memex_core.memory.sql_models import MemoryUnit
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.retrieval.engine import RetrievalEngine


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_enforces_no_indices_simple(session: AsyncSession):
    """
    Verify that opinion reasoning does not contain 'Fact 0' or 'Memory 1'
    in a standard scenario.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    embedder = await get_embedding_model()
    reranker = await get_reranking_model()
    retrieval_engine = RetrievalEngine(embedder=embedder, reranker=reranker)

    engine = ReasoningEngine(
        session, lm, embedding_model=embedder, retrieval_engine=retrieval_engine
    )

    context_units = [
        MemoryUnit(
            id=uuid4(),
            text='User prefers dark mode in all applications.',
            event_date=datetime.now(),
        ),
        MemoryUnit(
            id=uuid4(),
            text='User mentioned that light mode gives them a headache.',
            event_date=datetime.now(),
        ),
    ]

    request = OpinionFormationRequest(
        query="What are the user's UI preferences?",
        context=context_units,
        answer='The user prefers dark mode because light mode causes headaches.',
        agent_name='tester',
    )

    opinion_ids = await engine.form_opinions(request)
    assert len(opinion_ids) > 0

    op_uuids = [UUID(oid) for oid in opinion_ids]
    results = await session.exec(select(MemoryUnit).where(col(MemoryUnit.id).in_(op_uuids)))
    stored_opinions = results.all()

    for op in stored_opinions:
        reasoning = op.unit_metadata.get('reasoning', '').lower()
        # Ensure no mentions of index-based references
        assert 'fact 0' not in reasoning
        assert 'fact 1' not in reasoning
        assert 'memory 0' not in reasoning
        assert 'memory 1' not in reasoning
        assert 'index 0' not in reasoning
        assert not re.search(r'\[\d+\]', reasoning), (
            f'Found bracketed index in reasoning: {reasoning}'
        )


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_explicit_index_bait(session: AsyncSession):
    """
    Adversarial Test: Provide input text that literally starts with "Fact 1:", "Item 2:".
    The model should NOT reference these numbers in its *generated* reasoning metadata,
    distinguishing content from structural indices.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    embedder = await get_embedding_model()
    reranker = await get_reranking_model()
    retrieval_engine = RetrievalEngine(embedder=embedder, reranker=reranker)

    engine = ReasoningEngine(
        session, lm, embedding_model=embedder, retrieval_engine=retrieval_engine
    )

    # The content itself mimics the forbidden format
    context_units = [
        MemoryUnit(
            id=uuid4(),
            text='Fact 1: The user strictly follows a vegan diet.',
            event_date=datetime.now(),
        ),
        MemoryUnit(
            id=uuid4(),
            text='Item 2: They are allergic to peanuts.',
            event_date=datetime.now(),
        ),
    ]

    request = OpinionFormationRequest(
        query="What are the user's dietary restrictions?",
        context=context_units,
        answer='The user is a vegan with a peanut allergy.',
        agent_name='tester',
    )

    opinion_ids = await engine.form_opinions(request)
    assert len(opinion_ids) > 0

    op_uuids = [UUID(oid) for oid in opinion_ids]
    results = await session.exec(select(MemoryUnit).where(col(MemoryUnit.id).in_(op_uuids)))
    stored_opinions = results.all()

    for op in stored_opinions:
        reasoning = op.unit_metadata.get('reasoning', '').lower()
        # It should reason about "vegan diet" and "allergies", not "Fact 1" or "Item 2" as sources
        assert 'fact 1' not in reasoning
        assert 'item 2' not in reasoning

        # Check for ANY relevant semantic content
        relevant_terms = [
            'vegan',
            'diet',
            'allergy',
            'peanuts',
            'restriction',
            'medical',
            'avoidance',
        ]
        assert any(term in reasoning for term in relevant_terms), (
            f'Reasoning lacks semantic content: {reasoning}'
        )


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_cross_entity_synthesis(session: AsyncSession):
    """
    Verify synthesis of opinion across distinct entities without fallback to lazy referencing.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    embedder = await get_embedding_model()
    reranker = await get_reranking_model()
    retrieval_engine = RetrievalEngine(embedder=embedder, reranker=reranker)

    engine = ReasoningEngine(
        session, lm, embedding_model=embedder, retrieval_engine=retrieval_engine
    )

    context_units = [
        MemoryUnit(id=uuid4(), text='Alice is the engineering manager.', event_date=datetime.now()),
        MemoryUnit(id=uuid4(), text='Bob reports to Alice.', event_date=datetime.now()),
        MemoryUnit(
            id=uuid4(), text='Bob feels overwhelmed by recent deadlines.', event_date=datetime.now()
        ),
    ]

    request = OpinionFormationRequest(
        query='What is the team dynamic?',
        context=context_units,
        answer='Bob, who reports to Alice, is feeling overwhelmed by deadlines.',
        agent_name='tester',
    )

    opinion_ids = await engine.form_opinions(request)
    assert len(opinion_ids) > 0

    op_uuids = [UUID(oid) for oid in opinion_ids]
    results = await session.exec(select(MemoryUnit).where(col(MemoryUnit.id).in_(op_uuids)))
    stored_opinions = results.all()

    for op in stored_opinions:
        reasoning = op.unit_metadata.get('reasoning', '').lower()
        # Reasoning should be semantic
        relevant_terms = [
            'alice',
            'manager',
            'bob',
            'deadlines',
            'overwhelmed',
            'capacity',
            'workload',
        ]
        assert any(term in reasoning for term in relevant_terms), (
            f'Reasoning lacks semantic content: {reasoning}'
        )
        assert 'fact' not in reasoning
