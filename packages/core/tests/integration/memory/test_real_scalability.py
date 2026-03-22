import pytest
import time
import logging
from uuid import uuid4
from datetime import datetime, timezone
from urllib.parse import urlparse
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, func

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import Entity, MemoryUnit, UnitEntity, MentalModel
from memex_core.config import (
    MemexConfig,
    PostgresMetaStoreConfig,
    PostgresInstanceConfig,
    ExtractionConfig,
    SecretStr,
    ModelConfig,
    ServerConfig,
    MemoryConfig,
)

# Force using the real LM
import dspy

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm
async def test_real_scalability_concurrent_reflection(session: AsyncSession, postgres_uri: str):
    """
    REAL Scalability Test (NO MOCKS).

    This test runs the full Reflection loop against the real LLM API for 4 entities.

    Expected Behavior:
    - Concurrent (O(1)): ~12-18s total (dominated by the slowest LLM chain)

    We assert that the total time is significantly less than the sequential sum (~40s+).
    """

    # 1. Setup: Create 4 Entities with real distinct memories
    # -----------------------------------------------------
    entities = []
    topics = [
        ('Python', 'Python is a high-level programming language.'),
        ('Rust', 'Rust is a systems programming language focused on safety.'),
        ('Docker', 'Docker is a platform for developing, shipping, and running applications.'),
        ('Postgres', 'PostgreSQL is a powerful, open source object-relational database system.'),
    ]

    for name, content in topics:
        entity = Entity(id=uuid4(), name=name, canonical_name=name)
        session.add(entity)

        unit = MemoryUnit(
            text=f'I learned that {content} It is very popular.',
            embedding=[0.05] * 384,
            event_date=datetime.now(timezone.utc),
        )
        session.add(unit)
        await session.flush()

        link = UnitEntity(unit_id=unit.id, entity_id=entity.id)
        session.add(link)
        entities.append(entity)

    await session.commit()

    # 2. Initialize Engine (Real Config)
    # ----------------------------------
    parsed = urlparse(postgres_uri)
    config = MemexConfig(
        server=ServerConfig(
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 5432,
                    database=parsed.path.lstrip('/'),
                    user=parsed.username or 'postgres',
                    password=SecretStr(parsed.password or 'postgres'),
                )
            ),
            memory=MemoryConfig(
                extraction=ExtractionConfig(
                    max_concurrency=10, model=ModelConfig(model='gemini/gemini-3-flash-preview')
                )
            ),
        )
    )

    # Initialize the dspy settings
    lm = dspy.LM(model=config.server.memory.extraction.model.model)

    from memex_core.memory.models.embedding import get_embedding_model

    embedder = await get_embedding_model()

    with dspy.context(lm=lm):
        engine = ReflectionEngine(session, config, embedder=embedder)

        # 3. Execution & Measurement
        # --------------------------
        logger.info(f'Starting REAL reflection on {len(entities)} entities...')
        start_time = time.perf_counter()

        from memex_core.memory.reflect.models import ReflectionRequest

        requests = [ReflectionRequest(entity_id=e.id) for e in entities]

        results = await engine.reflect_batch(requests)

        end_time = time.perf_counter()
        duration = end_time - start_time

        # 4. Assertions
        # -------------
        logger.info(f'Reflection completed in {duration:.2f} seconds.')

        # Verify we got results
        assert len(results) == len(entities), (
            f'Expected {len(entities)} mental models, got {len(results)}'
        )

        # Verify database persistence
        count = (await session.exec(select(func.count(MentalModel.id)))).one()
        assert count == len(entities)

        print(f'REAL SCALABILITY RESULT: {len(entities)} entities processed in {duration:.2f}s')

        # We assert it's under 30 seconds (Parallel should be ~15-20s)
        assert duration < 30.0, (
            f'Test took {duration:.2f}s, which suggests sequential execution. '
            f'Parallel execution should be under 30s for {len(entities)} entities.'
        )
