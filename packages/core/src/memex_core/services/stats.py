"""Stats service — aggregate counts for Memex."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlmodel import col

from memex_common.exceptions import MemoryUnitNotFoundError

from memex_core.services.base import BaseService
from memex_core.services.notes import _cleanup_entities_after_delete

logger = logging.getLogger('memex.core.services.stats')

# Prevent fire-and-forget tasks from being garbage-collected before completion.
_background_tasks: set[asyncio.Task[None]] = set()


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
        Entity cleanup (orphan removal, mention_count recount, mental model pruning)
        runs as a background task after the commit to avoid lock contention.
        """
        from sqlmodel import select

        from memex_core.memory.sql_models import MemoryUnit, UnitEntity

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if not unit:
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')

            vault_id = unit.vault_id

            # Collect linked entity_ids before deletion
            entity_stmt = select(UnitEntity.entity_id).where(col(UnitEntity.unit_id) == unit_id)
            entity_result = await session.exec(entity_stmt)
            entity_ids = set(entity_result.all())

            await session.delete(unit)
            await session.commit()

        # Fire-and-forget entity cleanup in background.
        if entity_ids:
            task = asyncio.create_task(
                _cleanup_entities_after_delete(self.metastore, entity_ids, [unit_id], vault_id)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        return True
