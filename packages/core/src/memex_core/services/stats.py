"""Stats service — aggregate counts and token usage for Memex."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from cachetools import TTLCache
from cachetools_async import cached as cached_async
from sqlalchemy import cast as sa_cast, Date
from sqlmodel import col

from memex_common.exceptions import MemoryUnitNotFoundError

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.stats')


class StatsService(BaseService):
    """Aggregate statistics and memory unit CRUD."""

    async def get_stats_counts(
        self,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> dict[str, int]:
        """Get total counts for notes, memory units, entities, and reflection queue."""
        from memex_core.memory.sql_models import Entity, MemoryUnit, Note, ReflectionQueue
        from sqlmodel import func, select

        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            note_stmt = select(func.count(Note.id))
            memory_stmt = select(func.count(MemoryUnit.id))
            entity_stmt = select(func.count(Entity.id))
            queue_stmt = select(func.count(ReflectionQueue.id))

            if ids:
                note_stmt = note_stmt.where(col(Note.vault_id).in_(ids))
                memory_stmt = memory_stmt.where(col(MemoryUnit.vault_id).in_(ids))
                queue_stmt = queue_stmt.where(col(ReflectionQueue.vault_id).in_(ids))

            note_count = (await session.exec(note_stmt)).one()
            memory_count = (await session.exec(memory_stmt)).one()
            entity_count = (await session.exec(entity_stmt)).one()
            queue_count = (await session.exec(queue_stmt)).one()

            return {
                'notes': note_count,
                'memories': memory_count,
                'entities': entity_count,
                'reflection_queue': queue_count,
            }

    async def get_memory_unit(self, unit_id: UUID | str) -> Any | None:
        """Get a memory unit by ID."""
        from memex_core.memory.sql_models import MemoryUnit

        uid = UUID(str(unit_id))
        async with self.metastore.session() as session:
            return await session.get(MemoryUnit, uid)

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """Delete a memory unit and all associated data.

        ORM cascades handle: unit_entities, outgoing_links, incoming_links.
        DB FK cascade handles: evidence_log.
        """
        from memex_core.memory.sql_models import MemoryUnit

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if not unit:
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')

            await session.delete(unit)
            await session.commit()

        return True

    @cached_async(TTLCache(maxsize=1, ttl=300), key=lambda self: 'token_usage')
    async def get_daily_token_usage(self) -> list[dict[str, Any]]:
        """Get daily aggregated token usage. Cached for 5 minutes."""
        from memex_core.memory.sql_models import TokenUsage
        from sqlmodel import select, func

        async with self.metastore.session() as session:
            stmt = (
                select(
                    sa_cast(TokenUsage.timestamp, Date).label('date'),
                    func.sum(TokenUsage.total_tokens).label('total_tokens'),
                )
                .group_by(sa_cast(TokenUsage.timestamp, Date))
                .order_by(sa_cast(TokenUsage.timestamp, Date))
            )
            results = (await session.exec(stmt)).all()
            return [{'date': r.date, 'total_tokens': r.total_tokens} for r in results]
