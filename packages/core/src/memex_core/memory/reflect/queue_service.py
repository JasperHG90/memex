import math
import logging
from uuid import UUID
from datetime import datetime, timezone

from sqlmodel import select, col, desc
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus
from memex_core.config import ReflectionConfig, GLOBAL_VAULT_ID

logger = logging.getLogger('memex.core.memory.reflect.queue_service')


class ReflectionQueueService:
    """
    Service to manage the Reflection Queue and calculate priority scores.
    Implements the "Salience Formula" for Memory Consolidation.
    """

    def __init__(self, config: ReflectionConfig):
        self.config = config

    def calculate_priority(
        self,
        accumulated_evidence: int,
        mention_count: int,
        retrieval_count: int,
        last_retrieved_at: datetime | None = None,
    ) -> float:
        """
        Calculate the reflection priority score.
        Priority = (U * evidence) + (I * log10(mentions)) + (R * log10(retrieval))
        """
        importance_score = math.log10(max(mention_count, 1))
        # Resonance also uses log10 for diminishing returns
        resonance_score = math.log10(max(retrieval_count, 1))

        score = (
            (self.config.weight_urgency * accumulated_evidence)
            + (self.config.weight_importance * importance_score)
            + (self.config.weight_resonance * resonance_score)
        )
        return round(score, 4)

    async def handle_extraction_event(
        self,
        session: AsyncSession,
        entity_ids: set[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        if not entity_ids:
            return

        # 1. Ensure queue items exist
        await self._ensure_queue_items(session, entity_ids, vault_id)

        # 2. Fetch Entities and Queue Items
        stmt = (
            select(Entity, ReflectionQueue)
            .join(ReflectionQueue, col(Entity.id) == col(ReflectionQueue.entity_id))
            .where(col(Entity.id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        results = await session.exec(stmt)

        # 3. Update in Python
        now = datetime.now(timezone.utc)
        for entity, queue_item in results.all():
            # Defensive: if _ensure_queue_items failed or race condition, create here
            if queue_item is None:
                queue_item = ReflectionQueue(
                    entity_id=entity.id,
                    vault_id=vault_id,
                    status=ReflectionStatus.PENDING,
                    accumulated_evidence=0,
                    priority_score=0.0,
                )

            queue_item.accumulated_evidence += 1
            queue_item.last_queued_at = now
            queue_item.status = ReflectionStatus.PENDING
            queue_item.priority_score = self.calculate_priority(
                queue_item.accumulated_evidence, entity.mention_count, entity.retrieval_count
            )
            session.add(queue_item)

        await session.commit()

    async def handle_retrieval_event(
        self,
        session: AsyncSession,
        entity_ids: set[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        if not entity_ids:
            return

        now = datetime.now(timezone.utc)
        # 1. Ensure queue items exist
        await self._ensure_queue_items(session, entity_ids, vault_id)

        # 2. Fetch Entities and Queue Items
        stmt = (
            select(Entity, ReflectionQueue)
            .join(ReflectionQueue, col(Entity.id) == col(ReflectionQueue.entity_id))
            .where(col(Entity.id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        results = await session.exec(stmt)

        # 3. Update both Entity and QueueItem
        for entity, queue_item in results.all():
            entity.retrieval_count += 1
            entity.last_retrieved_at = now

            # Defensive check
            if queue_item is None:
                queue_item = ReflectionQueue(
                    entity_id=entity.id,
                    vault_id=vault_id,
                    status=ReflectionStatus.PENDING,
                    accumulated_evidence=0,
                    priority_score=0.0,
                )

            queue_item.last_queued_at = now
            queue_item.status = ReflectionStatus.PENDING
            queue_item.priority_score = self.calculate_priority(
                queue_item.accumulated_evidence, entity.mention_count, entity.retrieval_count
            )
            session.add(queue_item)
            session.add(entity)

        await session.commit()

    async def _ensure_queue_items(
        self, session: AsyncSession, entity_ids: set[UUID], vault_id: UUID
    ):
        if not entity_ids:
            return

        stmt = (
            select(ReflectionQueue.entity_id)
            .where(col(ReflectionQueue.entity_id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        result = await session.exec(stmt)
        rows = result.all()

        existing_ids = set()
        for r in rows:
            val = r[0] if isinstance(r, (tuple, list)) else r
            if hasattr(val, 'entity_id'):
                existing_ids.add(val.entity_id)
            elif hasattr(val, 'id'):
                existing_ids.add(val.id)
            elif isinstance(val, UUID):
                existing_ids.add(val)
            else:
                try:
                    existing_ids.add(UUID(str(val)))
                except (ValueError, TypeError):
                    continue

        missing_ids = entity_ids - existing_ids
        if not missing_ids:
            return

        for eid in missing_ids:
            new_item = ReflectionQueue(
                entity_id=eid,
                vault_id=vault_id,
                status=ReflectionStatus.PENDING,
                accumulated_evidence=0,
                priority_score=1.0,
            )
            session.add(new_item)

        await session.flush()

    async def get_next_batch(self, session, limit=10, vault_id=None):
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.status) == ReflectionStatus.PENDING)
            .where(col(ReflectionQueue.priority_score) >= self.config.min_priority)
            .order_by(desc(col(ReflectionQueue.priority_score)))
            .limit(limit)
        )
        if vault_id:
            stmt = stmt.where(col(ReflectionQueue.vault_id) == vault_id)
        results = await session.exec(stmt)
        return list(results.all())

    async def claim_next_batch(
        self,
        session: AsyncSession,
        limit: int = 10,
        vault_id: UUID | None = None,
    ) -> list[ReflectionQueue]:
        """
        Fetch and lock the next batch of pending reflection tasks.
        Uses SELECT ... FOR UPDATE SKIP LOCKED to ensure safe concurrency.
        Marks tasks as PROCESSING.
        """
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.status) == ReflectionStatus.PENDING)
            .where(col(ReflectionQueue.priority_score) >= self.config.min_priority)
            .order_by(desc(col(ReflectionQueue.priority_score)))
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        if vault_id:
            stmt = stmt.where(col(ReflectionQueue.vault_id) == vault_id)

        results = await session.exec(stmt)
        items = results.all()

        if not items:
            return []

        now = datetime.now(timezone.utc)
        for item in items:
            item.status = ReflectionStatus.PROCESSING
            item.last_queued_at = now
            session.add(item)

        await session.commit()

        for item in items:
            await session.refresh(item)

        return list(items)

    async def complete_reflection(self, session, entity_ids, vault_id=GLOBAL_VAULT_ID):
        if not entity_ids:
            return
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        results = await session.exec(stmt)
        items = results.all()
        for item in items:
            await session.delete(item)
        await session.commit()
