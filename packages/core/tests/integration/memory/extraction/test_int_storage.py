import pytest
from datetime import datetime, timezone
from uuid import uuid4, UUID

from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import ProcessedFact, FactTypes, ChunkMetadata
from memex_core.memory.sql_models import MemoryUnit, Document, Chunk


@pytest.mark.asyncio
async def test_int_insert_facts_batch_db(session):
    # Arrange
    doc_id = uuid4()
    # Create the document first to satisfy FK
    doc = Document(id=doc_id, original_text='Test Doc')
    session.add(doc)
    await session.commit()

    fact = ProcessedFact(
        fact_text='Integration Test Fact',
        fact_type=FactTypes.WORLD,
        embedding=[0.0] * 384,
        mentioned_at=datetime.now(timezone.utc),
        payload={'test_key': 'test_val'},
        tags=['integration'],
        chunk_id=None,  # chunk_id must be a valid UUID or None
    )

    # Act
    ids = await storage.insert_facts_batch(session, [fact], document_id=str(doc_id))

    # Assert
    assert len(ids) == 1
    unit_id = UUID(ids[0])

    # Verify in DB
    db_unit = await session.get(MemoryUnit, unit_id)
    assert db_unit is not None
    assert db_unit.text == 'Integration Test Fact'
    assert db_unit.document_id == doc_id
    assert db_unit.unit_metadata['test_key'] == 'test_val'
    assert db_unit.unit_metadata['tags'] == ['integration']
    assert db_unit.unit_metadata.get('chunk_id') is None


@pytest.mark.asyncio
async def test_int_handle_document_tracking_db(session):
    # Arrange
    doc_id = uuid4()
    content_v1 = 'Content Version 1'

    # Act 1: First batch
    await storage.handle_document_tracking(
        session, str(doc_id), content_v1, is_first_batch=True, document_tags=['v1']
    )

    # Verify DB
    session.expire_all()
    doc = await session.get(Document, doc_id)
    assert doc is not None
    assert doc.original_text == content_v1
    assert doc.doc_metadata['tags'] == ['v1']

    # Act 2: Subsequent batch (Update)
    content_v2 = 'Content Version 2'
    await storage.handle_document_tracking(
        session, str(doc_id), content_v2, is_first_batch=False, document_tags=['v2']
    )

    # Verify DB (Should be updated)
    session.expire_all()
    doc = await session.get(Document, doc_id)
    assert doc.original_text == content_v2
    assert doc.doc_metadata['tags'] == ['v2']

    # Act 3: First batch again (Should delete and re-insert)
    # We'll check if it resets metadata or something, or just replaces row.
    # The logic deletes then inserts.
    content_v3 = 'Content Version 3'
    await storage.handle_document_tracking(
        session, str(doc_id), content_v3, is_first_batch=True, document_tags=['v3']
    )

    session.expire_all()
    doc = await session.get(Document, doc_id)
    assert doc.original_text == content_v3
    assert doc.doc_metadata['tags'] == ['v3']


@pytest.mark.asyncio
async def test_int_store_chunks_batch_db(session):
    from memex_core.memory.extraction.core import content_hash

    # Arrange
    doc_id = uuid4()
    doc = Document(id=doc_id, original_text='Chunk Test Doc')
    session.add(doc)
    await session.commit()

    chunks = [
        ChunkMetadata(
            chunk_text='Integration Chunk 1',
            chunk_index=0,
            fact_count=1,
            content_index=0,
            content_hash=content_hash('Integration Chunk 1'),
        ),
        ChunkMetadata(
            chunk_text='Integration Chunk 2',
            chunk_index=1,
            fact_count=2,
            content_index=0,
            content_hash=content_hash('Integration Chunk 2'),
        ),
    ]

    # Act
    chunk_map = await storage.store_chunks_batch(session, str(doc_id), chunks)

    # Assert
    assert len(chunk_map) == 2
    assert 0 in chunk_map
    assert 1 in chunk_map

    # Verify in DB
    session.expire_all()

    # Check chunk 0
    chunk0_id = UUID(chunk_map[0])
    db_chunk0 = await session.get(Chunk, chunk0_id)
    assert db_chunk0 is not None
    assert db_chunk0.text == 'Integration Chunk 1'
    assert db_chunk0.chunk_index == 0
    assert db_chunk0.document_id == doc_id

    # Check chunk 1
    chunk1_id = UUID(chunk_map[1])
    db_chunk1 = await session.get(Chunk, chunk1_id)
    assert db_chunk1 is not None
    assert db_chunk1.text == 'Integration Chunk 2'
    assert db_chunk1.chunk_index == 1
    assert db_chunk1.document_id == doc_id


@pytest.mark.asyncio
async def test_int_chunk_reactivation_on_reingest(session):
    """Test that stale chunks are reactivated when re-ingesting same content."""
    from memex_core.memory.sql_models import ContentStatus
    from memex_core.memory.extraction.core import content_hash

    # Arrange: Create document and store chunks
    doc_id = uuid4()
    doc = Document(id=doc_id, original_text='Test Doc')
    session.add(doc)
    await session.commit()

    chunk1_text = 'First paragraph content.'
    chunk2_text = 'Second paragraph content.'
    chunk3_text = 'Third paragraph content.'

    chunks_v1 = [
        ChunkMetadata(
            chunk_text=chunk1_text,
            chunk_index=0,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk1_text),
        ),
        ChunkMetadata(
            chunk_text=chunk2_text,
            chunk_index=1,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk2_text),
        ),
        ChunkMetadata(
            chunk_text=chunk3_text,
            chunk_index=2,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk3_text),
        ),
    ]

    # Act 1: Store initial chunks
    chunk_map_v1 = await storage.store_chunks_batch(session, str(doc_id), chunks_v1)
    await session.commit()

    # Assert 1: All chunks active
    session.expire_all()
    chunk1_id = UUID(chunk_map_v1[0])
    chunk2_id = UUID(chunk_map_v1[1])
    chunk3_id = UUID(chunk_map_v1[2])

    db_chunk1 = await session.get(Chunk, chunk1_id)
    db_chunk2 = await session.get(Chunk, chunk2_id)
    db_chunk3 = await session.get(Chunk, chunk3_id)

    assert db_chunk1.status == ContentStatus.ACTIVE
    assert db_chunk2.status == ContentStatus.ACTIVE
    assert db_chunk3.status == ContentStatus.ACTIVE

    # Act 2: Mark chunks 1 and 2 as stale (simulating truncated reingest)
    await storage.mark_blocks_stale(session, [chunk1_id, chunk2_id])
    await session.commit()

    session.expire_all()
    db_chunk1 = await session.get(Chunk, chunk1_id)
    db_chunk2 = await session.get(Chunk, chunk2_id)
    assert db_chunk1.status == ContentStatus.STALE
    assert db_chunk2.status == ContentStatus.STALE

    # Act 3: Re-ingest original content (chunks 1 and 2 should be reactivated)
    chunk_map_v2 = await storage.store_chunks_batch(session, str(doc_id), chunks_v1)
    await session.commit()

    # Assert 3: Chunks 1 and 2 are reactivated with same IDs
    session.expire_all()
    assert chunk_map_v2[0] == chunk_map_v1[0]  # Same ID
    assert chunk_map_v2[1] == chunk_map_v1[1]  # Same ID
    assert chunk_map_v2[2] == chunk_map_v1[2]  # Same ID

    db_chunk1 = await session.get(Chunk, chunk1_id)
    db_chunk2 = await session.get(Chunk, chunk2_id)
    db_chunk3 = await session.get(Chunk, chunk3_id)

    assert db_chunk1.status == ContentStatus.ACTIVE
    assert db_chunk2.status == ContentStatus.ACTIVE
    assert db_chunk3.status == ContentStatus.ACTIVE


@pytest.mark.asyncio
async def test_int_chunk_index_update_on_reingest(session):
    """Test that chunk_index is updated when chunk position changes."""
    from memex_core.memory.sql_models import ContentStatus
    from memex_core.memory.extraction.core import content_hash

    # Arrange
    doc_id = uuid4()
    doc = Document(id=doc_id, original_text='Test Doc')
    session.add(doc)
    await session.commit()

    chunk_a_text = 'Chunk A content.'
    chunk_b_text = 'Chunk B content.'

    # Initial order: A at index 0, B at index 1
    chunks_v1 = [
        ChunkMetadata(
            chunk_text=chunk_a_text,
            chunk_index=0,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk_a_text),
        ),
        ChunkMetadata(
            chunk_text=chunk_b_text,
            chunk_index=1,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk_b_text),
        ),
    ]

    chunk_map_v1 = await storage.store_chunks_batch(session, str(doc_id), chunks_v1)
    await session.commit()

    chunk_a_id = UUID(chunk_map_v1[0])
    chunk_b_id = UUID(chunk_map_v1[1])

    # Mark both as stale
    await storage.mark_blocks_stale(session, [chunk_a_id, chunk_b_id])
    await session.commit()

    # Reingest with swapped order: B at index 0, A at index 1
    chunks_v2 = [
        ChunkMetadata(
            chunk_text=chunk_b_text,
            chunk_index=0,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk_b_text),
        ),
        ChunkMetadata(
            chunk_text=chunk_a_text,
            chunk_index=1,
            fact_count=1,
            content_index=0,
            content_hash=content_hash(chunk_a_text),
        ),
    ]

    chunk_map_v2 = await storage.store_chunks_batch(session, str(doc_id), chunks_v2)
    await session.commit()

    # Assert: Same IDs but indices updated
    session.expire_all()
    assert chunk_map_v2[0] == str(chunk_b_id)  # B now at index 0
    assert chunk_map_v2[1] == str(chunk_a_id)  # A now at index 1

    db_chunk_a = await session.get(Chunk, chunk_a_id)
    db_chunk_b = await session.get(Chunk, chunk_b_id)

    assert db_chunk_a.chunk_index == 1  # A moved to index 1
    assert db_chunk_b.chunk_index == 0  # B moved to index 0
    assert db_chunk_a.status == ContentStatus.ACTIVE
    assert db_chunk_b.status == ContentStatus.ACTIVE
