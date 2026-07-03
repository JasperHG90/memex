"""Candidate retrieval for contradiction detection."""

import logging
from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import MemoryUnit, UnitEntity, ContentStatus

logger = logging.getLogger('memex.core.memory.contradiction.candidates')


async def get_candidates(
    session: AsyncSession,
    unit: MemoryUnit,
    vault_id: UUID,
    k: int = 15,
    threshold: float = 0.5,
    target_entity_ids: list[UUID] | None = None,
) -> list[MemoryUnit]:
    """
    Retrieve candidate units that might contradict or be related to the given unit.

    Pipeline:
    1. Entity overlap — find units sharing entities
    2. Semantic similarity — cosine > threshold via pgvector
    3. Merge + deduplicate
    4. Source-diverse selection — round-robin by source document, cap at k

    When ``target_entity_ids`` is non-empty, both the entity-overlap and the
    semantic paths AND-filter by membership in that list. This narrows the
    candidate pool for explicit-claim units (where the LLM identified the
    claim's target) so contradiction matching does not drift across topics.
    """
    entity_candidates = await _get_entity_overlap_candidates(
        session, unit, vault_id, target_entity_ids=target_entity_ids
    )
    semantic_candidates = await _get_semantic_candidates(
        session, unit, vault_id, threshold, target_entity_ids=target_entity_ids
    )

    all_candidates: dict[UUID, MemoryUnit] = {}
    for c in entity_candidates + semantic_candidates:
        if c.id != unit.id:
            all_candidates[c.id] = c

    if not all_candidates:
        return []

    return _source_diverse_select(list(all_candidates.values()), k)


async def _get_entity_overlap_candidates(
    session: AsyncSession,
    unit: MemoryUnit,
    vault_id: UUID,
    target_entity_ids: list[UUID] | None = None,
) -> list[MemoryUnit]:
    """Find units that share entities with the given unit.

    When ``target_entity_ids`` is non-empty, restrict the entity-overlap
    join to that set instead of every entity the unit references.
    """
    entity_stmt = select(UnitEntity.entity_id).where(UnitEntity.unit_id == unit.id)
    result = await session.exec(entity_stmt)
    entity_ids = list(result.all())

    if target_entity_ids:
        # Intersect: only narrow on entities the claim actually targets.
        target_set = {eid for eid in target_entity_ids}
        entity_ids = [eid for eid in entity_ids if eid in target_set]

    if not entity_ids:
        return []

    shared_unit_ids_stmt = (
        select(UnitEntity.unit_id)
        .where(
            col(UnitEntity.entity_id).in_(entity_ids),
            UnitEntity.unit_id != unit.id,
            UnitEntity.vault_id == vault_id,
        )
        .distinct()
    )
    result = await session.exec(shared_unit_ids_stmt)
    candidate_ids = list(result.all())

    if not candidate_ids:
        return []

    units_stmt = select(MemoryUnit).where(
        col(MemoryUnit.id).in_(candidate_ids),
        MemoryUnit.status == ContentStatus.ACTIVE,
    )
    result = await session.exec(units_stmt)
    return list(result.all())


async def _get_semantic_candidates(
    session: AsyncSession,
    unit: MemoryUnit,
    vault_id: UUID,
    threshold: float,
    target_entity_ids: list[UUID] | None = None,
) -> list[MemoryUnit]:
    """Find semantically similar units via pgvector cosine distance.

    Similarities are corrected through the shared anisotropy corrector
    before thresholding, so candidates are scored on a discriminative scale
    rather than the compressed [0.7, 0.95] band typical of high-dimensional
    embeddings.

    When ``target_entity_ids`` is non-empty, AND-filter the SQL to units
    that mention at least one of the listed entity IDs.
    """
    if unit.embedding is None or len(unit.embedding) == 0:
        return []

    from memex_core.memory.models.anisotropy import get_shared_corrector

    # Loosen the SQL pre-filter to give the corrector room to discriminate.
    # The pgvector index still bounds cost via ORDER BY + LIMIT.
    coarse_max_distance = max(0.05, 1.0 - threshold + 0.2)

    if target_entity_ids:
        sql = """
            SELECT mu.id, 1 - (mu.embedding <=> :embedding) AS sim
            FROM memory_units mu
            WHERE mu.vault_id = :vault_id
              AND mu.id != :unit_id
              AND mu.status = 'active'
              AND (mu.embedding <=> :embedding) < :max_distance
              AND EXISTS (
                  SELECT 1 FROM unit_entities ue
                  WHERE ue.unit_id = mu.id
                    AND ue.entity_id = ANY(:target_entity_ids)
              )
            ORDER BY (mu.embedding <=> :embedding)
            LIMIT 60
        """
        params: dict = {
            'vault_id': str(vault_id),
            'unit_id': str(unit.id),
            'embedding': '[' + ','.join(str(float(x)) for x in unit.embedding) + ']',
            'max_distance': coarse_max_distance,
            'target_entity_ids': [str(eid) for eid in target_entity_ids],
        }
    else:
        sql = """
            SELECT id, 1 - (embedding <=> :embedding) AS sim
            FROM memory_units
            WHERE vault_id = :vault_id
              AND id != :unit_id
              AND status = 'active'
              AND (embedding <=> :embedding) < :max_distance
            ORDER BY (embedding <=> :embedding)
            LIMIT 60
        """
        params = {
            'vault_id': str(vault_id),
            'unit_id': str(unit.id),
            'embedding': '[' + ','.join(str(float(x)) for x in unit.embedding) + ']',
            'max_distance': coarse_max_distance,
        }

    stmt = text(sql)
    result = await session.execute(stmt, params)

    corrector = get_shared_corrector()
    candidate_ids: list[UUID] = []
    for row in result:
        if corrector.normalize(float(row.sim)) >= threshold:
            candidate_ids.append(row.id)
            if len(candidate_ids) >= 30:
                break

    if not candidate_ids:
        return []

    units_stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(candidate_ids))
    result = await session.exec(units_stmt)
    return list(result.all())


def _source_diverse_select(candidates: list[MemoryUnit], k: int) -> list[MemoryUnit]:
    """Round-robin selection across source documents to ensure diversity."""
    if len(candidates) <= k:
        return candidates

    by_note: dict[UUID | None, list[MemoryUnit]] = defaultdict(list)
    for c in candidates:
        by_note[c.note_id].append(c)

    selected: list[MemoryUnit] = []
    groups = list(by_note.values())
    group_indices = [0] * len(groups)

    while len(selected) < k:
        added_this_round = False
        for i, group in enumerate(groups):
            if group_indices[i] < len(group) and len(selected) < k:
                selected.append(group[group_indices[i]])
                group_indices[i] += 1
                added_this_round = True
        if not added_this_round:
            break

    return selected
