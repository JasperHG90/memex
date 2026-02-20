import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence, overload
from uuid import UUID
from collections import defaultdict

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import MemoryLink, MemoryUnit, UnitEntity
from memex_core.memory.extraction.models import EntityLink
from memex_core.memory.extraction import storage

logger = logging.getLogger('memex_core.memory.extraction.entity_links')


@overload
def _normalize_datetime(dt: datetime) -> datetime: ...


@overload
def _normalize_datetime(dt: None) -> None: ...


def _normalize_datetime(dt: datetime | None) -> datetime | None:
    """
    Normalize a datetime object to be timezone-aware (UTC).

    Args:
        dt: The datetime to normalize.

    Returns:
        A timezone-aware datetime in UTC, or None if input is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _bulk_insert_memory_links(
    session: AsyncSession, links_data: list[dict[str, Any]]
) -> None:
    """
    Bulk insert links into the MemoryLink table with conflict handling.

    Executes inserts in batches to avoid query size limits. Uses
    ON CONFLICT DO NOTHING to handle duplicate links idempotently.

    Args:
        session: Active database session.
        links_data: List of dictionaries matching MemoryLink fields.
    """
    if not links_data:
        return

    BATCH_SIZE = 1000
    for i in range(0, len(links_data), BATCH_SIZE):
        batch = links_data[i : i + BATCH_SIZE]
        insert_stmt = insert(MemoryLink).values(batch)
        upsert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=['from_unit_id', 'to_unit_id', 'link_type']
        )
        await session.exec(upsert_stmt)


def compute_temporal_links(
    new_units: dict[str, datetime],
    candidates: list[tuple[UUID, datetime]],
    time_window_hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Compute temporal links between new units and candidate neighbors based on time proximity.

    Pure logic function. Calculates a weight based on how close the events are.

    Args:
        new_units: Dictionary mapping unit_id (str) to event_date.
        candidates: List of tuples (unit_id, event_date) representing existing units.
        time_window_hours: The window size in hours to consider for linking.

    Returns:
        List of dictionaries representing temporal links.
    """
    if not new_units:
        return []

    links = []
    for unit_id_str, unit_event_date in new_units.items():
        unit_id = UUID(unit_id_str)
        unit_date = _normalize_datetime(unit_event_date)
        if not unit_date:
            continue

        try:
            time_lower = unit_date - timedelta(hours=time_window_hours)
            time_upper = unit_date + timedelta(hours=time_window_hours)
        except OverflowError:
            continue

        # Filter candidates within this unit's time window
        # We process manually to ensure type safety for the date comparison
        matching_neighbors = []
        for cand_id, cand_date in candidates:
            cand_date_norm = _normalize_datetime(cand_date)
            # Explicit check ensures we never compare datetime <= None
            if cand_date_norm is not None and time_lower <= cand_date_norm <= time_upper:
                matching_neighbors.append((cand_id, cand_date_norm))

        # Limit to top 10 closest
        matching_neighbors = matching_neighbors[:10]

        for recent_id, recent_date_norm in matching_neighbors:
            if recent_id == unit_id:
                continue

            # Calculate proximity weight (1.0 = identical time, 0.3 = edge of window)
            # recent_date_norm is guaranteed datetime here
            time_diff = abs((unit_date - recent_date_norm).total_seconds() / 3600)
            weight = max(0.3, 1.0 - (time_diff / time_window_hours))

            links.append(
                {
                    'from_unit_id': unit_id,
                    'to_unit_id': recent_id,
                    'link_type': 'temporal',
                    'weight': weight,
                    'entity_id': None,
                }
            )

    return links


async def _get_unit_dates(session: AsyncSession, unit_ids: list[str]) -> dict[str, datetime]:
    """
    Fetch event_date for specific unit IDs.

    Args:
        session: Active DB session.
        unit_ids: List of unit IDs (strings).

    Returns:
        Dictionary mapping unit ID string to event_date datetime.
    """
    u_ids = [UUID(u) for u in unit_ids]
    stmt = select(MemoryUnit.id, MemoryUnit.event_date).where(col(MemoryUnit.id).in_(u_ids))
    results = await session.exec(stmt)
    return {str(uid): date for uid, date in results.all()}


async def _get_temporal_candidates(
    session: AsyncSession,
    min_date: datetime,
    max_date: datetime,
    exclude_ids: list[str],
) -> Sequence[tuple[UUID, datetime]]:
    """
    Fetch all memory units within a specific date range, excluding specific IDs.

    Args:
        session: Active DB session.
        min_date: Start of time window.
        max_date: End of time window.
        exclude_ids: List of IDs to exclude (usually the new units themselves).

    Returns:
        Sequence of tuples (unit_id, event_date).
    """
    exclude_uuids = [UUID(u) for u in exclude_ids]
    cand_stmt = (
        select(MemoryUnit.id, MemoryUnit.event_date)
        .where(col(MemoryUnit.event_date) >= min_date)
        .where(col(MemoryUnit.event_date) <= max_date)
        .where(col(MemoryUnit.id).notin_(exclude_uuids))
        .order_by(col(MemoryUnit.event_date).desc())
    )
    # .all() returns a Sequence (list-like) of Row objects
    return (await session.exec(cand_stmt)).all()


async def create_temporal_links_batch_per_fact(
    session: AsyncSession,
    unit_ids: list[str],
    time_window_hours: int = 24,
) -> int:
    """
    Orchestrate the creation of temporal links for a batch of units.

    1. Fetches dates for the new units.
    2. Calculates the overall time bounds.
    3. Fetches all candidate neighbors in that range.
    4. Computes links in memory.
    5. Bulk inserts to DB.

    Args:
        session: Active DB session.
        unit_ids: List of new unit IDs to process.
        time_window_hours: Window size in hours.

    Returns:
        Number of links created.
    """
    if not unit_ids:
        return 0

    try:
        # 1. Get dates for the inputs
        new_units = await _get_unit_dates(session, unit_ids)
        dates = [d for d in new_units.values() if d]

        if not dates:
            return 0

        # 2. Determine efficient query bounds
        min_date = min(dates) - timedelta(hours=time_window_hours)
        max_date = max(dates) + timedelta(hours=time_window_hours)

        # 3. Fetch candidates
        candidates = await _get_temporal_candidates(session, min_date, max_date, unit_ids)

        # 4. Logic
        # Converting Sequence to list to match type hint if strict, though Sequence is usually compatible
        links_data = compute_temporal_links(new_units, list(candidates), time_window_hours)

        # 5. Persist
        await _bulk_insert_memory_links(session, links_data)

        logger.info(f'Created {len(links_data)} temporal links')
        return len(links_data)

    except Exception as e:
        logger.error(f'Failed to create temporal links: {e}')
        return 0


def _flatten_llm_entities(
    unit_ids: list[str],
    llm_entities: list[list[dict]],
    fact_dates: list[datetime],
) -> tuple[list[dict], list[int]]:
    """
    Flatten nested entity lists from LLM extraction into a linear list for batch processing.

    Args:
        unit_ids: List of source unit IDs.
        llm_entities: List of list of entity dicts (one list per unit).
        fact_dates: List of dates corresponding to units.

    Returns:
        Tuple containing:
        - List of flat entity dictionaries with 'text', 'type', 'event_date'.
        - List of indices mapping flat entries back to original unit_ids index.
    """
    all_entities_flat = []
    flat_idx_map = []

    for u_idx, (entity_list, date) in enumerate(zip(llm_entities, fact_dates)):
        for ent in entity_list:
            ent_text = ent.get('text') if isinstance(ent, dict) else getattr(ent, 'text', '')

            if ent_text:
                all_entities_flat.append({'text': ent_text, 'event_date': date})
                flat_idx_map.append(u_idx)

    return all_entities_flat, flat_idx_map


async def _fetch_units_by_entity_ids(
    session: AsyncSession, entity_uuids: list[UUID]
) -> dict[str, list[UUID]]:
    """
    Fetch all Unit IDs associated with a list of Entity IDs.

    Args:
        session: Active DB session.
        entity_uuids: List of Entity UUIDs.

    Returns:
        Map {entity_id_str: [unit_uuid, ...]}.
    """
    stmt = select(UnitEntity.entity_id, UnitEntity.unit_id).where(
        col(UnitEntity.entity_id).in_(entity_uuids)
    )
    rows = await session.exec(stmt)

    entity_to_units = defaultdict(list)
    for eid, uid in rows.all():
        eid_str = str(eid)
        entity_to_units[eid_str].append(uid)
    return entity_to_units


def _generate_entity_graph_links(
    entity_to_units: dict[str, list[UUID]],
    new_unit_ids: list[str],
    max_links: int = 50,
) -> list[EntityLink]:
    """
    Generate Unit-to-Unit links based on shared entities.

    Connects new units to other new units (internal) and new units to existing units.

    Args:
        entity_to_units: Map of entity_id -> list of unit_ids.
        new_unit_ids: List of unit IDs currently being processed.
        max_links: Max number of existing units to link to (avoids explosion).

    Returns:
        List of EntityLink objects.
    """
    links: list[EntityLink] = []
    new_unit_set = set(UUID(u) for u in new_unit_ids)

    for eid_str, units in entity_to_units.items():
        entity_uuid = UUID(eid_str)

        # Split into new (this batch) vs existing
        current_new = [u for u in units if u in new_unit_set]
        current_existing = [u for u in units if u not in new_unit_set]

        # Limit existing to most recent to avoid N^2 explosion for common entities
        current_existing = current_existing[-max_links:]

        # Link New <-> New
        for i, u1 in enumerate(current_new):
            for u2 in current_new[i + 1 :]:
                links.append(EntityLink(from_unit_id=u1, to_unit_id=u2, entity_id=entity_uuid))
                links.append(EntityLink(from_unit_id=u2, to_unit_id=u1, entity_id=entity_uuid))

        # Link New <-> Existing
        for u_new in current_new:
            for u_exist in current_existing:
                links.append(
                    EntityLink(
                        from_unit_id=u_new,
                        to_unit_id=u_exist,
                        entity_id=entity_uuid,
                    )
                )
                links.append(
                    EntityLink(
                        from_unit_id=u_exist,
                        to_unit_id=u_new,
                        entity_id=entity_uuid,
                    )
                )

    return links


async def extract_entities_batch_optimized(
    entity_resolver: Any,
    session: AsyncSession,
    unit_ids: list[str],
    sentences: list[str],
    context: str,
    fact_dates: list[datetime],
    llm_entities: list[list[dict]],
) -> list[EntityLink]:
    """
    Orchestrate: Process LLM-extracted entities, resolve them, and create links.

    1. Prepares entities for resolution.
    2. Calls entity_resolver to map text to canonical IDs.
    3. Creates Unit->Entity links.
    4. Creates Unit->Unit links (via shared entities).

    Args:
        entity_resolver: Instance of EntityResolver service.
        session: Active DB session.
        unit_ids: IDs of units being processed.
        sentences: Raw text of facts (unused here but part of interface).
        context: Context string for resolution.
        fact_dates: Dates for resolution scoring.
        llm_entities: Entities extracted by LLM.

    Returns:
        List of created EntityLink objects.
    """
    try:
        # 1. Flatten
        all_entities_flat, flat_idx_map = _flatten_llm_entities(unit_ids, llm_entities, fact_dates)

        if not all_entities_flat:
            return []

        # 2. Resolve
        resolved_ids = await entity_resolver.resolve_entities_batch(
            session=session,
            entities_data=all_entities_flat,
            context=context,
            unit_event_date=None,
        )

        # 3. Link Unit -> Entity
        unit_entity_pairs = []
        unit_to_entity_ids = set()

        for i, entity_id_str in enumerate(resolved_ids):
            if not entity_id_str:
                continue

            u_idx = flat_idx_map[i]
            unit_id = unit_ids[u_idx]

            unit_entity_pairs.append((unit_id, entity_id_str))
            unit_to_entity_ids.add(entity_id_str)

        await entity_resolver.link_units_to_entities_batch(session, unit_entity_pairs)

        # 4. Link Unit <-> Unit (Graph)
        if not unit_to_entity_ids:
            return []

        entity_uuids = [UUID(eid) for eid in unit_to_entity_ids]
        entity_to_units = await _fetch_units_by_entity_ids(session, entity_uuids)
        links = _generate_entity_graph_links(entity_to_units, unit_ids)

        logger.info(f'Resolved {len(resolved_ids)} entities, created {len(links)} links')
        return links

    except Exception as e:
        logger.error(f'Entity extraction batch failed: {e}')
        raise


async def create_semantic_links_batch(
    session: AsyncSession,
    unit_ids: list[str],
    embeddings: list[list[float]],
    top_k: int = 5,
    threshold: float = 0.7,
) -> int:
    """
    Orchestrate creation of semantic links.

    Uses pgvector (via storage.find_similar_facts) to efficiently find
    nearest neighbors in the database for each new fact.

    Args:
        session: Active DB session.
        unit_ids: IDs of new units.
        embeddings: Vectors for new units.
        top_k: Max neighbors per unit.
        threshold: Similarity threshold.

    Returns:
        Number of links created.
    """
    if not unit_ids or not embeddings:
        return 0

    try:
        u_ids_uuid = [UUID(u) for u in unit_ids]
        all_links_data = []

        # We can run these searches in parallel
        async def _process_single_embedding(
            index: int,
            u_id: UUID,
            emb: list[float],
        ) -> list[dict[str, Any]]:
            # Exclude self from search
            exclude = [u_id]

            similar_items = await storage.find_similar_facts(
                session,
                emb,
                limit=top_k,
                threshold=threshold,
                exclude_ids=exclude,
            )

            links = []
            for target_uuid, score in similar_items:
                # find_similar_facts returns similarity (0 to 1) if using 1-distance
                # but let's ensure we use the score provided
                links.append(
                    {
                        'from_unit_id': u_id,
                        'to_unit_id': target_uuid,
                        'link_type': 'semantic',
                        'weight': float(score),
                        'entity_id': None,
                    }
                )
            return links

        # Execute sequentially as AsyncSession is not concurrency-safe
        for i in range(len(unit_ids)):
            links = await _process_single_embedding(i, u_ids_uuid[i], embeddings[i])
            all_links_data.extend(links)

        if all_links_data:
            await _bulk_insert_memory_links(session, all_links_data)

        logger.info(f'Created {len(all_links_data)} semantic links')
        return len(all_links_data)

    except Exception as e:
        logger.error(f'Semantic link batch failed: {e}')
        return 0


def _build_causal_link_data(
    unit_ids: list[str], causal_relations_per_fact: list[list[dict]]
) -> list[dict[str, Any]]:
    """
    Validate and construct causal link dictionaries from LLM output.

    Args:
        unit_ids: List of unit IDs.
        causal_relations_per_fact: Nested list of causal relation dicts.

    Returns:
        List of link dictionaries.
    """
    u_ids_uuid = [UUID(u) for u in unit_ids]
    links_data = []
    valid_types = {'causes', 'caused_by', 'enables', 'prevents'}

    for i, relations in enumerate(causal_relations_per_fact):
        from_id = u_ids_uuid[i]

        for rel in relations:
            target_idx = rel.get('target_fact_index')
            rel_type = rel.get('relation_type')
            strength = rel.get('strength', 1.0)

            if rel_type not in valid_types:
                continue

            if target_idx is None or not (0 <= target_idx < len(unit_ids)):
                continue

            if i == target_idx:
                continue

            links_data.append(
                {
                    'from_unit_id': from_id,
                    'to_unit_id': u_ids_uuid[target_idx],
                    'link_type': rel_type,
                    'weight': float(strength),
                    'entity_id': None,
                }
            )
    return links_data


async def create_causal_links_batch(
    session: AsyncSession,
    unit_ids: list[str],
    causal_relations_per_fact: list[list[dict]],
) -> int:
    """
    Orchestrate creation of causal links from LLM extraction.

    Args:
        session: Active DB session.
        unit_ids: List of unit IDs.
        causal_relations_per_fact: Extracted causal relationships.

    Returns:
        Number of links created.
    """
    if not unit_ids or not causal_relations_per_fact:
        return 0

    if len(unit_ids) != len(causal_relations_per_fact):
        raise ValueError(
            f'Mismatch between unit_ids ({len(unit_ids)}) '
            f'and causal_relations ({len(causal_relations_per_fact)})'
        )

    try:
        links_data = _build_causal_link_data(unit_ids, causal_relations_per_fact)
        await _bulk_insert_memory_links(session, links_data)
        return len(links_data)

    except Exception as e:
        logger.error(f'Causal link batch failed: {e}')
        return 0


async def create_temporal_links_batch(
    session: AsyncSession, unit_ids: list[str], time_window_hours: int = 24
) -> int:
    """
    Create temporal links between facts.

    Links facts that occurred close in time to each other.

    Args:
        session: Active database session.
        unit_ids: List of unit IDs to create links for.
        time_window_hours: Window size in hours.

    Returns:
        Number of temporal links created.
    """
    if not unit_ids:
        return 0

    return await create_temporal_links_batch_per_fact(session, unit_ids, time_window_hours)


async def insert_entity_links_batch(session: AsyncSession, links: list[EntityLink]) -> None:
    """
    Bulk insert EntityLink objects into the database.

    Args:
        session: Active DB session.
        links: List of EntityLink objects.
    """
    if not links:
        return

    data = [
        {
            'from_unit_id': link.from_unit_id,
            'to_unit_id': link.to_unit_id,
            'link_type': link.link_type,
            'weight': link.weight,
            'entity_id': link.entity_id,
        }
        for link in links
    ]

    await _bulk_insert_memory_links(session, data)
