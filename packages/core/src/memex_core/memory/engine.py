import logging
from typing import Any

import dspy
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.memory.contradiction import ContradictionEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import (
    MemoryUnit,
    MentalModel,
    ReflectionQueue,
    ReflectionStatus,
)

logger = logging.getLogger('memex.core.memory.engine')


async def get_memory_engine(
    config: MemexConfig,
    extraction_engine: ExtractionEngine | None = None,
    retrieval_engine: RetrievalEngine | None = None,
) -> 'MemoryEngine':
    """
    Factory method to create a MemoryEngine with dependencies.

    Args:
        config: The global Memex configuration.
        extraction_engine: Optional pre-configured ExtractionEngine instance.
        retrieval_engine: Optional pre-configured RetrievalEngine instance.

    Returns:
        Configured MemoryEngine instance.
    """
    from memex_core.memory.models import get_embedding_model, get_reranking_model, get_ner_model

    embedding_model = await get_embedding_model()
    reranking_model = await get_reranking_model()
    ner_model = await get_ner_model()

    if extraction_engine is None:
        from memex_core.memory.entity_resolver import EntityResolver
        from memex_core.memory.extraction.core import ExtractSemanticFacts

        model_config = config.server.memory.extraction.model
        assert model_config is not None, (
            'extraction.model must be set (via default_model propagation)'
        )
        lm = dspy.LM(
            model=model_config.model,
            api_base=str(model_config.base_url) if model_config.base_url else None,
            api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
        )
        predictor = dspy.Predict(ExtractSemanticFacts)
        entity_resolver = EntityResolver()

        extraction_engine = ExtractionEngine(
            config=config.server.memory.extraction,
            lm=lm,
            predictor=predictor,
            embedding_model=embedding_model,
            entity_resolver=entity_resolver,
            reflection_config=config.server.memory.reflection,
        )

    if retrieval_engine is None:
        retrieval_engine = RetrievalEngine(
            embedder=embedding_model,
            reranker=reranking_model,
            ner_model=ner_model,
            reflection_config=config.server.memory.reflection,
            retrieval_config=config.server.memory.retrieval,
        )

    contradiction_engine = _build_contradiction_engine(config)

    return MemoryEngine(
        config=config,
        extraction_engine=extraction_engine,
        retrieval_engine=retrieval_engine,
        contradiction_engine=contradiction_engine,
    )


def _build_contradiction_engine(config: MemexConfig) -> ContradictionEngine | None:
    """Create a ContradictionEngine if enabled in config."""
    try:
        contradiction_config = config.server.memory.contradiction
        if not contradiction_config.enabled:
            return None
        model_config = contradiction_config.model
        if model_config is None:
            logger.warning('contradiction.model is None — skipping contradiction engine')
            return None
        lm = dspy.LM(
            model=model_config.model,
            api_base=str(model_config.base_url) if model_config.base_url else None,
            api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
        )
        return ContradictionEngine(lm=lm, config=contradiction_config)
    except (AttributeError, TypeError, ValueError, RuntimeError) as e:
        logger.warning('Failed to build contradiction engine: %s', e)
        return None


class MemoryEngine:
    """
    The Hindsight Memory Engine.

    This is the central coordinator for the Memex system, implementing the "Hindsight is 20/20"
    framework for high-performance query execution and caching.

    It manages the lifecycle of memories through three main phases:
    1.  **Retention (Extraction)**: Ingesting raw content, extracting facts, and persisting them.
    2.  **Recall (Retrieval)**: Retrieving relevant memories using the 4-channel TEMPR architecture.
    3.  **Reflection (Synthesis)**: Synthesizing observations and mental models from raw memories.

    Attributes:
        extraction_engine: Handles fact extraction and vector indexing.
        retrieval_engine: Handles search and reranking.
    """

    def __init__(
        self,
        config: MemexConfig,
        extraction_engine: ExtractionEngine,
        retrieval_engine: RetrievalEngine,
        contradiction_engine: ContradictionEngine | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ):
        """
        Initialize the MemoryEngine.

        Args:
            config: The global configuration.
            extraction_engine: Pre-configured ExtractionEngine instance.
            retrieval_engine: Pre-configured RetrievalEngine instance.
            contradiction_engine: Optional ContradictionEngine for retain-time detection.
            session_factory: Session factory for background tasks (contradiction detection).
        """
        self.config = config
        self.extraction = extraction_engine
        self.retrieval = retrieval_engine
        self.contradiction = contradiction_engine
        self._session_factory = session_factory

    async def retain(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        note_id: str | None = None,
        reflect_after: bool = True,
        agent_name: str = 'memex_agent',
    ) -> dict[str, Any]:
        """
        Ingest and persist content into memory.

        This method corresponds to the "Retain" phase. It extracts facts, generates embeddings,
        and stores them in the `memory_units` table.

        Hindsight Principle:
        - "Edge Execution" implies we can defer heavy processing.
        - By default (`reflect_after=True`), we trigger an immediate reflection loop on
          any entities that were significantly updated.
        - If `reflect_after=False`, reflection is deferred (lazy evaluation).

        Args:
            session: Active DB session.
            contents: List of content items to retain.
            note_id: Optional ID of the parent document.
            reflect_after: If True, trigger reflection on touched entities immediately.
            agent_name: Name of the agent performing the extraction.

        Returns:
            Dictionary containing:
            - unit_ids: List of created MemoryUnit IDs.
            - usage: TokenUsage statistics.
            - touched_entities: Set of entity IDs that were involved.
        """
        # 1. Extraction Phase
        unit_ids, usage, touched_entities = await self.extraction.extract_and_persist(
            session=session,
            contents=contents,
            agent_name=agent_name,
            note_id=note_id,
        )

        logger.info(f'Retained {len(unit_ids)} units. Touched {len(touched_entities)} entities.')

        if not touched_entities:
            return {
                'unit_ids': unit_ids,
                'usage': usage,
                'touched_entities': touched_entities,
            }

        # Determine vault_id (assuming uniform per batch)
        vault_id = contents[0].vault_id if contents else GLOBAL_VAULT_ID

        # 2. Reflection Phase (Immediate or Deferred)
        if reflect_after:
            logger.info(
                f'Triggering immediate reflection on touched entities for vault {vault_id}...'
            )
            reflector = ReflectionEngine(
                session, self.config, embedder=self.extraction.embedding_model
            )

            # Use optimized batch reflection
            requests = [
                ReflectionRequest(entity_id=eid, vault_id=vault_id) for eid in touched_entities
            ]
            results = await reflector.reflect_batch(requests)

            logger.info(f'Reflected on {len(results)}/{len(touched_entities)} entities.')

            # If successful, remove them from the queue (since extract_and_persist added them)
            if self.extraction.queue_service:
                await self.extraction.queue_service.complete_reflection(
                    session, [r.entity_id for r in results]
                )
        else:
            logger.info(
                f'Deferring reflection. Entities are queued for background processing in vault {vault_id}.'
            )
            # Note: extract_and_persist already called queue_service.handle_extraction_event,
            # so entities are already in the queue with PENDING status.

        # 3. Contradiction Detection — return as pending background work
        #    The caller (server route) should schedule this via FastAPI BackgroundTasks
        #    rather than asyncio.create_task, which is unreliable under multi-worker servers.
        contradiction_task = None
        if self.contradiction and self._session_factory and unit_ids:
            contradiction_task = self.contradiction.detect_contradictions(
                session_factory=self._session_factory,
                document_id=note_id,
                unit_ids=unit_ids,
                vault_id=vault_id,
            )
            logger.info('Contradiction detection prepared for %d units.', len(unit_ids))

        return {
            'unit_ids': unit_ids,
            'usage': usage,
            'touched_entities': touched_entities,
            'contradiction_task': contradiction_task,
        }

    async def recall(
        self,
        session: AsyncSession,
        request: RetrievalRequest,
    ) -> tuple[list[MemoryUnit], Any]:
        """
        Retrieve memories using the 4-channel TEMPR Recall architecture.

        Returns:
            Tuple of (ranked MemoryUnits, optional resonance_task coroutine).
            The caller should schedule resonance_task via BackgroundTasks.
        """
        results, resonance_ctx = await self.retrieval.retrieve(session, request)
        logger.debug(f'Recall retrieved {len(results)} units.')

        # Build a background coroutine for resonance update (non-blocking).
        # Returns the coroutine function (not invoked) so the caller can
        # schedule it via BackgroundTasks or asyncio.create_task as appropriate.
        resonance_task = None
        queue_svc = self.extraction.queue_service
        if resonance_ctx and self._session_factory and queue_svc:
            session_factory = self._session_factory
            entity_ids = resonance_ctx['entity_ids']
            vault_id = resonance_ctx['vault_id']

            async def _do_resonance_update() -> None:
                try:
                    async with session_factory() as bg_session:
                        await queue_svc.handle_retrieval_event(
                            bg_session,
                            entity_ids,
                            vault_id=vault_id,
                        )
                        await bg_session.commit()
                except Exception:
                    logger.exception('Background resonance update failed')

            resonance_task = _do_resonance_update

        return (results, resonance_task)

    async def reflect(
        self,
        session: AsyncSession,
        request: ReflectionRequest,
    ) -> MentalModel:
        """
        Explicitly trigger the reflection loop for a specific entity.

        This runs the 5-phase Hindsight reflection process:
        0. Update existing observations.
        1. Seed new candidates.
        2. Hunt for evidence.
        3. Validate candidates.
        4. Compare & Merge.

        Args:
            session: Active DB session.
            request: The reflection request targeting an entity.

        Returns:
            The updated MentalModel.
        """
        reflector = ReflectionEngine(session, self.config, embedder=self.extraction.embedding_model)
        return await reflector.reflect_on_entity(request)

    async def process_reflection_queue(
        self,
        session: AsyncSession,
        limit: int = 10,
    ) -> int:
        """
        Process pending reflection tasks from the queue.

        Args:
            session: Active DB session.
            limit: Maximum number of entities to process in this batch.

        Returns:
            Number of entities successfully processed.
        """
        # 1. Fetch pending tasks with row locking
        stmt = (
            select(ReflectionQueue)
            .where(ReflectionQueue.status == ReflectionStatus.PENDING)
            .order_by(col(ReflectionQueue.priority_score).desc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        result = await session.exec(stmt)
        tasks = result.all()

        if not tasks:
            return 0

        # 2. Mark as PROCESSING
        for task in tasks:
            task.status = ReflectionStatus.PROCESSING
            session.add(task)
        await session.commit()

        logger.info(f'Processing reflection queue batch: {len(tasks)} tasks')

        # 3. Reflect
        reflector = ReflectionEngine(session, self.config, embedder=self.extraction.embedding_model)
        requests = [ReflectionRequest(entity_id=t.entity_id, vault_id=t.vault_id) for t in tasks]

        try:
            results = await reflector.reflect_batch(requests)

            # Map results back to tasks to identify successes
            # We use (entity_id, vault_id) pair as key
            succeeded_pairs = {(m.entity_id, m.vault_id) for m in results}

            # 4. Cleanup / Update Queue
            for task in tasks:
                if (task.entity_id, task.vault_id) in succeeded_pairs:
                    # Success: Remove from queue
                    await session.delete(task)
                else:
                    # Failure: Mark as FAILED
                    task.status = ReflectionStatus.FAILED
                    session.add(task)

            await session.commit()
            return len(results)

        except (ValueError, RuntimeError, OSError) as e:
            logger.error(f'Critical failure in reflection queue processing: {e}', exc_info=True)
            # Mark all as failed if the batch exploded
            for task in tasks:
                task.status = ReflectionStatus.FAILED
                session.add(task)
            await session.commit()
            return 0
