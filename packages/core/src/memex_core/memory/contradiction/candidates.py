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
) -> list[MemoryUnit]:
    """
    Retrieve candidate units that might contradict or be related to the given unit.

    Pipeline:
    1. Entity overlap — find units sharing entities
    2. Semantic similarity — cosine > threshold via pgvector
    3. Merge + deduplicate
    4. Source-diverse selection — round-robin by source document, cap at k
    """
    entity_candidates = await _get_entity_overlap_candidates(session, unit, vault_id)
    semantic_candidates = await _get_semantic_candidates(session, unit, vault_id, threshold)

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
) -> list[MemoryUnit]:
    """Find units that share entities with the given unit."""
    entity_stmt = select(UnitEntity.entity_id).where(UnitEntity.unit_id == unit.id)
    result = await session.exec(entity_stmt)
    entity_ids = list(result.all())

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
) -> list[MemoryUnit]:
    """Find semantically similar units via pgvector cosine distance."""
    if unit.embedding is None or len(unit.embedding) == 0:
        return []

    max_distance = 1.0 - threshold

    stmt = text("""
        SELECT id FROM memory_units
        WHERE vault_id = :vault_id
          AND id != :unit_id
          AND status = 'active'
          AND (embedding <=> :embedding) < :max_distance
        ORDER BY (embedding <=> :embedding)
        LIMIT 30
    """)

    result = await session.execute(
        stmt,
        {
            'vault_id': str(vault_id),
            'unit_id': str(unit.id),
            'embedding': '[' + ','.join(str(float(x)) for x in unit.embedding) + ']',
            'max_distance': max_distance,
        },
    )
    candidate_ids = [row[0] for row in result]

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
