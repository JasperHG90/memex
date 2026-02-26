import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID
from memex_core.api import NoteInput
from memex_common.schemas import NoteCreateDTO


@pytest.mark.asyncio
async def test_ingest_batch_internal_success(api, mock_metastore, mock_filestore, mock_session):
    """Test successful batch ingestion internal logic."""
    # Setup
    vault_id = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=vault_id)

    note_dto = NoteCreateDTO(
        name='Test NoteInput',
        description='Desc',
        content='Y29udGVudA==',  # "content"
        files={'test.txt': 'YXNzZXQ='},  # "asset"
    )

    # Mock Transaction
    mock_txn = AsyncMock()
    mock_txn.db_session = MagicMock()
    mock_txn.__aenter__.return_value = mock_txn
    with patch('memex_core.api.AsyncTransaction', return_value=mock_txn):
        # Mock MemoryEngine.retain
        api.memory.retain = AsyncMock(return_value={'unit_ids': [uuid4()]})

        # Mock Vault lookup in idempotency check
        mock_vault = MagicMock()
        mock_vault.name = 'test-vault'
        mock_session.get.return_value = mock_vault

        final_result = None
        async for res in api.ingest_batch_internal(
            notes=[note_dto], vault_id=vault_id, batch_size=1
        ):
            final_result = res

        assert final_result is not None
        assert final_result['processed_count'] == 1
        assert final_result['skipped_count'] == 0
        assert final_result['failed_count'] == 0
        assert len(final_result['note_ids']) == 1

        # Verify idempotency check called
        mock_session.exec.assert_called()

        # Verify assets saved
        mock_filestore.save.assert_called()

        # Verify memory.retain called
        api.memory.retain.assert_called()


@pytest.mark.asyncio
async def test_ingest_batch_internal_skips_duplicates(api, mock_metastore, mock_session):
    """Test batch ingestion skips duplicate notes."""
    vault_id = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=vault_id)

    note_dto = NoteCreateDTO(name='Dup', description='Dup', content='ZHVw', vault_id=vault_id)

    # Calculate what the UUID (note_key) and fingerprint would be
    # NoteCreateDTO content is already bytes
    temp_note = NoteInput(
        name=note_dto.name, description=note_dto.description, content=note_dto.content
    )
    expected_uuid = UUID(temp_note.uuid)
    expected_fingerprint = temp_note.content_fingerprint

    # Mock returns (id, content_hash) tuples for two-gate check
    mock_session.exec.return_value.all.return_value = [(expected_uuid, expected_fingerprint)]

    final_result = None
    async for res in api.ingest_batch_internal(notes=[note_dto], vault_id=vault_id):
        final_result = res

    assert final_result is not None
    assert final_result['processed_count'] == 0
    assert final_result['skipped_count'] == 1
    assert final_result['failed_count'] == 0
    assert len(final_result['note_ids']) == 0
