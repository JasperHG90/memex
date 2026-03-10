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

        await session.flush()

    async def handle_retrieval_event(
        self,
        session: AsyncSession,
        entity_ids: set[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        if not entity_ids:
            return

        now = datetime.now(timezone.utc)

        # Fetch entities with their queue items (LEFT join — queue item may not exist)
        stmt = (
            select(Entity, ReflectionQueue)
            .outerjoin(ReflectionQueue, col(Entity.id) == col(ReflectionQueue.entity_id))
            .where(col(Entity.id).in_(entity_ids))
            .where(
                (col(ReflectionQueue.vault_id) == vault_id)
                | (col(ReflectionQueue.vault_id).is_(None))
            )
        )
        results = await session.exec(stmt)

        # Update entity retrieval stats only — do NOT re-queue for reflection
        # since no new evidence was added (retrieval != extraction).
        for entity, queue_item in results.all():
            entity.retrieval_count += 1
            entity.last_retrieved_at = now
            session.add(entity)

            # Update priority score if queue item exists (doesn't change status)
            if queue_item is not None:
                queue_item.priority_score = self.calculate_priority(
                    queue_item.accumulated_evidence,
                    entity.mention_count,
                    entity.retrieval_count,
                )
                session.add(queue_item)

        await session.flush()

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

    async def get_next_batch(self, session, limit=10, vault_id=None, vault_ids=None):
        stmt = (
            select(ReflectionQueue)
            .where(
                col(ReflectionQueue.status).in_([ReflectionStatus.PENDING, ReflectionStatus.FAILED])
            )
            .where(col(ReflectionQueue.priority_score) >= self.config.min_priority)
            .order_by(desc(col(ReflectionQueue.priority_score)))
            .limit(limit)
        )
        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)
        if ids:
            stmt = stmt.where(col(ReflectionQueue.vault_id).in_(ids))
        results = await session.exec(stmt)
        return list(results.all())

    async def claim_next_batch(
        self,
        session: AsyncSession,
        limit: int = 10,
        vault_id: UUID | None = None,
    ) -> list[ReflectionQueue]:
        """
        Fetch and lock the next batch of pending/failed reflection tasks.
        Uses SELECT ... FOR UPDATE SKIP LOCKED to ensure safe concurrency.
        Marks tasks as PROCESSING.
        """
        stmt = (
            select(ReflectionQueue)
            .where(
                col(ReflectionQueue.status).in_([ReflectionStatus.PENDING, ReflectionStatus.FAILED])
            )
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

    async def mark_failed(
        self,
        session: AsyncSession,
        entity_id: UUID,
        vault_id: UUID = GLOBAL_VAULT_ID,
        error: str = '',
    ) -> None:
        """Record a failure for a queue item, moving to DEAD_LETTER when retries exhausted."""
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id) == entity_id)
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        result = await session.exec(stmt)
        item = result.first()
        if item is None:
            return

        item.retry_count += 1
        item.last_error = error[:2000] if error else None

        if item.retry_count >= item.max_retries:
            item.status = ReflectionStatus.DEAD_LETTER
            logger.info(
                'Reflection task for entity %s moved to dead letter after %d retries',
                entity_id,
                item.retry_count,
            )
        else:
            item.status = ReflectionStatus.FAILED
            logger.info(
                'Reflection task for entity %s failed (retry %d/%d)',
                entity_id,
                item.retry_count,
                item.max_retries,
            )

        session.add(item)
        await session.commit()

    async def get_dead_letter_items(
        self,
        session: AsyncSession,
        limit: int = 50,
        offset: int = 0,
        vault_id: UUID | None = None,
    ) -> list[ReflectionQueue]:
        """Retrieve dead-lettered reflection tasks."""
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.status) == ReflectionStatus.DEAD_LETTER)
            .order_by(desc(col(ReflectionQueue.last_queued_at)))
            .limit(limit)
            .offset(offset)
        )
        if vault_id is not None:
            stmt = stmt.where(col(ReflectionQueue.vault_id) == vault_id)

        results = await session.exec(stmt)
        return list(results.all())

    async def retry_dead_letter(
        self,
        session: AsyncSession,
        item_id: UUID,
    ) -> ReflectionQueue | None:
        """Reset a dead-lettered item back to pending for re-processing."""
        item = await session.get(ReflectionQueue, item_id)
        if item is None or item.status != ReflectionStatus.DEAD_LETTER:
            return None

        item.status = ReflectionStatus.PENDING
        item.retry_count = 0
        item.last_error = None
        item.last_queued_at = datetime.now(timezone.utc)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item
