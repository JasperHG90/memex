"""Shared helper for pruning stale evidence from MentalModel observations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import MemoryUnit, MentalModel, Observation, UnitEntity

if TYPE_CHECKING:
    from memex_core.memory.reflect.queue_service import ReflectionQueueService

logger = logging.getLogger('memex.core.services.mental_model_cleanup')


async def prune_stale_evidence(
    session: AsyncSession,
    entity_ids: set[UUID],
    deleted_unit_ids: list[UUID],
    vault_id: UUID,
) -> set[UUID]:
    """Remove evidence citing deleted memory units from MentalModel observations.

    For each entity_id, queries MentalModels in the given vault, deserializes
    observations, removes evidence whose memory_id is in deleted_unit_ids,
    drops observations with zero evidence, deletes models with zero observations,
    otherwise updates JSONB + flag_modified + sets embedding = None.

    Returns the set of entity IDs whose mental models were actually modified
    (evidence pruned or model deleted).
    """
    if not entity_ids or not deleted_unit_ids:
        return set()

    deleted_set = set(deleted_unit_ids)
    affected_entity_ids: set[UUID] = set()

    for entity_id in entity_ids:
        stmt = select(MentalModel).where(
            col(MentalModel.entity_id) == entity_id,
            col(MentalModel.vault_id) == vault_id,
        )
        result = await session.exec(stmt)
        models = list(result.all())

        for model in models:
            if not model.observations:
                continue

            observations = [Observation(**obs) for obs in model.observations]
            pruned = False
            pruned_to_empty: set[UUID] = set()

            for obs in observations:
                original_len = len(obs.evidence)
                obs.evidence = [ev for ev in obs.evidence if ev.memory_id not in deleted_set]
                if len(obs.evidence) < original_len:
                    pruned = True
                    if not obs.evidence:
                        pruned_to_empty.add(obs.id)

            # Only drop observations that were pruned to empty, not naturally empty ones
            if pruned_to_empty:
                observations = [obs for obs in observations if obs.id not in pruned_to_empty]

            if not pruned:
                continue

            affected_entity_ids.add(entity_id)

            if not observations:
                await session.delete(model)
            else:
                model.observations = [obs.model_dump(mode='json') for obs in observations]
                model.embedding = None
                flag_modified(model, 'observations')

    return affected_entity_ids


async def cascade_chunk_unit_staling(
    session: AsyncSession,
    note_id: UUID,
    vault_id: UUID,
    stale_chunk_ids: list[UUID],
    queue_service: 'ReflectionQueueService | None' = None,
) -> list[UUID]:
    """Cascade memory-unit staling triggered by chunks being marked stale.

    Storage-level ``mark_memory_units_stale`` flips the units' status, but
    the entity graph is left holding evidence pointers to memory units that
    no longer back active text. Without this cascade, mental models keep
    referencing facts that have been silently retired, and the entities are
    never re-reflected so their summaries don't shrink.

    This helper:
    1. Finds memory units linked to the supplied chunk_ids (regardless of
       their current status — they may have just been staled by the caller).
    2. Collects the entities those units mentioned.
    3. Prunes evidence for those units from the entities' mental models.
    4. Queues affected entities for re-reflection so the summaries catch up.

    Returns the unit IDs whose evidence was considered for pruning.
    """
    if not stale_chunk_ids:
        return []

    units_stmt = select(MemoryUnit).where(
        col(MemoryUnit.note_id) == note_id,
        col(MemoryUnit.chunk_id).in_(stale_chunk_ids),
    )
    units = (await session.exec(units_stmt)).all()
    if not units:
        return []
    unit_ids: list[UUID] = [u.id for u in units]

    entity_stmt = select(UnitEntity.entity_id).where(col(UnitEntity.unit_id).in_(unit_ids))
    entity_ids: set[UUID] = set((await session.exec(entity_stmt)).all())
    if not entity_ids:
        return unit_ids

    await session.flush()

    affected_entity_ids = await prune_stale_evidence(session, entity_ids, unit_ids, vault_id)

    if affected_entity_ids and queue_service is not None:
        await queue_service.handle_deletion_event(session, affected_entity_ids, vault_id)

    return unit_ids
