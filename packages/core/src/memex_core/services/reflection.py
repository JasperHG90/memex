"""Reflection service — opinion formation, reflection, and belief adjustment."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import dspy

from memex_core.config import MemexConfig
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.reflect.models import (
    OpinionFormationRequest,
    ReflectionRequest,
    ReflectionResult,
)
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import MemoryUnit, Observation
from memex_core.memory.models.embedding import FastEmbedder
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.reflection')


class ReflectionService:
    """Reflection, opinion formation, and belief adjustment operations.

    Unlike other services, ReflectionService has heavier dependencies
    because reflection interacts with the memory engine, LLM, and queue.
    """

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        config: MemexConfig,
        lm: dspy.LM,
        memory: MemoryEngine,
        extraction: ExtractionEngine,
        queue_service: ReflectionQueueService,
        embedding_model: FastEmbedder,
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.lm = lm
        self.memory = memory
        self._extraction = extraction
        self.queue_service = queue_service
        self.embedding_model = embedding_model
        self._reflection_lock = asyncio.Lock()

    async def process_opinion_formation(
        self, query: str, context: list[MemoryUnit], vault_id: UUID
    ) -> None:
        """
        Process the opinion formation loop.
        Synthesizes an answer and forms opinions.
        Intended to be capable of running as a background task.
        """
        answer = await self._synthesize_answer(query, context)

        op_request = OpinionFormationRequest(
            query=query, context=context, answer=answer, vault_id=vault_id
        )
        await self.form_opinions(op_request)

    async def process_opinion_formation_minimal(
        self, query: str, context: list[dict], vault_id: UUID
    ) -> None:
        """
        Process opinion formation with minimal context to prevent memory leaks.
        Receives only lightweight dicts and fetches units by ID in a fresh session,
        instead of holding full MemoryUnit objects in the background task.
        """
        unit_ids = [UUID(c['id']) for c in context if 'id' in c]

        async with self.metastore.session() as session:
            from sqlmodel import select, col

            stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(unit_ids))
            result = await session.exec(stmt)
            fresh_units = list(result.all())

            if not fresh_units:
                return

            answer = await self._synthesize_answer(query, fresh_units)

            op_request = OpinionFormationRequest(
                query=query, context=fresh_units, answer=answer, vault_id=vault_id
            )

            await self.memory.form_opinions(session, op_request)
            await session.commit()

    async def background_reflect(self, request: ReflectionRequest) -> None:
        """Run reflection in the background, ensuring serialization via lock."""
        async with self._reflection_lock:
            try:
                logger.info(f'Starting background reflection for entity {request.entity_id}')
                await self.reflect(request)
                logger.info(f'Completed background reflection for entity {request.entity_id}')
            except Exception as e:
                logger.error(
                    f'Error during background reflection for entity {request.entity_id}: {e}',
                    exc_info=True,
                )

    async def background_reflect_batch(self, requests: list[ReflectionRequest]) -> None:
        """Run batch reflection in the background, ensuring serialization via lock."""
        if not requests:
            return

        async with self._reflection_lock:
            try:
                entity_ids = [str(r.entity_id) for r in requests]
                logger.info(f'Starting background batch reflection for entities: {entity_ids}')
                await self.reflect_batch(requests)
                logger.info(f'Completed background batch reflection for {len(requests)} entities')
            except Exception as e:
                logger.error(f'Error during background batch reflection: {e}', exc_info=True)

    async def _synthesize_answer(self, query: str, context: list[MemoryUnit]) -> str:
        """Helper to generate an answer for opinion formation context."""

        class RagSignature(dspy.Signature):
            """Answer the query given the context."""

            context = dspy.InputField()
            question = dspy.InputField()
            answer = dspy.OutputField()

        predictor = dspy.Predict(RagSignature)
        with dspy.context(lm=self.lm):
            pred = predictor(context=[u.text for u in context], question=query)
            return pred.answer

    async def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """Reflect on a single entity to update its Mental Model."""
        async with self.metastore.session() as session:
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(session, self.config, self.embedding_model)

            models = await reflector.reflect_batch([request])
            if not models:
                await self.queue_service.mark_failed(
                    session,
                    entity_id=request.entity_id,
                    vault_id=request.vault_id,
                    error=f'Reflection produced no models for entity {request.entity_id}',
                )
                from memex_core.memory.sql_models import MentalModel

                return ReflectionResult(
                    entity_id=request.entity_id,
                    new_observations=[],
                    updated_model=MentalModel(
                        entity_id=request.entity_id, vault_id=request.vault_id
                    ),
                )

            mental_model = models[0]

            await self.queue_service.complete_reflection(
                session, [request.entity_id], vault_id=request.vault_id
            )

            return ReflectionResult(
                entity_id=request.entity_id,
                new_observations=[Observation(**o) for o in mental_model.observations],
                updated_model=mental_model,
            )

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResult]:
        """
        Reflect on multiple entities in parallel using a single DB session.
        This is significantly faster than sequential calls.
        """
        if not requests:
            return []

        async with self.metastore.session() as session:
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(session, self.config, self.embedding_model)

            models = await reflector.reflect_batch(requests)

            from collections import defaultdict

            succeeded_ids = {m.entity_id for m in models}
            processed_by_vault = defaultdict(list)
            for m in models:
                processed_by_vault[m.vault_id].append(m.entity_id)

            for vid, eids in processed_by_vault.items():
                await self.queue_service.complete_reflection(session, eids, vault_id=vid)

            for req in requests:
                if req.entity_id not in succeeded_ids:
                    await self.queue_service.mark_failed(
                        session,
                        entity_id=req.entity_id,
                        vault_id=req.vault_id,
                        error=f'Reflection failed for entity {req.entity_id}',
                    )

            results = []
            for model in models:
                results.append(
                    ReflectionResult(
                        entity_id=model.entity_id,
                        new_observations=list(model.observations),
                        updated_model=model,
                    )
                )
            return results

    async def form_opinions(self, request: OpinionFormationRequest) -> list[Any]:
        """Extract and persist opinions based on a recent interaction."""
        async with self.metastore.session() as session:
            return await self.memory.form_opinions(session, request)

    async def adjust_belief(
        self,
        unit_uuid: str | UUID,
        evidence_type_key: str,
        description: str | None = None,
    ) -> dict[str, float]:
        """Adjust the confidence of a memory unit based on new evidence."""
        async with self.metastore.session() as session:
            result = await self._extraction.adjust_belief(
                session, str(unit_uuid), evidence_type_key, description
            )
            await session.commit()
            return result

    async def get_evidence_log(self, unit_id: UUID, *, limit: int = 20) -> list[dict]:
        """Retrieve the evidence audit trail for a memory unit."""
        async with self.metastore.session() as session:
            from sqlmodel import select, col
            from memex_core.memory.sql_models import EvidenceLog

            statement = (
                select(EvidenceLog)
                .where(col(EvidenceLog.unit_id) == unit_id)
                .order_by(col(EvidenceLog.created_at).desc())
                .limit(limit)
            )
            result = await session.exec(statement)
            logs = result.all()
            return [
                {
                    'id': log.id,
                    'unit_id': log.unit_id,
                    'evidence_type': log.evidence_type,
                    'description': log.description,
                    'alpha_before': log.alpha_before,
                    'beta_before': log.beta_before,
                    'alpha_after': log.alpha_after,
                    'beta_after': log.beta_after,
                    'confidence_before': log.alpha_before / (log.alpha_before + log.beta_before),
                    'confidence_after': log.alpha_after / (log.alpha_after + log.beta_after),
                    'created_at': log.created_at,
                }
                for log in logs
            ]

    async def get_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """Get the next batch of items from the reflection queue."""
        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)
        async with self.metastore.session() as session:
            return await self.queue_service.get_next_batch(
                session,
                limit=limit,
                vault_ids=ids or None,
            )

    async def claim_reflection_queue_batch(
        self, limit: int = 10, vault_id: UUID | None = None
    ) -> list[Any]:
        """Claim and lock the next batch of items from the reflection queue."""
        async with self.metastore.session() as session:
            return await self.queue_service.claim_next_batch(
                session, limit=limit, vault_id=vault_id
            )

    async def get_dead_letter_items(
        self,
        limit: int = 50,
        offset: int = 0,
        vault_id: UUID | None = None,
    ) -> list[Any]:
        """List dead-lettered reflection tasks."""
        async with self.metastore.session() as session:
            return await self.queue_service.get_dead_letter_items(
                session, limit=limit, offset=offset, vault_id=vault_id
            )

    async def retry_dead_letter_item(self, item_id: UUID) -> Any:
        """Retry a dead-lettered reflection task by resetting it to pending."""
        async with self.metastore.session() as session:
            return await self.queue_service.retry_dead_letter(session, item_id)
