"""Integration tests for incremental PageIndex extraction storage helpers.

Tests backfill_node_block_ids, migrate_facts_to_chunks, and get_node_hashes_by_block
against a real PostgreSQL database (testcontainers).
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4, UUID

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import (
    ProcessedFact,
    FactTypes,
    ChunkMetadata,
    content_hash_md5,
)
from memex_core.memory.sql_models import Note, Chunk, MemoryUnit, ContentStatus


async def _create_document(session: AsyncSession, doc_id: UUID) -> Note:
    doc = Note(id=doc_id, original_text='test doc')
    session.add(doc)
    await session.flush()
    return doc


async def _create_chunk(
    session: AsyncSession,
    doc_id: UUID,
    content_hash: str,
    chunk_index: int = 0,
) -> Chunk:
    chunks = [
        ChunkMetadata(
            chunk_text='chunk text',
            fact_count=0,
            content_index=0,
            chunk_index=chunk_index,
            content_hash=content_hash,
            embedding=[0.1] * 384,
        )
    ]
    chunk_map = await storage.store_chunks_batch(session, str(doc_id), chunks)
    chunk_uuid = UUID(chunk_map[chunk_index])
    chunk = await session.get(Chunk, chunk_uuid)
    assert chunk is not None
    return chunk


# --- backfill_node_block_ids ---


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_node_block_ids(session: AsyncSession):
    """backfill_node_block_ids sets block_id on nodes matching node_hash -> chunk mapping."""
    doc_id = uuid4()
    await _create_document(session, doc_id)

    # Create two chunks
    chunk_a = await _create_chunk(session, doc_id, 'hash_chunk_a', chunk_index=0)
    chunk_b = await _create_chunk(session, doc_id, 'hash_chunk_b', chunk_index=1)

    # Insert nodes (without block_id)
    node_hash_1 = content_hash_md5('content of node 1')
    node_hash_2 = content_hash_md5('content of node 2')

    node_rows = [
        {
            'note_id': doc_id,
            'node_hash': node_hash_1,
            'title': 'Node 1',
            'text': 'content of node 1',
            'level': 1,
            'seq': 0,
            'token_estimate': 10,
            'status': ContentStatus.ACTIVE,
        },
        {
            'note_id': doc_id,
            'node_hash': node_hash_2,
            'title': 'Node 2',
            'text': 'content of node 2',
            'level': 1,
            'seq': 1,
            'token_estimate': 10,
            'status': ContentStatus.ACTIVE,
        },
    ]
    node_ids = await storage.insert_nodes_batch(session, node_rows)
    assert len(node_ids) == 2

    # Backfill: node 1 -> chunk A, node 2 -> chunk B
    await storage.backfill_node_block_ids(
        session,
        str(doc_id),
        {node_hash_1: chunk_a.id, node_hash_2: chunk_b.id},
    )
    await session.flush()

    # Verify
    nodes = await storage.get_note_nodes(session, str(doc_id))
    node_by_hash = {str(n['node_hash']): n for n in nodes}
    assert node_by_hash[node_hash_1]['block_id'] == chunk_a.id
    assert node_by_hash[node_hash_2]['block_id'] == chunk_b.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_node_block_ids_empty_mapping(session: AsyncSession):
    """backfill_node_block_ids with empty mapping is a no-op."""
    doc_id = uuid4()
    await _create_document(session, doc_id)
    # Should not raise
    await storage.backfill_node_block_ids(session, str(doc_id), {})


# --- migrate_facts_to_chunks ---


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrate_facts_to_chunks(session: AsyncSession):
    """migrate_facts_to_chunks moves MemoryUnits from old to new chunk IDs."""
    doc_id = uuid4()
    await _create_document(session, doc_id)

    old_chunk = await _create_chunk(session, doc_id, 'old_hash', chunk_index=0)
    new_chunk = await _create_chunk(session, doc_id, 'new_hash', chunk_index=1)

    # Create a fact linked to the old chunk
    fact = ProcessedFact(
        fact_text='migrated fact',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        mentioned_at=datetime.now(timezone.utc),
        payload={},
        chunk_id=str(old_chunk.id),
    )
    unit_ids = await storage.insert_facts_batch(session, [fact], note_id=str(doc_id))
    unit_uuid = UUID(unit_ids[0])

    # Migrate
    await storage.migrate_facts_to_chunks(session, {old_chunk.id: new_chunk.id})
    await session.flush()

    # Verify
    unit = await session.get(MemoryUnit, unit_uuid)
    assert unit is not None
    assert unit.chunk_id == new_chunk.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrate_facts_to_chunks_empty(session: AsyncSession):
    """migrate_facts_to_chunks with empty mapping is a no-op."""
    await storage.migrate_facts_to_chunks(session, {})


# --- get_node_hashes_by_block ---


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_node_hashes_by_block(session: AsyncSession):
    """get_node_hashes_by_block returns {chunk_id -> set(node_hashes)}."""
    doc_id = uuid4()
    await _create_document(session, doc_id)

    chunk = await _create_chunk(session, doc_id, 'chunk_hash', chunk_index=0)

    node_hash_1 = content_hash_md5('node content 1')
    node_hash_2 = content_hash_md5('node content 2')

    node_rows = [
        {
            'note_id': doc_id,
            'node_hash': node_hash_1,
            'title': 'N1',
            'text': 'node content 1',
            'level': 1,
            'seq': 0,
            'token_estimate': 5,
            'block_id': chunk.id,
            'status': ContentStatus.ACTIVE,
        },
        {
            'note_id': doc_id,
            'node_hash': node_hash_2,
            'title': 'N2',
            'text': 'node content 2',
            'level': 1,
            'seq': 1,
            'token_estimate': 5,
            'block_id': chunk.id,
            'status': ContentStatus.ACTIVE,
        },
    ]
    await storage.insert_nodes_batch(session, node_rows)
    await session.flush()

    result = await storage.get_node_hashes_by_block(session, str(doc_id))
    assert chunk.id in result
    assert result[chunk.id] == {node_hash_1, node_hash_2}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_node_hashes_by_block_excludes_stale(session: AsyncSession):
    """get_node_hashes_by_block excludes stale nodes."""
    doc_id = uuid4()
    await _create_document(session, doc_id)

    chunk = await _create_chunk(session, doc_id, 'chunk_hash_2', chunk_index=0)

    node_hash_active = content_hash_md5('active node')
    node_hash_stale = content_hash_md5('stale node')

    node_rows = [
        {
            'note_id': doc_id,
            'node_hash': node_hash_active,
            'title': 'Active',
            'text': 'active node',
            'level': 1,
            'seq': 0,
            'token_estimate': 5,
            'block_id': chunk.id,
            'status': ContentStatus.ACTIVE,
        },
        {
            'note_id': doc_id,
            'node_hash': node_hash_stale,
            'title': 'Stale',
            'text': 'stale node',
            'level': 1,
            'seq': 1,
            'token_estimate': 5,
            'block_id': chunk.id,
            'status': ContentStatus.STALE,
        },
    ]
    await storage.insert_nodes_batch(session, node_rows)
    await session.flush()

    result = await storage.get_node_hashes_by_block(session, str(doc_id))
    assert chunk.id in result
    assert result[chunk.id] == {node_hash_active}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_node_hashes_by_block_excludes_null_block_id(session: AsyncSession):
    """get_node_hashes_by_block excludes nodes without block_id."""
    doc_id = uuid4()
    await _create_document(session, doc_id)

    node_hash = content_hash_md5('orphan node')
    node_rows = [
        {
            'note_id': doc_id,
            'node_hash': node_hash,
            'title': 'Orphan',
            'text': 'orphan node',
            'level': 1,
            'seq': 0,
            'token_estimate': 5,
            'status': ContentStatus.ACTIVE,
        },
    ]
    await storage.insert_nodes_batch(session, node_rows)
    await session.flush()

    result = await storage.get_node_hashes_by_block(session, str(doc_id))
    assert result == {}
