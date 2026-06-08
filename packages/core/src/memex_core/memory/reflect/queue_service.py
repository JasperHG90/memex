import math
import logging
from uuid import UUID
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select, col, desc
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.vault_policy import reflect_enabled
from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus, Vault
from memex_core.config import ReflectionConfig, GLOBAL_VAULT_ID
from memex_core.metrics import (
    REFLECTION_ENQUEUE_SKIPPED_TOTAL,
    REFLECTION_QUEUE_PRIORITY_LANE_ENQUEUED_TOTAL,
)

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

    async def handle_deletion_event(
        self,
        session: AsyncSession,
        entity_ids: set[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> None:
        """Queue entities for urgent reflection after evidence deletion.

        Uses priority 1.0 (maximum) to bypass min_priority threshold,
        since these models have lost evidence and need immediate re-reflection.
        Does NOT increment accumulated_evidence (evidence was removed, not added).
        """
        if not entity_ids:
            return

        # Ensure queue rows exist for all affected entities
        await self._ensure_queue_items(session, entity_ids, vault_id)

        # Fetch and update all queue items for affected entities
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        results = await session.exec(stmt)

        now = datetime.now(timezone.utc)
        for queue_item in results.all():
            queue_item.priority_score = 1.0
            queue_item.status = ReflectionStatus.PENDING
            queue_item.retry_count = 0
            queue_item.last_queued_at = now
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

    async def _reflect_enabled_for_vault(self, session: AsyncSession, vault_id: UUID) -> bool:
        """Whether reflection should be queued for this vault (kind+policy).

        System vaults default reflection off, so their notes never enter the
        reflection queue — keeping it from filling with work that would only be
        discarded. Unknown vaults default to enabled (fail open).
        """
        vault = await session.get(Vault, vault_id)
        if not isinstance(vault, Vault):
            # Unknown / not loaded → fail open (reflect). Real vaults are checked.
            return True
        return reflect_enabled(vault.kind, vault.policy)

    async def _ensure_queue_items(
        self, session: AsyncSession, entity_ids: set[UUID], vault_id: UUID
    ):
        if not entity_ids:
            return
        if not await self._reflect_enabled_for_vault(session, vault_id):
            REFLECTION_ENQUEUE_SKIPPED_TOTAL.labels(reason='vault_policy').inc()
            return

        stmt = (
            select(ReflectionQueue.entity_id)
            .where(col(ReflectionQueue.entity_id).in_(entity_ids))
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        result = await session.exec(stmt)
        rows = result.all()

        # The query selects ``ReflectionQueue.entity_id``, so in
        # production rows are bare UUIDs. The branches below tolerate
        # the additional shapes that show up in tests where the same
        # ``session.exec`` is mocked across query variants:
        # tuple-wrapped scalars, row objects exposing ``entity_id`` or
        # ``id``, and stringified UUIDs. Anything that cannot be
        # coerced is skipped so an unreadable row does not crash the
        # batch and re-queue every entity in it.
        existing_ids: set[UUID] = set()
        for r in rows:
            val = r[0] if isinstance(r, (tuple, list)) else r
            if isinstance(val, UUID):
                existing_ids.add(val)
                continue
            if hasattr(val, 'entity_id'):
                existing_ids.add(val.entity_id)
                continue
            if hasattr(val, 'id'):
                existing_ids.add(val.id)
                continue
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

    async def get_next_batch(
        self,
        session: AsyncSession,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[ReflectionQueue]:
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
        Marks tasks as PROCESSING. Priority lane is claimed first; refresh-
        task backoff via ``last_queued_at`` is respected (rows whose backoff
        hasn't elapsed are skipped, supported by the partial index from
        migration 043).
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(ReflectionQueue)
            .where(
                col(ReflectionQueue.status).in_([ReflectionStatus.PENDING, ReflectionStatus.FAILED])
            )
            .where(col(ReflectionQueue.priority_score) >= self.config.min_priority)
            # Backoff filter — but a freshly enqueued row has
            # ``last_queued_at IS NULL``, and ``NULL <= now()`` is UNKNOWN
            # (falsy in WHERE), which would silently exclude every fresh
            # task. Treat NULL as eligible.
            .where(
                or_(
                    col(ReflectionQueue.last_queued_at) <= now,
                    col(ReflectionQueue.last_queued_at).is_(None),
                )
            )
            .order_by(
                desc(col(ReflectionQueue.priority_lane)),
                desc(col(ReflectionQueue.priority_score)),
                col(ReflectionQueue.last_queued_at),
            )
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

    async def complete_reflection(
        self,
        session: AsyncSession,
        entity_ids: list[UUID] | set[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> None:
        """Delete reflect tasks for the given entities — including historical
        FAILED and DEAD_LETTER rows for the same ``(entity, vault)``.

        Filters by ``task_type='reflect'`` so a reflect ack does NOT nuke any
        pending/processing ``refresh_observation`` rows on the same entity —
        the two task types ride independent lanes by design.

        Asymmetry note: ``complete_refresh`` deletes ONLY the claimed row
        (scoped by ``ReflectionQueue.id``) and intentionally preserves
        FAILED/DEAD_LETTER siblings as a diagnostic trail. This method
        preserves the original pre-V21 behavior — reflect's per-entity
        rows are coalesced on every ack — because reflect tasks are
        per-entity (one row per status) whereas refresh tasks are
        per-observation (many rows per entity, partial-UNIQUE-deduped
        on the in-flight lane).
        """
        if not entity_ids:
            return
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id).in_(list(entity_ids)))
            .where(col(ReflectionQueue.vault_id) == vault_id)
            .where(col(ReflectionQueue.task_type) == 'reflect')
        )
        results = await session.exec(stmt)
        items = results.all()
        for item in items:
            await session.delete(item)
        await session.commit()

    async def complete_refresh(self, session: AsyncSession, item: ReflectionQueue) -> None:
        """Delete only the claimed refresh-observation row.

        Scoped to ``ReflectionQueue.id == item.id`` so the ack cannot wipe
        historical FAILED/DEAD_LETTER siblings on the same
        ``(entity_id, vault_id, observation_id)`` — operators need those
        rows as the diagnostic trail when a refresh task starts failing.
        """
        row = await session.get(ReflectionQueue, item.id)
        if row is not None:
            await session.delete(row)
            await session.commit()

    async def enqueue_priority_reflect(
        self,
        session: AsyncSession,
        entity_ids: set[UUID] | list[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> int:
        """Insert priority-lane reflect tasks; upsert priority_lane=True on existing pending/processing rows.

        Used by the restore-MU path. The upsert relies on the partial UNIQUE
        index ``idx_reflection_queue_entity_vault_active_unique`` from
        migration 043 (predicate filtered to pending+processing reflect rows).
        ``index_where`` uses SQLAlchemy column expressions so the rendered
        predicate matches the migration DDL form character-for-character —
        partial-UNIQUE arbiter inference is text-normalized and finicky.

        Insert-vs-update detection via ``RETURNING (xmax = 0) AS was_insert``:
        Postgres exposes the row's ``xmax`` system column on a successful
        DML — for a fresh INSERT, ``xmax`` is 0 (no deleting transaction);
        for an ON CONFLICT UPDATE, ``xmax`` is the updating xid. This gives
        a per-row insert/update label without a second query and is the
        canonical Postgres idiom. Wrapped in ``sql_text(...)`` because the
        bare ``column('xmax') == 0`` form depends on Postgres-version-
        sensitive integer-literal coercion.
        """
        if not entity_ids:
            return 0
        if not await self._reflect_enabled_for_vault(session, vault_id):
            REFLECTION_ENQUEUE_SKIPPED_TOTAL.labels(reason='vault_policy').inc()
            return 0

        rows = [
            {
                'entity_id': eid,
                'vault_id': vault_id,
                'status': ReflectionStatus.PENDING,
                'priority_lane': True,
                'priority_score': 1.0,
                'task_type': 'reflect',
                'accumulated_evidence': 0,
            }
            for eid in entity_ids
        ]
        stmt = pg_insert(ReflectionQueue).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=['entity_id', 'vault_id'],
            index_where=and_(
                col(ReflectionQueue.task_type) == 'reflect',
                col(ReflectionQueue.status).in_(
                    [ReflectionStatus.PENDING, ReflectionStatus.PROCESSING]
                ),
            ),
            set_={
                'priority_lane': True,
                'priority_score': func.greatest(
                    col(ReflectionQueue.priority_score), stmt.excluded.priority_score
                ),
            },
        )
        # ON CONFLICT DO UPDATE returns rowcount = inserts + updates. To count
        # ONLY true inserts (an existing PENDING row's priority_lane flip is
        # not an enqueue), use RETURNING with the Postgres-specific xmax check
        # — xmax is type xid and only sql_text() renders a stable
        # ``xmax = 0`` comparison across Postgres versions (a bare
        # ``column('xmax') == 0`` would rely on integer-literal coercion to
        # xid which has no native operator in pg_proc).
        stmt = stmt.returning(sql_text('(xmax = 0) AS was_insert'))
        result = await session.execute(stmt)
        rows_returned = result.all()
        # Caller owns the transaction (restore-path commits flag flip + upsert
        # together). Bump counter only for true INSERTs.
        true_inserts = sum(1 for r in rows_returned if r[0])
        if true_inserts > 0:
            REFLECTION_QUEUE_PRIORITY_LANE_ENQUEUED_TOTAL.inc(true_inserts)
        return true_inserts

    async def reclaim_with_backoff(
        self, session: AsyncSession, item: ReflectionQueue, jitter_seconds: float
    ) -> None:
        """Re-enqueue a task whose advisory lock was held (sentinel path).

        Resets status to PENDING with ``last_queued_at = now() + jitter`` so
        the worker doesn't busy-spin on a long-held lock. ``retry_count`` is
        NOT incremented — advisory-lock contention is benign, not a failure.
        """
        item.status = ReflectionStatus.PENDING
        item.last_queued_at = datetime.now(timezone.utc) + timedelta(seconds=jitter_seconds)
        session.add(item)
        await session.commit()

    async def mark_abandoned(
        self,
        session: AsyncSession,
        entity_id: UUID,
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> None:
        """Re-enqueue a queue item after Phase 5 CAS UPDATE abandoned.

        Used when a concurrent worker advanced the row's version between
        our read and our CAS UPDATE. CAS abandons are benign concurrency
        contention — the work itself didn't fail, another writer just
        committed first. We flip the row back to PENDING so the next
        scheduler tick re-claims via SKIP LOCKED. retry_count is NOT
        incremented (the entity didn't fail), so a hot entity in a
        multi-worker cluster cannot DEAD_LETTER from contention alone.
        """
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id) == entity_id)
            .where(col(ReflectionQueue.vault_id) == vault_id)
        )
        result = await session.exec(stmt)
        item = result.first()
        if item is None:
            return

        item.status = ReflectionStatus.PENDING
        # Preserve ``last_error`` from any prior real failure. CAS abandons
        # are benign concurrency contention; stomping the column would hide
        # an outstanding LLM-timeout / parse-error / etc. that an operator
        # is investigating. Only write when the column is empty or already
        # records a CAS abandon (so consecutive abandons don't accumulate).
        if not item.last_error or 'CAS abandon' in item.last_error:
            item.last_error = 'CAS abandon (concurrent refresh won)'
        session.add(item)
        await session.commit()
        logger.info(
            'Reflection task for entity %s re-enqueued after CAS abandon (retry_count unchanged)',
            entity_id,
        )

    async def mark_failed(
        self,
        session: AsyncSession,
        entity_id: UUID,
        vault_id: UUID = GLOBAL_VAULT_ID,
        error: str = '',
        *,
        task_type: str = 'reflect',
        observation_id: UUID | None = None,
    ) -> None:
        """Record a failure for a queue item, moving to DEAD_LETTER when retries exhausted.

        Filters by ``task_type`` (and ``observation_id`` for refresh tasks) so a
        reflect failure on an entity does NOT bump retry_count on a co-pending
        refresh task for the same entity.
        """
        if task_type == 'refresh_observation' and observation_id is None:
            # A refresh failure with no observation_id cannot be safely
            # attributed to any row — bumping retry_count on a random sibling
            # would dead-letter the wrong task. Log and abort; the orphan
            # PROCESSING row (if any) is recovered by ``recover_stale_processing``.
            logger.warning(
                'mark_failed called with task_type=refresh_observation and '
                'observation_id=None for entity %s vault %s — aborting to avoid '
                'incrementing retry_count on the wrong row',
                entity_id,
                vault_id,
            )
            return
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.entity_id) == entity_id)
            .where(col(ReflectionQueue.vault_id) == vault_id)
            .where(col(ReflectionQueue.task_type) == task_type)
        )
        if task_type == 'refresh_observation':
            stmt = stmt.where(col(ReflectionQueue.observation_id) == observation_id)
        # Prefer the currently-PROCESSING row (the one we just failed) over
        # any historical FAILED siblings; without ORDER BY, the planner may
        # return any matching row, leaving the PROCESSING one stuck.
        # ``LIMIT 1`` + ``FOR UPDATE SKIP LOCKED`` matches ``claim_next_batch``'s
        # row-level safety — two concurrent failure paths for the same row
        # don't double-increment retry_count, and a row held by an in-flight
        # claim is silently skipped.
        stmt = (
            stmt.order_by(
                (col(ReflectionQueue.status) == ReflectionStatus.PROCESSING).desc(),
                col(ReflectionQueue.last_queued_at).desc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
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

    async def observability_snapshot(self, session: AsyncSession) -> tuple[dict[str, int], float]:
        """Return (depth by task_type, oldest DEAD_LETTER refresh age in seconds).

        Two cheap aggregate queries used by the scheduler to refresh Prometheus
        gauges. Depth counts ``status IN ('pending', 'processing')`` rows
        grouped by ``task_type``. Dead-letter age is the gap between now and
        the oldest ``last_queued_at`` of a DEAD_LETTER row with
        ``task_type='refresh_observation'`` — non-zero in steady state signals
        operator action (DEAD_LETTER refresh rows accumulate without auto-expiry
        because ``complete_reflection`` only deletes ``reflect``-type rows by
        design).
        """
        depth_rows = (
            await session.exec(
                select(ReflectionQueue.task_type, func.count())  # type: ignore[call-overload]
                .where(
                    col(ReflectionQueue.status).in_(
                        [ReflectionStatus.PENDING, ReflectionStatus.PROCESSING]
                    )
                )
                .group_by(col(ReflectionQueue.task_type))
            )
        ).all()
        depths = {str(row[0]): int(row[1]) for row in depth_rows}
        for tt in ('reflect', 'refresh_observation'):
            depths.setdefault(tt, 0)

        oldest_dl_row = (
            await session.exec(
                select(ReflectionQueue.last_queued_at)
                .where(col(ReflectionQueue.status) == ReflectionStatus.DEAD_LETTER)
                .where(col(ReflectionQueue.task_type) == 'refresh_observation')
                .order_by(col(ReflectionQueue.last_queued_at))
                .limit(1)
            )
        ).first()
        if oldest_dl_row is None:
            oldest_age = 0.0
        else:
            now = datetime.now(timezone.utc)
            oldest_age = max(0.0, (now - oldest_dl_row).total_seconds())
        return depths, oldest_age

    async def recover_stale_processing(self, session: AsyncSession) -> int:
        """Reset PROCESSING items that have been stuck longer than the configured timeout.

        Covers BOTH ``task_type='reflect'`` and ``task_type='refresh_observation'``
        — the WHERE clause filters by status only. If a refresh worker crashes
        between Phase C CAS abandon and the scheduler's
        ``reclaim_refresh_with_backoff`` call, the orphan PROCESSING row is
        recovered here on the next stale-recovery tick.

        Returns the number of recovered items.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.config.stale_processing_timeout_seconds
        )
        stmt = (
            select(ReflectionQueue)
            .where(col(ReflectionQueue.status) == ReflectionStatus.PROCESSING)
            .where(col(ReflectionQueue.last_queued_at) < cutoff)
            .with_for_update(skip_locked=True)
        )
        result = await session.exec(stmt)
        items = result.all()

        if not items:
            return 0

        now = datetime.now(timezone.utc)
        for item in items:
            item.status = ReflectionStatus.PENDING
            item.last_queued_at = now
            session.add(item)

        await session.commit()
        logger.info('Recovered %d stale PROCESSING items back to PENDING.', len(items))
        return len(items)
