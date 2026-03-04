from collections import defaultdict
import logging
from typing import Any

import dspy
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.reflect.models import OpinionFormationRequest, ReflectionRequest
from memex_core.memory.reflect.reasoning import ReasoningEngine
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import (
    MemoryUnit,
    UnitEntity,
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
            api_base=str(model_config.base_url).rstrip('/') if model_config.base_url else None,
            api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
        )
        predictor = dspy.Predict(ExtractSemanticFacts)
        entity_resolver = EntityResolver(
            resolution_threshold=config.server.memory.opinion_formation.confidence.similarity_threshold
        )

        extraction_engine = ExtractionEngine(
            config=config.server.memory.extraction,
            confidence_config=config.server.memory.opinion_formation.confidence,
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

    return MemoryEngine(
        config=config,
        extraction_engine=extraction_engine,
        retrieval_engine=retrieval_engine,
    )


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
    ):
        """
        Initialize the MemoryEngine.

        Args:
            config: The global configuration.
            extraction_engine: Pre-configured ExtractionEngine instance.
            retrieval_engine: Pre-configured RetrievalEngine instance.
        """
        self.config = config
        self.extraction = extraction_engine
        self.retrieval = retrieval_engine

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

        return {
            'unit_ids': unit_ids,
            'usage': usage,
            'touched_entities': touched_entities,
        }

    async def recall(
        self,
        session: AsyncSession,
        request: RetrievalRequest,
    ) -> list[MemoryUnit]:
        """
        Retrieve memories using the 4-channel TEMPR Recall architecture.

        Channels:
        1.  **Temporal**: Time-based decay and proximity.
        2.  **Semantic**: Vector similarity.
        3.  **Graph**: Knowledge graph traversal (entity links).
        4.  **Keyword**: BM25/Lexical matching.

        Args:
            session: Active DB session.
            request: The retrieval parameters (query, filters, limit).

        Returns:
            List of ranked MemoryUnits.
        """
        results = await self.retrieval.retrieve(session, request)
        logger.debug(f'DEBUG: Recall retrieved {len(results)} units.')

        # Update Resonance (Retrieval Count) in Reflection Queue
        if results and self.extraction.queue_service:
            try:
                unit_ids = [u.id for u in results]
                stmt = select(UnitEntity.entity_id, UnitEntity.vault_id).where(
                    col(UnitEntity.unit_id).in_(unit_ids)
                )
                rows = await session.exec(stmt)
                rows_list = rows.all()
                logger.debug(f'DEBUG: Found {len(rows_list)} linked entities via UnitEntity.')

                entities_by_vault = defaultdict(set)
                for eid, vid in rows_list:
                    entities_by_vault[vid].add(eid)

                for vid, eids in entities_by_vault.items():
                    logger.debug(
                        f'DEBUG: Calling handle_retrieval_event for vault {vid} with {len(eids)} entities.'
                    )
                    await self.extraction.queue_service.handle_retrieval_event(
                        session, eids, vault_id=vid
                    )
            except (ValueError, RuntimeError, OSError) as e:
                # Do not fail retrieval if queue update fails
                logger.warning(f'Failed to update reflection queue resonance: {e}')

        return results

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

    async def form_opinions(
        self,
        session: AsyncSession,
        request: OpinionFormationRequest,
    ) -> list[str]:
        """
        Form new opinions based on an interaction (CARA framework).

        This analyzes the interaction to detect user beliefs, preferences, or
        corrections, and updates the "Opinions" layer of memory with Bayesian confidence.

        Args:
            session: Active DB session.
            request: The interaction details (query, answer, context).

        Returns:
            List of created MemoryUnit IDs (opinions).
        """
        # Ensure we have an LM
        lm = dspy.settings.lm
        if not lm:
            # Fallback to configuring from config if dspy global not set
            # This is a safety net; ideally the app configures dspy globally.
            try:
                model_config = self.config.server.memory.extraction.model
                lm = dspy.LM(
                    model=model_config.model,
                    api_base=str(model_config.base_url).rstrip('/') if model_config.base_url else None,
                    api_key=model_config.api_key.get_secret_value()
                    if model_config.api_key
                    else None,
                )
            except (ValueError, RuntimeError, OSError, KeyError, AttributeError) as e:
                logger.warning(
                    'No LM configured for reasoning (%s). Attempting to proceed, but may fail.', e
                )

        # If lm is still None, ReasoningEngine might fail if it doesn't handle it.
        # But ReasoningEngine __init__ type hint says dspy.LM is required.
        # We'll assume the caller (App/CLI) has set up dspy.

        reasoner = ReasoningEngine(
            session,
            lm,
            embedding_model=self.extraction.embedding_model,
            retrieval_engine=self.retrieval,
        )
        results = await reasoner.form_opinions(request)
        await session.commit()
        return results

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
