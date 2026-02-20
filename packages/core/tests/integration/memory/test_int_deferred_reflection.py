import pytest
import datetime as dt
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, col
import dspy

from memex_core.config import (
    MemexConfig,
    ExtractionConfig,
    SimpleTextSplitting,
    ConfidenceConfig,
    PostgresMetaStoreConfig,
    PostgresInstanceConfig,
    SecretStr,
    GLOBAL_VAULT_ID,
    ModelConfig,
    ServerConfig,
    MemoryConfig,
    OpinionFormationConfig,
    ReflectionConfig,
)
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.sql_models import MentalModel, ReflectionQueue, ReflectionStatus, Entity
from urllib.parse import urlparse


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_deferred_reflection_workflow(session: AsyncSession, postgres_uri: str):
    """
    Integration test for Deferred Reflection (The "Dirty Queue" Pattern).
    1. Retain content with reflect_after=False.
    2. Verify entities are queued in ReflectionQueue.
    3. Retain more content to verify priority increment.
    4. Call process_reflection_queue.
    5. Verify MentalModels are created and Queue is cleared.
    """

    # --- Setup ---
    # Set explicit session ID
    from memex_core.context import set_session_id

    set_session_id('test_deferred_reflection')

    parsed = urlparse(postgres_uri)
    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                extraction=ExtractionConfig(
                    model=ModelConfig(model='gemini/gemini-3-flash-preview'),
                    text_splitting=SimpleTextSplitting(
                        chunk_size_tokens=1000, chunk_overlap_tokens=100
                    ),
                    max_concurrency=5,
                ),
                opinion_formation=OpinionFormationConfig(confidence=ConfidenceConfig()),
                reflection=ReflectionConfig(),  # Enable reflection queue service
            ),
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 5432,
                    database=parsed.path.lstrip('/'),
                    user=parsed.username or 'postgres',
                    password=SecretStr(parsed.password or 'postgres'),
                )
            ),
        )
    )

    lm = dspy.LM(model=config.server.memory.extraction.model.model)
    with dspy.context(lm=lm):
        predictor = dspy.Predict(ExtractSemanticFacts)
        embedding_model = await get_embedding_model()
        entity_resolver = EntityResolver(resolution_threshold=0.65)

        extraction_engine = ExtractionEngine(
            config=config.server.memory.extraction,
            confidence_config=config.server.memory.opinion_formation.confidence,
            lm=lm,
            predictor=predictor,
            embedding_model=embedding_model,
            entity_resolver=entity_resolver,
            reflection_config=config.server.memory.reflection,
        )

        retrieval_engine = RetrievalEngine(embedder=embedding_model)

        memory_engine = MemoryEngine(
            config=config,
            extraction_engine=extraction_engine,
            retrieval_engine=retrieval_engine,
        )

        # --- Step 1: Retain WITHOUT Reflection ---
        content_1 = (
            'Elon Musk announced a new rocket for SpaceX today. It is designed for rapid reuse.'
        )

        await memory_engine.retain(
            session=session,
            contents=[
                RetainContent(content=content_1, event_date=dt.datetime.now(dt.timezone.utc))
            ],
            reflect_after=False,
        )

        # --- Step 2: Verify Queue ---
        stmt_all_q = select(ReflectionQueue).order_by(col(ReflectionQueue.priority_score).desc())
        queue_items = (await session.exec(stmt_all_q)).all()

        assert len(queue_items) > 0, 'Queue should not be empty'
        assert all(q.status == ReflectionStatus.PENDING for q in queue_items)

        # Find the "Elon" entity item
        elon_queue_item = None
        elon_entity = None
        for q in queue_items:
            ent = await session.get(Entity, q.entity_id)
            if ent and 'Elon' in ent.canonical_name:
                elon_queue_item = q
                elon_entity = ent
                break

        assert elon_queue_item is not None, 'Elon Musk entity should be queued'
        initial_priority = elon_queue_item.priority_score
        assert initial_priority > 0

        # --- Step 3: Retain AGAIN (Increment Priority) ---
        content_2 = 'SpaceX plans to launch the new rocket next month. Elon Musk is excited.'
        result_2 = await memory_engine.retain(
            session=session,
            contents=[
                RetainContent(content=content_2, event_date=dt.datetime.now(dt.timezone.utc))
            ],
            reflect_after=False,
        )

        # Debug: Print what entities were touched
        print(
            f'\n[DEBUG] Second retain touched entities: {result_2.get("touched_entities", set())}'
        )
        print(f'[DEBUG] Elon entity ID: {elon_entity.id if elon_entity else None}')

        # Check if Elon entity was in the touched entities
        if elon_entity and elon_entity.id not in result_2.get('touched_entities', set()):
            print(f'[WARNING] Elon entity {elon_entity.id} was not in touched entities!')

        # Re-fetch the queue item from database since session state may have changed
        stmt_refetch = select(ReflectionQueue).where(ReflectionQueue.id == elon_queue_item.id)
        refetched_item = (await session.exec(stmt_refetch)).first()
        assert refetched_item is not None, 'Queue item should still exist'
        updated_priority = refetched_item.priority_score
        assert updated_priority > initial_priority, (
            f'Priority should increase. {initial_priority} -> {updated_priority}'
        )
        # Update reference for later use
        elon_queue_item = refetched_item

        # --- Step 4: Process Queue ---
        # Manually trigger processing since reflect_on_queue doesn't exist
        queue_service = extraction_engine.queue_service
        pending_items = await queue_service.get_next_batch(session, vault_id=GLOBAL_VAULT_ID)

        if pending_items:
            from memex_core.memory.reflect.reflection import ReflectionEngine, ReflectionRequest

            reflector = ReflectionEngine(session, config, embedder=embedding_model)

            requests = [
                ReflectionRequest(entity_id=item.entity_id, vault_id=item.vault_id)
                for item in pending_items
            ]

            await reflector.reflect_batch(requests)

            await queue_service.complete_reflection(
                session, [item.entity_id for item in pending_items], vault_id=GLOBAL_VAULT_ID
            )

        # --- Step 5: Verify Queue Cleared & Mental Models Created ---
        # Check if item exists in DB (it should be deleted by complete_reflection)
        stmt_check = select(ReflectionQueue).where(ReflectionQueue.id == elon_queue_item.id)
        result_check = await session.exec(stmt_check)
        # Note: Depending on session state, we might need to detach or clear cache, but `exec` should fetch fresh
        assert result_check.first() is None, 'Queue item should be deleted after processing'

        # Verify Mental Model
        assert elon_entity is not None
        stmt_mm = select(MentalModel).where(MentalModel.entity_id == elon_entity.id)
        mm = (await session.exec(stmt_mm)).first()
        assert mm is not None, 'Mental Model should be created'
        assert len(mm.observations) > 0
