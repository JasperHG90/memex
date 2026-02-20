import pytest
import os
import dspy
from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.models import OpinionFormationRequest
from memex_core.memory.sql_models import MemoryUnit
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_engine_opinion_formation_end_to_end(session: AsyncSession):
    """
    Integration test for ReasoningEngine using real Gemini and real Postgres.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    # Using flash model for faster/cheaper integration test
    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    embedder = await get_embedding_model()
    reranker = await get_reranking_model()
    retrieval_engine = RetrievalEngine(embedder=embedder, reranker=reranker)

    engine = ReasoningEngine(
        session, lm, embedding_model=embedder, retrieval_engine=retrieval_engine
    )

    # Input for a hypothetical interaction where an opinion should be formed.
    query = 'Should I use Polars or Pandas for processing 100GB of CSV files?'
    from datetime import datetime
    from uuid import uuid4

    context_units = [
        MemoryUnit(
            id=uuid4(),
            text='Polars is a multi-threaded query engine written in Rust.',
            event_date=datetime.now(),
        ),
        MemoryUnit(
            id=uuid4(),
            text="Pandas is single-threaded and often limited by Python's GIL.",
            event_date=datetime.now(),
        ),
        MemoryUnit(
            id=uuid4(),
            text='Polars uses Apache Arrow for memory efficiency.',
            event_date=datetime.now(),
        ),
        MemoryUnit(
            id=uuid4(),
            text='Pandas can be slow with very large datasets due to memory overhead.',
            event_date=datetime.now(),
        ),
    ]
    answer = 'Based on the efficiency of Apache Arrow and its multi-threaded Rust core, Polars is significantly better than Pandas for 100GB datasets.'

    # Run opinion formation
    request = OpinionFormationRequest(query=query, context=context_units, answer=answer)
    unit_ids = await engine.form_opinions(request)

    # 1. Check if opinions were created
    assert len(unit_ids) > 0

    # 2. Verify existence in DB
    result = await session.exec(select(MemoryUnit).where(MemoryUnit.fact_type == 'opinion'))
    opinions = result.all()

    assert len(opinions) >= 1

    # 3. Verify content
    found_polars = False
    for op in opinions:
        text_lower = op.text.lower()
        if 'polars' in text_lower:
            found_polars = True
            # Verify Bayesian priors were set
            assert op.confidence_alpha is not None
            assert op.confidence_beta is not None
            # Check metadata/payload
            assert 'reasoning' in op.unit_metadata
            # The entities list in payload should contain polars or pandas
            entities = op.unit_metadata.get('entities', [])
            assert any('polars' in e.lower() for e in entities) or any(
                'pandas' in e.lower() for e in entities
            )

    assert found_polars, 'Should have formed an opinion about Polars preference.'


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_engine_deduplication_merge(session: AsyncSession):
    """
    Test that forming the same opinion twice results in a merge (Bayesian update)
    rather than a duplicate entry.
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

    query = 'Is Rust better than C++ for memory safety?'
    from datetime import datetime
    from uuid import uuid4

    context_units = [
        MemoryUnit(id=uuid4(), text='Rust has a borrow checker.', event_date=datetime.now()),
        MemoryUnit(
            id=uuid4(), text='C++ allows manual memory management.', event_date=datetime.now()
        ),
    ]
    answer = 'Yes, Rust is strictly better for memory safety due to its compile-time guarantees.'

    req = OpinionFormationRequest(query=query, context=context_units, answer=answer)

    # 1. First Pass
    ids_1 = await engine.form_opinions(req)
    assert len(ids_1) >= 1

    # Track initial alphas
    initial_alphas = {}
    for uid in ids_1:
        unit = await session.get(MemoryUnit, uid)
        assert unit is not None
        initial_alphas[uid] = unit.confidence_alpha

    # 2. Second Pass (Same input -> Same opinions)
    ids_2 = await engine.form_opinions(req)
    assert len(ids_2) == len(ids_1)

    # 3. Assertions
    # IDs should be identical (all merged)
    assert set(ids_1) == set(ids_2), 'Should have returned the same Unit IDs (merge)'

    # Refresh to check updated confidence for at least one
    for uid in ids_1:
        unit = await session.get(MemoryUnit, uid)
        assert unit is not None
        assert unit.confidence_alpha > initial_alphas[uid], (
            f'Confidence alpha for {uid} should have increased'
        )
        assert unit.access_count >= 1


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reasoning_engine_merge_similar_phrasing(session: AsyncSession):
    """
    Test that opinions with slightly different wording but high semantic similarity
    are merged into a single belief, accumulating confidence.
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

    # 1. Interaction A: "Polars is faster"
    query_a = 'Is Polars fast?'
    from datetime import datetime
    from uuid import uuid4

    context_a = [MemoryUnit(id=uuid4(), text='Polars uses Arrow.', event_date=datetime.now())]
    answer_a = 'Polars is significantly faster than Pandas for large datasets.'

    ids_a = await engine.form_opinions(
        OpinionFormationRequest(query=query_a, context=context_a, answer=answer_a)
    )
    assert len(ids_a) >= 1
    id_a = ids_a[0]

    unit_a = await session.get(MemoryUnit, id_a)
    assert unit_a is not None
    initial_alpha = unit_a.confidence_alpha

    # 2. Interaction B: "Polars has better speed" (Semantically similar)
    # We craft this to likely yield a similar embedding but different text
    query_b = 'How does Polars speed compare to Pandas?'
    context_b = [
        MemoryUnit(id=uuid4(), text='Pandas is single threaded.', event_date=datetime.now())
    ]
    answer_b = 'Polars has much better speed performance than Pandas on big data.'

    ids_b = await engine.form_opinions(
        OpinionFormationRequest(query=query_b, context=context_b, answer=answer_b)
    )
    assert len(ids_b) >= 1
    id_b = ids_b[0]

    # 3. Assertions
    # If the embeddings are close enough (>0.92), this should be a merge.
    # Note: 0.92 is a high bar. If this fails, it means the embeddings weren't close enough,
    # which is a useful calibration signal for our threshold.
    assert id_a == id_b, f'Expected merge for similar opinions. IDs differed: {id_a} vs {id_b}'

    await session.refresh(unit_a)
    assert unit_a is not None
    assert unit_a.confidence_alpha is not None
    assert initial_alpha is not None
    assert unit_a.confidence_alpha > initial_alpha, (
        'Confidence should accumulate across similar phrasings'
    )
