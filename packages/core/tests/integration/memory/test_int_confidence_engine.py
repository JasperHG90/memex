import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.sql_models import MemoryUnit
from memex_common.types import FactTypes
from memex_core.memory.confidence import ConfidenceEngine
from memex_core.memory.models.embedding import get_embedding_model
import datetime as dt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confidence_engine_informative_prior(session: AsyncSession):
    # 1. Setup
    embedding_model = await get_embedding_model()
    engine = ConfidenceEngine(damping_factor=0.2, max_inherited_mass=20.0)

    # 2. Insert an existing high-confidence Opinion
    text_1 = 'I believe that Python is the best language for AI.'
    embedding_1 = embedding_model.encode([text_1])[0]

    unit_1 = MemoryUnit(
        text=text_1,
        embedding=embedding_1,
        event_date=dt.datetime.now(dt.timezone.utc),
        fact_type=FactTypes.OPINION,
        confidence_alpha=50.0,  # High confidence
        confidence_beta=2.0,
        access_count=0,
    )
    session.add(unit_1)
    await session.commit()

    # 3. Calculate prior for a similar opinion
    text_2 = 'I believe Python is the best language for AI.'
    embedding_2 = embedding_model.encode([text_2])[0]

    alpha, beta = await engine.calculate_informative_prior(session, embedding_2)

    # Logic:
    # Initial (1, 1)
    # Inherited: 50 * ~0.9 (similarity) * 0.2 (damping) = ~9.0
    # Expected alpha > 5.0
    assert alpha > 5.0
    assert beta > 1.0
    assert alpha > beta

    # 4. Calculate prior for a completely different topic
    text_3 = 'The moon is made of green cheese.'
    embedding_3 = embedding_model.encode([text_3])[0]

    alpha_3, beta_3 = await engine.calculate_informative_prior(session, embedding_3)
    # Should be close to (1.0, 1.0)
    assert alpha_3 == pytest.approx(1.0, abs=0.5)
    assert beta_3 == pytest.approx(1.0, abs=0.5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confidence_engine_inheritance_from_world_fact(session: AsyncSession):
    # 1. Setup
    embedding_model = await get_embedding_model()
    engine = ConfidenceEngine(damping_factor=0.5)

    # 2. Insert a world fact (no explicit confidence)
    text_1 = 'The capital of France is Paris.'
    embedding_1 = embedding_model.encode([text_1])[0]

    unit_1 = MemoryUnit(
        text=text_1,
        embedding=embedding_1,
        event_date=dt.datetime.now(dt.timezone.utc),
        fact_type=FactTypes.WORLD,
        confidence_alpha=None,
        confidence_beta=None,
        access_count=0,
    )
    session.add(unit_1)
    await session.commit()

    # 3. Calculate prior for a similar opinion
    text_2 = 'I think Paris is the capital of France.'
    embedding_2 = embedding_model.encode([text_2])[0]

    alpha, beta = await engine.calculate_informative_prior(session, embedding_2)

    # It should inherit from the implicit (5.0, 1.0) of the world fact
    # Inherited alpha: 5.0 * ~0.9 * 0.5 = ~2.25
    # Total alpha: 1.0 + ~2.25 = ~3.25
    assert alpha > 2.5
    assert beta > 1.0
    assert alpha > beta
