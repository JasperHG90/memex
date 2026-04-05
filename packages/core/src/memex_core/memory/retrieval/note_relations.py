"""Reusable async query functions for note & memory unit relationships."""

from __future__ import annotations

import math
from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_common.schemas import MemoryLinkDTO, RelatedNoteDTO

# Cap: entities with mention_count above this are too generic to be useful
_ENTITY_FANOUT_CAP = 50

# Max related notes returned per input note
_TOP_K_RELATED = 5

# Max entity IDs to consider in the second query (most specific first)
_MAX_ENTITY_IDS = 100


async def fetch_memory_links(
    session: AsyncSession,
    unit_ids: list[UUID],
) -> dict[UUID, list[MemoryLinkDTO]]:
    """Fetch MemoryLink data for the given unit IDs (both directions).

    Returns a dict keyed by the queried unit_id, with a list of MemoryLinkDTOs
    representing links to/from that unit.
    """
    if not unit_ids:
        return {}

    result = await session.execute(
        text("""
            SELECT
                ml.from_unit_id,
                ml.to_unit_id,
                ml.link_type,
                ml.weight,
                ml.created_at,
                ml.link_metadata,
                mu.note_id AS linked_note_id,
                n.title AS linked_note_title
            FROM memory_links ml
            JOIN memory_units mu
                ON mu.id = CASE
                    WHEN ml.from_unit_id = ANY(:ids) THEN ml.to_unit_id
                    ELSE ml.from_unit_id
                END
            LEFT JOIN notes n ON n.id = mu.note_id
            WHERE ml.from_unit_id = ANY(:ids) OR ml.to_unit_id = ANY(:ids)
        """),
        {'ids': [str(uid) for uid in unit_ids]},
    )

    unit_id_set = {UUID(str(u)) for u in unit_ids}
    links_map: dict[UUID, list[MemoryLinkDTO]] = defaultdict(list)
    for row in result.mappings():
        from_uid = UUID(str(row['from_unit_id']))
        to_uid = UUID(str(row['to_unit_id']))
        linked_unit_id = UUID(
            str(row['to_unit_id'] if from_uid in unit_id_set else row['from_unit_id'])
        )

        # Determine which queried unit_id this belongs to
        if from_uid in unit_id_set:
            queried_uid = from_uid
        else:
            queried_uid = to_uid

        dto = MemoryLinkDTO(
            unit_id=linked_unit_id,
            note_id=UUID(str(row['linked_note_id'])) if row['linked_note_id'] else None,
            note_title=row['linked_note_title'],
            relation=row['link_type'],
            weight=float(row['weight']),
            time=row['created_at'],
            metadata=row['link_metadata'] if row['link_metadata'] else {},
        )
        links_map[queried_uid].append(dto)

    return dict(links_map)


async def fetch_memory_links_for_notes(
    session: AsyncSession,
    note_ids: list[UUID],
) -> dict[UUID, list[MemoryLinkDTO]]:
    """Fetch links for notes by first resolving note_ids to unit_ids, then
    aggregating and deduplicating at note level.

    Deduplication: same relation to same target note keeps highest weight.
    """
    if not note_ids:
        return {}

    # Step 1: get all unit_ids for the given note_ids
    result = await session.execute(
        text('SELECT id, note_id FROM memory_units WHERE note_id = ANY(:note_ids)'),
        {'note_ids': [str(nid) for nid in note_ids]},
    )
    unit_to_note: dict[UUID, UUID] = {}
    all_unit_ids: list[UUID] = []
    for row in result.mappings():
        uid = UUID(str(row['id']))
        nid = UUID(str(row['note_id']))
        unit_to_note[uid] = nid
        all_unit_ids.append(uid)

    if not all_unit_ids:
        return {}

    # Step 2: fetch links for all units
    unit_links = await fetch_memory_links(session, all_unit_ids)

    # Step 3: re-group by note_id and deduplicate
    note_links: dict[UUID, dict[tuple[UUID | None, str], MemoryLinkDTO]] = defaultdict(dict)
    for uid, links in unit_links.items():
        if uid not in unit_to_note:
            continue
        nid = unit_to_note[uid]
        for link in links:
            # Dedup key: (target_note_id, relation)
            key = (link.note_id, link.relation)
            existing = note_links[nid].get(key)
            if existing is None or link.weight > existing.weight:
                note_links[nid][key] = link

    return {nid: list(deduped.values()) for nid, deduped in note_links.items()}


async def compute_related_notes(
    session: AsyncSession,
    note_ids: list[UUID],
) -> dict[UUID, list[RelatedNoteDTO]]:
    """Find notes related to the given notes via shared entities.

    Uses inverse-log weighting: entities with lower mention_count are more
    specific and score higher. Entities with mention_count > _ENTITY_FANOUT_CAP
    are excluded.
    """
    if not note_ids:
        return {}

    # Step 1: get entities for input notes (excluding high-fanout ones)
    result = await session.execute(
        text("""
            SELECT DISTINCT
                mu.note_id,
                e.id AS entity_id,
                e.canonical_name,
                e.entity_type,
                e.mention_count
            FROM memory_units mu
            JOIN unit_entities ue ON ue.unit_id = mu.id
            JOIN entities e ON e.id = ue.entity_id
            WHERE mu.note_id = ANY(:note_ids)
              AND e.mention_count <= :fanout_cap
        """),
        {'note_ids': [str(nid) for nid in note_ids], 'fanout_cap': _ENTITY_FANOUT_CAP},
    )

    # Map: note_id -> set of (entity_id, canonical_name, mention_count)
    note_entities: dict[UUID, list[tuple[UUID, str, int]]] = defaultdict(list)
    all_entity_ids_with_count: dict[UUID, tuple[str, int]] = {}

    for row in result.mappings():
        nid = UUID(str(row['note_id']))
        eid = UUID(str(row['entity_id']))
        name = row['canonical_name']
        count = int(row['mention_count'])
        note_entities[nid].append((eid, name, count))
        all_entity_ids_with_count[eid] = (name, count)

    if not all_entity_ids_with_count:
        return {}

    # Limit to top-100 most specific entities (lowest mention_count)
    sorted_entities = sorted(all_entity_ids_with_count.items(), key=lambda x: x[1][1])
    top_entity_ids = [eid for eid, _ in sorted_entities[:_MAX_ENTITY_IDS]]

    # Step 2: find other notes sharing those entities (excluding input notes)
    note_id_strs = [str(nid) for nid in note_ids]
    result = await session.execute(
        text("""
            SELECT DISTINCT
                mu.note_id,
                e.id AS entity_id,
                e.canonical_name
            FROM memory_units mu
            JOIN unit_entities ue ON ue.unit_id = mu.id
            JOIN entities e ON e.id = ue.entity_id
            WHERE e.id = ANY(:entity_ids)
              AND mu.note_id IS NOT NULL
              AND mu.note_id != ALL(:note_ids)
        """),
        {
            'entity_ids': [str(eid) for eid in top_entity_ids],
            'note_ids': note_id_strs,
        },
    )

    # Map: candidate_note_id -> set of (entity_id, canonical_name)
    candidate_entities: dict[UUID, set[tuple[UUID, str]]] = defaultdict(set)
    for row in result.mappings():
        cnid = UUID(str(row['note_id']))
        eid = UUID(str(row['entity_id']))
        name = row['canonical_name']
        candidate_entities[cnid].add((eid, name))

    if not candidate_entities:
        return {}

    # Step 3: fetch titles for candidate notes
    candidate_note_ids = list(candidate_entities.keys())
    title_result = await session.execute(
        text('SELECT id, title FROM notes WHERE id = ANY(:ids)'),
        {'ids': [str(nid) for nid in candidate_note_ids]},
    )
    note_titles: dict[UUID, str | None] = {}
    for row in title_result.mappings():
        note_titles[UUID(str(row['id']))] = row['title']

    # Step 4: score and build results per input note
    related_map: dict[UUID, list[RelatedNoteDTO]] = {}

    for input_nid in note_ids:
        input_entity_ids = {eid for eid, _, _ in note_entities.get(input_nid, [])}
        if not input_entity_ids:
            continue

        scored: list[tuple[UUID, float, list[str]]] = []
        for cand_nid, cand_ents in candidate_entities.items():
            shared = [(eid, name) for eid, name in cand_ents if eid in input_entity_ids]
            if not shared:
                continue

            # Inverse-log weighting: rarer entities score higher
            score = 0.0
            for eid, _ in shared:
                _, mc = all_entity_ids_with_count[eid]
                score += 1.0 / (1.0 + math.log1p(mc))

            # Top 3 most specific shared entity names
            shared_sorted = sorted(
                shared,
                key=lambda x: all_entity_ids_with_count[x[0]][1],
            )
            top_names = [name for _, name in shared_sorted[:3]]
            scored.append((cand_nid, score, top_names))

        # Sort by score descending, take top K
        scored.sort(key=lambda x: x[1], reverse=True)

        # Normalize strengths relative to max score
        if scored:
            max_score = scored[0][1]
            related_map[input_nid] = [
                RelatedNoteDTO(
                    note_id=cand_nid,
                    title=note_titles.get(cand_nid),
                    shared_entities=top_names,
                    strength=round(score / max_score, 3) if max_score > 0 else 0.0,
                )
                for cand_nid, score, top_names in scored[:_TOP_K_RELATED]
            ]

    return related_map
