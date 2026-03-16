from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import ProcessedFact, FactTypes, ChunkMetadata

# --- Fixtures ---


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result
    return session


@pytest.fixture
def sample_fact():
    return ProcessedFact(
        fact_text='Test Fact',
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        mentioned_at=datetime.now(timezone.utc),
        payload={'source': 'test'},
        chunk_id=str(uuid4()),  # Valid UUID
        tags=['tag1', 'tag2'],
    )


# --- Tests ---


@pytest.mark.asyncio
async def test_insert_facts_batch_empty(mock_session):
    result = await storage.insert_facts_batch(mock_session, [])
    assert result == []
    mock_session.exec.assert_not_called()


@pytest.mark.asyncio
async def test_insert_facts_batch_success(mock_session, sample_fact):
    # Setup mock return
    mock_result = MagicMock()
    # Simulate returning 1 ID (as a row tuple)
    mock_result.all.return_value = [(uuid4(),)]
    mock_session.exec.return_value = mock_result

    # Act
    ids = await storage.insert_facts_batch(mock_session, [sample_fact], note_id=str(uuid4()))

    # Assert
    assert len(ids) == 1
    mock_session.exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_insert_facts_batch_verify_values(mock_session, sample_fact):
    document_id = str(uuid4())

    with patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.returning.return_value = mock_stmt

        await storage.insert_facts_batch(mock_session, [sample_fact], note_id=document_id)

        # Verify values passed to insert
        mock_insert.return_value.values.assert_called_once()
        inserted_rows = mock_insert.return_value.values.call_args[0][0]
        assert len(inserted_rows) == 1
        row = inserted_rows[0]

        assert row['text'] == sample_fact.fact_text
        assert row['embedding'] == sample_fact.embedding
        assert row['note_id'] == UUID(document_id)
        assert row['unit_metadata']['tags'] == ['tag1', 'tag2']
        # The stored metadata might still have the string or UUID depending on impl details,
        # but the COLUMN 'chunk_id' is what matters most now.
        assert row['chunk_id'] == UUID(sample_fact.chunk_id)
        assert row['unit_metadata']['source'] == 'test'


@pytest.mark.asyncio
async def test_insert_facts_batch_metadata_merge(mock_session):
    chunk_uuid = str(uuid4())
    fact = ProcessedFact(
        fact_text='Simple',
        fact_type=FactTypes.WORLD,
        embedding=[0.0],
        mentioned_at=datetime.now(timezone.utc),
        payload={'existing': 'val'},
        tags=['newtag'],
        chunk_id=chunk_uuid,
    )

    with patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.returning.return_value = mock_stmt

        await storage.insert_facts_batch(mock_session, [fact])

        inserted_rows = mock_insert.return_value.values.call_args[0][0]
        row = inserted_rows[0]
        meta = row['unit_metadata']

        assert meta['existing'] == 'val'
        assert meta['tags'] == ['newtag']
        assert row['chunk_id'] == UUID(chunk_uuid)


@pytest.mark.asyncio
async def test_handle_document_tracking_first_batch(mock_session):
    doc_id = str(uuid4())
    content = 'test content'

    with (
        patch('memex_core.memory.extraction.storage.delete') as mock_delete,
        patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert,
    ):
        # Setup mocks
        mock_delete_stmt = MagicMock()
        mock_delete.return_value.where.return_value = mock_delete_stmt

        mock_insert_stmt = MagicMock()
        mock_insert.return_value.values.return_value = mock_insert_stmt
        mock_insert_stmt.on_conflict_do_update.return_value = MagicMock()  # The upsert stmt

        await storage.handle_document_tracking(
            mock_session, doc_id, content, is_first_batch=True, document_tags=['tag1']
        )

        # Assert Delete called
        mock_delete.assert_called_once()
        # We can verify .where was called with correct column comparison if needed,
        # but just verifying delete was invoked is good for unit test here.

        # Assert Insert/Upsert called
        mock_insert.assert_called_once()

        # Verify 2 exec calls (delete + upsert)
        assert mock_session.exec.await_count == 2


@pytest.mark.asyncio
async def test_handle_document_tracking_subsequent_batch(mock_session):
    doc_id = str(uuid4())
    content = 'more content'

    with (
        patch('memex_core.memory.extraction.storage.delete') as mock_delete,
        patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert,
    ):
        mock_insert_stmt = MagicMock()
        mock_insert.return_value.values.return_value = mock_insert_stmt
        mock_insert_stmt.on_conflict_do_update.return_value = MagicMock()

        await storage.handle_document_tracking(mock_session, doc_id, content, is_first_batch=False)

        # Assert Delete NOT called
        mock_delete.assert_not_called()

        # Assert Upsert called
        mock_insert.assert_called_once()

        # Verify 1 exec call
        assert mock_session.exec.await_count == 1


@pytest.mark.asyncio
async def test_store_chunks_batch(mock_session):
    doc_id = str(uuid4())
    chunks = [
        ChunkMetadata(
            chunk_text='Chunk 1', chunk_index=0, fact_count=0, content_index=0, content_hash='hash1'
        ),
        ChunkMetadata(
            chunk_text='Chunk 2', chunk_index=1, fact_count=0, content_index=0, content_hash='hash2'
        ),
    ]

    # Fake generated IDs
    id1 = uuid4()
    id2 = uuid4()

    with patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert:
        # Setup mock return
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.returning.return_value = mock_stmt

        # Simulate execution result: list of (id, chunk_index) tuples
        mock_result = MagicMock()
        mock_result.all.return_value = [(id1, 0), (id2, 1)]
        mock_session.exec.return_value = mock_result

        # Act
        result_map = await storage.store_chunks_batch(mock_session, doc_id, chunks)

        # Assert
        assert len(result_map) == 2
        assert result_map[0] == str(id1)
        assert result_map[1] == str(id2)

        # Verify insert values
        mock_insert.return_value.values.assert_called_once()
        inserted_rows = mock_insert.return_value.values.call_args[0][0]
        assert len(inserted_rows) == 2

        assert inserted_rows[0]['text'] == 'Chunk 1'
        assert inserted_rows[0]['chunk_index'] == 0
        assert inserted_rows[0]['note_id'] == UUID(doc_id)

        assert inserted_rows[1]['text'] == 'Chunk 2'
        assert inserted_rows[1]['chunk_index'] == 1


@pytest.mark.asyncio
async def test_store_chunks_batch_deduplicates_content_hash(mock_session):
    """Duplicate content_hash in a single batch should be deduped (first occurrence wins)."""
    doc_id = str(uuid4())
    chunks = [
        ChunkMetadata(
            chunk_text='Chunk A', chunk_index=0, fact_count=0, content_index=0, content_hash='aaa'
        ),
        ChunkMetadata(
            chunk_text='Chunk B', chunk_index=1, fact_count=0, content_index=0, content_hash='bbb'
        ),
        ChunkMetadata(
            chunk_text='Chunk A dup',
            chunk_index=2,
            fact_count=0,
            content_index=0,
            content_hash='aaa',
        ),
    ]

    id1 = uuid4()
    id2 = uuid4()

    with patch('memex_core.memory.extraction.storage.pg_insert') as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.returning.return_value = mock_stmt

        mock_result = MagicMock()
        mock_result.all.return_value = [(id1, 0), (id2, 1)]
        mock_session.exec.return_value = mock_result

        result_map = await storage.store_chunks_batch(mock_session, doc_id, chunks)

        assert len(result_map) == 2

        # Verify only 2 rows passed to INSERT (deduped by content_hash)
        inserted_rows = mock_insert.return_value.values.call_args[0][0]
        assert len(inserted_rows) == 2
        assert inserted_rows[0]['chunk_index'] == 0  # first occurrence of 'aaa'
        assert inserted_rows[0]['content_hash'] == 'aaa'
        assert inserted_rows[1]['chunk_index'] == 1
        assert inserted_rows[1]['content_hash'] == 'bbb'
