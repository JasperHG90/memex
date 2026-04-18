"""Link creation pipeline stage for the extraction engine.

Creates three types of links between memory units:

1. **Causal links** — derived from LLM-extracted causal relations.
2. **Temporal links** — intra-document ordering by event date.
3. **Semantic links** — based on embedding similarity.
4. **Cross-document temporal links** — connect new facts to the
   existing timeline in the database.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.memory.extraction.utils import normalize_timestamp
from memex_core.memory.sql_models import MemoryLink

logger = logging.getLogger('memex.core.memory.extraction.pipeline.linking')


async def create_links(
    session: AsyncSession,
    unit_ids: list[str],
    facts: list[ProcessedFact],
    vault_id: UUID = GLOBAL_VAULT_ID,
    event_date: datetime | None = None,
) -> None:
    """Create Temporal, Causal, and Semantic links between memory units.

    Also creates cross-document temporal links to connect the new batch
    of facts to the existing timeline.

    Args:
        session: Active DB session.
        unit_ids: List of memory unit IDs (parallel to *facts*).
        facts: Processed facts with embeddings and metadata.
        vault_id: Vault scope.
    """
    links: list[dict] = []

    # 1. Causal Links
    for i, fact in enumerate(facts):
        uid_from = unit_ids[i]
        for rel in fact.causal_relations:
            target_idx = rel.target_fact_index
            if 0 <= target_idx < len(unit_ids) and target_idx != i:
                uid_to = unit_ids[target_idx]
                links.append(
                    {
                        'from_unit_id': uid_to
                        if rel.relationship_type == 'caused_by'
                        else uid_from,
                        'to_unit_id': uid_from if rel.relationship_type == 'caused_by' else uid_to,
                        'vault_id': vault_id,
                        'link_type': rel.relationship_type.value
                        if hasattr(rel.relationship_type, 'value')
                        else rel.relationship_type,
                        'weight': rel.strength,
                    }
                )

    # 2. Temporal Links
    sorted_indices = sorted(
        range(len(facts)),
        key=lambda i: normalize_timestamp(
            facts[i].occurred_start or facts[i].mentioned_at, fallback=event_date
        ),
    )
    for k in range(len(sorted_indices) - 1):
        idx_a = sorted_indices[k]
        idx_b = sorted_indices[k + 1]
        fact_a = facts[idx_a]
        fact_b = facts[idx_b]

        # Intra-document temporal links
        if fact_a.note_id and fact_a.note_id == fact_b.note_id:
            links.append(
                {
                    'from_unit_id': unit_ids[idx_a],
                    'to_unit_id': unit_ids[idx_b],
                    'vault_id': vault_id,
                    'link_type': 'temporal',
                    'weight': 1.0,
                }
            )

    # 3. Semantic Links
    # Find similar facts for each new fact
    # We must run these sequentially because we are sharing the same AsyncSession
    for i, fact in enumerate(facts):
        # Exclude the fact itself from search results
        exclude = [UUID(unit_ids[i])]
        similar_items = await storage.find_similar_facts(
            session,
            fact.embedding,
            limit=5,
            threshold=0.75,
            exclude_ids=exclude,
            vault_ids=[vault_id] if vault_id else None,
        )

        from_id = unit_ids[i]
        for target_uuid, score in similar_items:
            if math.isnan(score):
                continue

            links.append(
                {
                    'from_unit_id': from_id,
                    'to_unit_id': str(target_uuid),
                    'vault_id': vault_id,
                    'link_type': 'semantic',
                    'weight': score,
                }
            )

    if links:
        stmt = pg_insert(MemoryLink).values(links).on_conflict_do_nothing()
        await session.exec(stmt)

    # 4. Cross-Document Temporal Linking
    await create_cross_doc_links(session, unit_ids, facts, vault_id=vault_id, event_date=event_date)


async def create_cross_doc_links(
    session: AsyncSession,
    unit_ids: list[str],
    facts: list[ProcessedFact],
    vault_id: UUID = GLOBAL_VAULT_ID,
    event_date: datetime | None = None,
) -> None:
    """Link the new batch of facts to the existing timeline in the DB.

    Finds the closest predecessor and successor facts by event date
    and creates temporal links to connect the new batch.

    Args:
        session: Active DB session.
        unit_ids: List of memory unit IDs (parallel to *facts*).
        facts: Processed facts with timestamps.
        vault_id: Vault scope.
    """
    if not facts:
        return

    # Identify the temporal bounds of the new batch
    sorted_facts = sorted(
        zip(unit_ids, facts),
        key=lambda x: normalize_timestamp(
            x[1].occurred_start or x[1].mentioned_at, fallback=event_date
        ),
    )

    earliest_id, earliest_fact = sorted_facts[0]
    latest_id, latest_fact = sorted_facts[-1]

    earliest_ts = earliest_fact.occurred_start or earliest_fact.mentioned_at
    latest_ts = latest_fact.occurred_start or latest_fact.mentioned_at

    # Exclude current batch IDs from search to avoid self-linking
    current_batch_uuids = [UUID(uid) for uid in unit_ids]

    # Find Predecessor (Fact < Earliest)
    predecessor_uuid = await storage.find_temporal_neighbor(
        session, earliest_ts, direction='before', exclude_ids=current_batch_uuids
    )

    # Find Successor (Fact > Latest)
    successor_uuid = await storage.find_temporal_neighbor(
        session, latest_ts, direction='after', exclude_ids=current_batch_uuids
    )

    cross_links = []
    if predecessor_uuid:
        cross_links.append(
            {
                'from_unit_id': str(predecessor_uuid),
                'to_unit_id': earliest_id,
                'vault_id': vault_id,
                'link_type': 'temporal',
                'weight': 1.0,
            }
        )

    if successor_uuid:
        cross_links.append(
            {
                'from_unit_id': latest_id,
                'to_unit_id': str(successor_uuid),
                'vault_id': vault_id,
                'link_type': 'temporal',
                'weight': 1.0,
            }
        )

    if cross_links:
        stmt = pg_insert(MemoryLink).values(cross_links).on_conflict_do_nothing()
        await session.exec(stmt)
