"""Shared helper for pruning stale evidence from MentalModel observations."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import MentalModel, Observation

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
