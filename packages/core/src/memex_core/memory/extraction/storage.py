"""
Fact storage for retain pipeline.

Handles insertion of facts into the database using SQLModel.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import case, type_coerce, update
from sqlalchemy.dialects.postgresql import insert as pg_insert, UUID as SA_UUID
from sqlmodel import col, delete, func, select, and_
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.context import get_session_id

from memex_core.memory.sql_models import Document, MemoryUnit, Chunk, Node, ContentStatus
from memex_core.memory.extraction.models import ProcessedFact, ChunkMetadata

logger = logging.getLogger('memex.core.memory.extraction.storage')


async def insert_facts_batch(
    session: AsyncSession,
    facts: list[ProcessedFact],
    document_id: str | None = None,
) -> list[str]:
    """
    Insert facts into the database in batch using high-performance Core Insert.

    Args:
        session: Active database session.
        facts: List of ProcessedFact objects to insert.
        document_id: Optional document ID to associate with facts.

    Returns:
        List of unit IDs (UUIDs as strings) for the inserted facts.
    """
    if not facts:
        return []

    insert_data = []

    for fact in facts:
        # Determine effective document_id
        effective_doc_id = fact.document_id if fact.document_id else document_id

        # Determine effective event_date
        event_date = fact.occurred_start if fact.occurred_start is not None else fact.mentioned_at

        # Merge specific fields into metadata if not present on model
        # (Assuming 'tags' and 'chunk_id' go into unit_metadata based on previous schema)
        metadata_merged: dict[str, str | list[str]] = (
            {k: v for k, v in fact.payload.items()} if fact.payload is not None else {}
        )
        if fact.tags:
            metadata_merged['tags'] = fact.tags
        if fact.chunk_id:
            metadata_merged['chunk_id'] = fact.chunk_id

        row = {
            'text': fact.fact_text,
            'embedding': fact.embedding,
            'event_date': event_date,
            'occurred_start': fact.occurred_start,
            'occurred_end': fact.occurred_end,
            'mentioned_at': fact.mentioned_at,
            'context': fact.context,
            'fact_type': fact.fact_type,
            'confidence_alpha': fact.confidence_alpha,
            'confidence_beta': fact.confidence_beta,
            'access_count': 0,
            'unit_metadata': metadata_merged,
            'document_id': UUID(effective_doc_id) if effective_doc_id else None,
            'chunk_id': UUID(fact.chunk_id) if fact.chunk_id else None,
            'vault_id': fact.vault_id if fact.vault_id else GLOBAL_VAULT_ID,
            'status': ContentStatus.ACTIVE,
        }
        if fact.chunk_id:
            logger.debug(f'Linking fact to chunk_id: {fact.chunk_id}')
        insert_data.append(row)

    # Execute Bulk Insert with Returning
    stmt = pg_insert(MemoryUnit).values(insert_data).returning(MemoryUnit.id)

    results = await session.exec(stmt)

    return [str(row[0]) for row in results.all()]


async def handle_document_tracking(
    session: AsyncSession,
    document_id: str,
    combined_content: str,
    is_first_batch: bool,
    retain_params: dict | None = None,
    document_tags: list[str] | None = None,
    vault_id: UUID = GLOBAL_VAULT_ID,
    assets: list[str] | None = None,
    content_fingerprint: str | None = None,
    publish_date: datetime | None = None,
    title: str | None = None,
) -> None:
    """
    Handle document tracking: delete old version (if start) and upsert new metadata.

    Args:
        session: Active database session.
        document_id: Document identifier.
        combined_content: Combined content text.
        is_first_batch: Whether this is the first batch (triggers deletion of old data).
        retain_params: Optional parameters passed during retain.
        document_tags: Optional list of tags.
        vault_id: Vault ID for the document.
        assets: Optional list of asset paths.
        content_fingerprint: Optional explicit content hash. If None, SHA256 of text is used.
        title: Resolved human-readable title for the document.
    """
    doc_uuid = UUID(document_id)

    if content_fingerprint:
        content_hash = content_fingerprint
    else:
        content_hash = hashlib.sha256(combined_content.encode()).hexdigest()

    # 1. Delete old document if this is the first batch
    if is_first_batch:
        delete_stmt = delete(Document).where(col(Document.id) == doc_uuid)
        await session.exec(delete_stmt)

    # Prepare metadata (merging tags and params into doc_metadata)
    doc_metadata: dict[str, str | list[str] | dict] = {}
    if retain_params:
        doc_metadata['retain_params'] = retain_params
        if 'source_uri' in retain_params and retain_params['source_uri']:
            doc_metadata['source_uri'] = retain_params['source_uri']

    if document_tags:
        doc_metadata['tags'] = document_tags

    # 2. Upsert Document
    # We define the base insert statement first using the underlying __table__
    # to avoid SQLModel's 'metadata' attribute shadowing.
    # Prefer explicit title; fall back to note_name from retain_params
    effective_title = title or (retain_params.get('note_name') if retain_params else None)

    values = {
        'id': doc_uuid,
        'title': effective_title,
        'original_text': combined_content,
        'content_hash': content_hash,
        'metadata': doc_metadata,
        'vault_id': vault_id,
        'filestore_path': retain_params.get('filestore_path') if retain_params else None,
        'assets': assets or [],
        'session_id': get_session_id(),
        'publish_date': publish_date,
        # created_at/updated_at handled by server_default
    }

    insert_stmt = pg_insert(Document.__table__).values(**values)  # type: ignore[attr-defined]

    # Now we reference 'insert_stmt' in the on_conflict clause
    # Logic: If we are tracking a doc, we overwrite its vault_id if provided.
    set_clause = {
        'title': insert_stmt.excluded.title,
        'original_text': insert_stmt.excluded.original_text,
        'content_hash': insert_stmt.excluded.content_hash,
        'metadata': insert_stmt.excluded.metadata,
        'updated_at': func.now(),
        'vault_id': insert_stmt.excluded.vault_id,
        'filestore_path': insert_stmt.excluded.filestore_path,
        'assets': insert_stmt.excluded.assets,
        'session_id': insert_stmt.excluded.session_id,
        'publish_date': insert_stmt.excluded.publish_date,
    }

    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=['id'],
        set_=set_clause,
    )

    await session.exec(upsert_stmt)


async def store_chunks_batch(
    session: AsyncSession,
    document_id: str,
    chunks: list[ChunkMetadata],
    vault_id: UUID = GLOBAL_VAULT_ID,
) -> dict[int, str]:
    """
    Store document chunks using upsert with ON CONFLICT on (document_id, content_hash).

    If a chunk with the same content_hash already exists (possibly stale), it is
    reactivated (status set to ACTIVE) and its chunk_index is updated.

    Args:
        session: Active database session.
        document_id: Document identifier.
        chunks: List of ChunkMetadata objects.
        vault_id: Vault ID.

    Returns:
        Dictionary mapping chunk index to chunk_id (UUID string).
    """
    if not chunks:
        return {}

    doc_uuid = UUID(document_id)
    insert_data = []

    for chunk in chunks:
        row = {
            'document_id': doc_uuid,
            'text': chunk.chunk_text,
            'chunk_index': chunk.chunk_index,
            'vault_id': vault_id,
            'embedding': chunk.embedding,
            'content_hash': chunk.content_hash,
            'status': ContentStatus.ACTIVE,
        }
        insert_data.append(row)

    stmt = (
        pg_insert(Chunk)
        .values(insert_data)
        .on_conflict_do_update(
            constraint='uq_chunks_document_content_hash',
            set_={
                'chunk_index': pg_insert(Chunk).excluded.chunk_index,
                'status': ContentStatus.ACTIVE,
                'embedding': pg_insert(Chunk).excluded.embedding,
            },
        )
        .returning(Chunk.id, Chunk.chunk_index)
    )

    results = await session.exec(stmt)

    return {row[1]: str(row[0]) for row in results.all()}


async def find_similar_facts(
    session: AsyncSession,
    embedding: list[float],
    limit: int = 5,
    threshold: float = 0.8,
    exclude_ids: list[UUID] | None = None,
    fact_type: str | None = None,
    vault_ids: list[UUID] | None = None,
) -> list[tuple[UUID, float]]:
    """
    Find semantically similar facts using vector cosine distance.

    Args:
        session: Active database session.
        embedding: Query embedding vector.
        limit: Max number of results.
        threshold: Minimum similarity score (0 to 1).
        exclude_ids: List of UUIDs to exclude from results.
        fact_type: Optional filter for fact type.
        vault_ids: Optional list of Vault IDs to scope search. If None/Empty, search all.

    Returns:
        List of (unit_id, similarity_score) tuples.
    """
    if exclude_ids is None:
        exclude_ids = []

    from typing import Any, cast

    # 1 - cosine_distance = cosine_similarity
    similarity = 1 - cast(Any, col(MemoryUnit.embedding)).cosine_distance(embedding)

    statement = (
        select(MemoryUnit.id, similarity)
        .where(similarity >= threshold)
        .where(col(MemoryUnit.status) == ContentStatus.ACTIVE)
        .order_by(similarity.desc())
        .limit(limit)
    )

    if fact_type:
        statement = statement.where(col(MemoryUnit.fact_type) == fact_type)

    if exclude_ids:
        statement = statement.where(col(MemoryUnit.id).not_in(exclude_ids))

    # Vault Scoping: Strict IN check or Global Search
    if vault_ids:
        statement = statement.where(col(MemoryUnit.vault_id).in_(vault_ids))

    results = await session.exec(statement)
    return [(row[0], float(row[1])) for row in results.all()]


async def update_fact_confidence(
    session: AsyncSession,
    unit_id: UUID,
    alpha_delta: float,
    beta_delta: float,
) -> None:
    """
    Update the confidence alpha/beta of a MemoryUnit by adding deltas.
    Also increments access_count and updates mentions.

    Args:
        session: Active database session.
        unit_id: UUID of the unit to update.
        alpha_delta: Amount to add to confidence_alpha.
        beta_delta: Amount to add to confidence_beta.
    """
    # Use direct update for atomicity
    # We coalesce with 1.0 (default prior) if null, though opinions should have it.

    update_stmt = (
        update(MemoryUnit)
        .where(col(MemoryUnit.id) == unit_id)
        .values(
            confidence_alpha=func.coalesce(col(MemoryUnit.confidence_alpha), 1.0) + alpha_delta,
            confidence_beta=func.coalesce(col(MemoryUnit.confidence_beta), 1.0) + beta_delta,
            access_count=col(MemoryUnit.access_count) + 1,
            mentioned_at=datetime.now(timezone.utc),
        )
    )

    await session.exec(update_stmt)


async def check_duplicates_in_window(
    session: AsyncSession,
    texts: list[str],
    embeddings: list[list[float]],
    target_date: datetime,
    window_hours: int = 24,
    similarity_threshold: float = 0.95,
    vault_ids: list[UUID] | None = None,
) -> list[bool]:
    """
    Check for duplicates within a time window using exact text match and cosine similarity.
    Scoped to the provided vault_ids. If None, checks ALL vaults.

    Args:
        session: Active database session.
        texts: List of new fact texts.
        embeddings: List of new fact embeddings.
        target_date: Central date for the window.
        window_hours: Total window size in hours (centered on target_date).
        similarity_threshold: Threshold for semantic duplication.
        vault_ids: Optional list of Vault IDs to scope the check.

    Returns:
        List of booleans where True means the fact is a duplicate.
    """
    if not texts:
        return []

    # Calculate window
    delta = timedelta(hours=window_hours / 2)
    start_date = target_date - delta
    end_date = target_date + delta

    count = len(texts)
    is_duplicate = [False] * count

    # 1. Batch Exact Match Check
    stmt_exact = (
        select(MemoryUnit.text)
        .where(
            and_(
                col(MemoryUnit.event_date) >= start_date,
                col(MemoryUnit.event_date) <= end_date,
            )
        )
        .where(col(MemoryUnit.text).in_(texts))
    )

    # Vault Scoping
    if vault_ids:
        stmt_exact = stmt_exact.where(col(MemoryUnit.vault_id).in_(vault_ids))

    existing_texts_result = await session.exec(stmt_exact)
    existing_texts = set(existing_texts_result.all())

    # Mark exact matches
    indices_to_check_semantic = []

    for i, text in enumerate(texts):
        if text in existing_texts:
            is_duplicate[i] = True
        else:
            indices_to_check_semantic.append(i)

    # 2. Semantic Check
    distance_threshold = 1.0 - similarity_threshold

    for i in indices_to_check_semantic:
        emb = embeddings[i]
        from typing import Any, cast

        similarity_expr = (
            cast(Any, col(MemoryUnit.embedding)).cosine_distance(emb) < distance_threshold
        )

        statement = (
            select(1)
            .where(
                and_(
                    col(MemoryUnit.event_date) >= start_date,
                    col(MemoryUnit.event_date) <= end_date,
                )
            )
            .where(similarity_expr)
            .limit(1)
        )

        # Vault Scoping
        if vault_ids:
            statement = statement.where(col(MemoryUnit.vault_id).in_(vault_ids))

        result = await session.exec(statement)
        if result.first() is not None:
            is_duplicate[i] = True

    return is_duplicate


async def find_temporal_neighbor(
    session: AsyncSession,
    timestamp: datetime,
    direction: str = 'before',
    exclude_ids: list[UUID] | None = None,
) -> UUID | None:
    """
    Find the closest memory unit chronologically before or after the timestamp.

    Args:
        session: Active database session.
        timestamp: The reference timestamp.
        direction: 'before' or 'after'.
        exclude_ids: Optional list of UUIDs to exclude (e.g., current batch).

    Returns:
        UUID of the closest neighbor, or None if none found.
    """
    if direction == 'before':
        # Find max date where date < timestamp
        statement = (
            select(MemoryUnit.id)
            .where(col(MemoryUnit.event_date) < timestamp)
            .order_by(col(MemoryUnit.event_date).desc())
            .limit(1)
        )
    elif direction == 'after':
        # Find min date where date > timestamp
        statement = (
            select(MemoryUnit.id)
            .where(col(MemoryUnit.event_date) > timestamp)
            .order_by(col(MemoryUnit.event_date).asc())
            .limit(1)
        )
    else:
        raise ValueError("direction must be 'before' or 'after'")

    if exclude_ids:
        statement = statement.where(col(MemoryUnit.id).not_in(exclude_ids))

    result = await session.exec(statement)
    return result.first()


# --- Incremental ingestion storage functions ---


async def get_document_blocks(
    session: AsyncSession,
    document_id: str,
) -> list[dict[str, object]]:
    """Retrieve existing block hashes and metadata for a document.

    Returns a list of dicts with keys: ``id``, ``content_hash``, ``chunk_index``.
    Uses ``SELECT ... FOR UPDATE`` to serialize concurrent updates to the same
    document.

    Args:
        session: Active database session.
        document_id: Document identifier.

    Returns:
        List of block metadata dicts for all active blocks.
    """
    doc_uuid = UUID(document_id)
    stmt = (
        select(Chunk.id, Chunk.content_hash, Chunk.chunk_index)
        .where(
            and_(
                col(Chunk.document_id) == doc_uuid,
                col(Chunk.status) == ContentStatus.ACTIVE,
            )
        )
        .with_for_update()
    )
    results = await session.exec(stmt)
    return [{'id': row[0], 'content_hash': row[1], 'chunk_index': row[2]} for row in results.all()]


async def mark_blocks_stale(
    session: AsyncSession,
    block_ids: list[UUID],
) -> None:
    """Set status to 'stale' on the given chunk IDs.

    Args:
        session: Active database session.
        block_ids: List of chunk UUIDs to mark stale.
    """
    if not block_ids:
        return
    stmt = update(Chunk).where(col(Chunk.id).in_(block_ids)).values(status=ContentStatus.STALE)
    await session.exec(stmt)


async def mark_memory_units_stale(
    session: AsyncSession,
    chunk_ids: list[UUID],
) -> None:
    """Set status to 'stale' on all memory units linked to the given chunk IDs.

    Args:
        session: Active database session.
        chunk_ids: List of chunk UUIDs whose memory units should be staled.
    """
    if not chunk_ids:
        return
    stmt = (
        update(MemoryUnit)
        .where(col(MemoryUnit.chunk_id).in_(chunk_ids))
        .values(status=ContentStatus.STALE)
    )
    await session.exec(stmt)


async def reindex_blocks(
    session: AsyncSession,
    block_updates: list[tuple[UUID, int]],
) -> None:
    """Batch-update chunk_index for retained blocks.

    Args:
        session: Active database session.
        block_updates: List of ``(chunk_id, new_chunk_index)`` tuples.
    """
    for chunk_id, new_index in block_updates:
        stmt = update(Chunk).where(col(Chunk.id) == chunk_id).values(chunk_index=new_index)
        await session.exec(stmt)


# --- Node CRUD operations ---


async def insert_nodes_batch(
    session: AsyncSession,
    nodes_data: list[dict[str, object]],
) -> list[str]:
    """Bulk insert nodes into the Node table.

    Uses upsert with ON CONFLICT on (document_id, node_hash) to handle
    re-indexing of existing documents.

    Args:
        session: Active database session.
        nodes_data: List of dicts with Node column values.

    Returns:
        List of node IDs (UUIDs as strings) for the inserted/upserted nodes.
    """
    if not nodes_data:
        return []

    stmt = (
        pg_insert(Node)
        .values(nodes_data)
        .on_conflict_do_update(
            constraint='uq_nodes_document_node_hash',
            set_={
                'title': pg_insert(Node).excluded.title,
                'text': pg_insert(Node).excluded.text,
                'summary': pg_insert(Node).excluded.summary,
                'summary_formatted': pg_insert(Node).excluded.summary_formatted,
                'level': pg_insert(Node).excluded.level,
                'seq': pg_insert(Node).excluded.seq,
                'token_estimate': pg_insert(Node).excluded.token_estimate,
                'block_id': pg_insert(Node).excluded.block_id,
                'status': ContentStatus.ACTIVE,
            },
        )
        .returning(Node.id)
    )

    results = await session.exec(stmt)
    return [str(row[0]) for row in results.all()]


async def get_document_nodes(
    session: AsyncSession,
    document_id: str,
) -> list[dict[str, object]]:
    """Fetch all active nodes for a document.

    Args:
        session: Active database session.
        document_id: Document identifier.

    Returns:
        List of node metadata dicts with keys: id, node_hash, block_id, seq.
    """
    doc_uuid = UUID(document_id)
    stmt = (
        select(Node.id, Node.node_hash, Node.block_id, Node.seq)
        .where(
            and_(
                col(Node.document_id) == doc_uuid,
                col(Node.status) == ContentStatus.ACTIVE,
            )
        )
        .order_by(col(Node.seq))
    )
    results = await session.exec(stmt)
    return [
        {'id': row[0], 'node_hash': row[1], 'block_id': row[2], 'seq': row[3]}
        for row in results.all()
    ]


async def backfill_node_block_ids(
    session: AsyncSession,
    document_id: str,
    node_hash_to_block_id: dict[str, UUID],
) -> None:
    """Set Node.block_id for nodes matching the given node_hash -> chunk UUID mapping.

    Uses a bulk CASE/WHEN UPDATE for efficiency.

    Args:
        session: Active database session.
        document_id: Document identifier.
        node_hash_to_block_id: Mapping of node_hash string to chunk UUID.
    """
    if not node_hash_to_block_id:
        return

    doc_uuid = UUID(document_id)
    whens = [
        (Node.node_hash == nh, type_coerce(bid, SA_UUID()))
        for nh, bid in node_hash_to_block_id.items()
    ]
    stmt = (
        update(Node)
        .where(
            and_(
                col(Node.document_id) == doc_uuid,
                col(Node.node_hash).in_(list(node_hash_to_block_id.keys())),
                col(Node.status) == ContentStatus.ACTIVE,
            )
        )
        .values(block_id=case(*whens))
    )
    await session.exec(stmt)


async def migrate_facts_to_chunks(
    session: AsyncSession,
    chunk_id_mapping: dict[UUID, UUID],
) -> None:
    """Reassign MemoryUnits from old chunks to new chunks (boundary shift migration).

    Args:
        session: Active database session.
        chunk_id_mapping: Mapping of old_chunk_id -> new_chunk_id.
    """
    if not chunk_id_mapping:
        return

    whens = [
        (MemoryUnit.chunk_id == old_id, type_coerce(new_id, SA_UUID()))
        for old_id, new_id in chunk_id_mapping.items()
    ]
    stmt = (
        update(MemoryUnit)
        .where(col(MemoryUnit.chunk_id).in_(list(chunk_id_mapping.keys())))
        .values(chunk_id=case(*whens))
    )
    await session.exec(stmt)


async def get_node_hashes_by_block(
    session: AsyncSession,
    document_id: str,
) -> dict[UUID, set[str]]:
    """Return {chunk_id -> set of node_hashes} for all active nodes with a block_id.

    Args:
        session: Active database session.
        document_id: Document identifier.

    Returns:
        Mapping of chunk UUID to the set of node_hash strings belonging to it.
    """
    doc_uuid = UUID(document_id)
    stmt = select(Node.block_id, Node.node_hash).where(
        and_(
            col(Node.document_id) == doc_uuid,
            col(Node.status) == ContentStatus.ACTIVE,
            col(Node.block_id).is_not(None),
        )
    )
    results = await session.exec(stmt)
    mapping: dict[UUID, set[str]] = {}
    for block_id, node_hash in results.all():
        mapping.setdefault(block_id, set()).add(str(node_hash))
    return mapping


async def mark_nodes_stale(
    session: AsyncSession,
    node_ids: list[UUID],
) -> None:
    """Set status to 'stale' on the given node IDs.

    Args:
        session: Active database session.
        node_ids: List of node UUIDs to mark stale.
    """
    if not node_ids:
        return
    stmt = update(Node).where(col(Node.id).in_(node_ids)).values(status=ContentStatus.STALE)
    await session.exec(stmt)


async def update_document_page_index(
    session: AsyncSession,
    document_id: str,
    page_index_json: dict | list | None,
) -> None:
    """Update the Document.page_index JSONB column.

    Args:
        session: Active database session.
        document_id: Document identifier.
        page_index_json: The thin tree structure to store, or None to clear.
    """
    doc_uuid = UUID(document_id)
    stmt = update(Document).where(col(Document.id) == doc_uuid).values(page_index=page_index_json)
    await session.exec(stmt)


async def update_document_title(
    session: AsyncSession,
    document_id: str,
    title: str,
) -> None:
    """Update the Document.title column.

    Args:
        session: Active database session.
        document_id: Document identifier.
        title: The resolved title to store.
    """
    doc_uuid = UUID(document_id)
    stmt = update(Document).where(col(Document.id) == doc_uuid).values(title=title)
    await session.exec(stmt)


async def get_block_text(
    session: AsyncSession,
    block_id: UUID,
) -> str:
    """Stitch node texts for a given block, ordered by seq.

    Args:
        session: Active database session.
        block_id: The chunk/block UUID.

    Returns:
        Concatenated text of all active nodes in the block.
    """
    stmt = (
        select(Node.text)
        .where(
            and_(
                col(Node.block_id) == block_id,
                col(Node.status) == ContentStatus.ACTIVE,
            )
        )
        .order_by(col(Node.seq))
    )
    results = await session.exec(stmt)
    texts = [row for row in results.all()]
    return '\n\n'.join(texts)


async def cleanup_orphaned_entities(
    session: AsyncSession,
) -> int:
    """Remove entities that have no remaining UnitEntity links.

    Intended to run as a periodic background maintenance task, NOT inline
    during user-facing operations. This avoids race conditions with concurrent
    ingestion.

    Returns:
        Number of orphaned entities deleted.
    """
    from memex_core.memory.sql_models import Entity, UnitEntity

    # Find entities with zero links
    subq = select(UnitEntity.entity_id).distinct().subquery()
    stmt = delete(Entity).where(col(Entity.id).not_in(select(subq.c.entity_id)))
    result = await session.exec(stmt)
    return result.rowcount  # type: ignore[return-value]
