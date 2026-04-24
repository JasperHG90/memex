import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
from memex_core.api import NoteInput
from memex_common.schemas import NoteCreateDTO


@pytest.mark.asyncio
async def test_ingest_batch_internal_success(api, mock_metastore, mock_filestore, mock_session):
    """Test successful batch ingestion internal logic."""
    # Setup
    vault_id = uuid4()
    api._ingestion._vaults.resolve_vault_identifier = AsyncMock(return_value=vault_id)

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
    with patch('memex_core.services.ingestion.AsyncTransaction', return_value=mock_txn):
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

        # Verify assets saved via transaction proxy
        mock_txn.save_file.assert_called()

        # Verify memory.retain called
        api.memory.retain.assert_called()


@pytest.mark.asyncio
async def test_ingest_batch_internal_skips_duplicates(api, mock_metastore, mock_session):
    """Test batch ingestion skips duplicate notes."""
    vault_id = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=vault_id)

    note_dto = NoteCreateDTO(name='Dup', description='Dup', content='ZHVw', vault_id=vault_id)

    # Calculate what the idempotency key and fingerprint would be
    # NoteCreateDTO content is already bytes
    temp_note = NoteInput(
        name=note_dto.name, description=note_dto.description, content=note_dto.content
    )
    expected_uuid = UUID(temp_note.idempotency_key)
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


@pytest.mark.asyncio
async def test_ingest_batch_internal_resolves_title(
    api, mock_metastore, mock_filestore, mock_session
):
    """Test that batch ingestion calls resolve_document_title and uses the resolved title."""
    vault_id = uuid4()
    api._ingestion._vaults.resolve_vault_identifier = AsyncMock(return_value=vault_id)

    note_dto = NoteCreateDTO(
        name='content.md',
        description='Desc',
        content='Y29udGVudA==',  # "content"
    )

    mock_txn = AsyncMock()
    mock_txn.db_session = MagicMock()
    mock_txn.__aenter__.return_value = mock_txn

    with (
        patch('memex_core.services.ingestion.AsyncTransaction', return_value=mock_txn),
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Resolved Title From Content',
        ) as mock_resolve,
    ):
        api.memory.retain = AsyncMock(return_value={'unit_ids': [uuid4()]})

        mock_vault = MagicMock()
        mock_vault.name = 'test-vault'
        mock_session.get.return_value = mock_vault

        async for _ in api.ingest_batch_internal(notes=[note_dto], vault_id=vault_id, batch_size=1):
            pass

        # Verify resolve_document_title was called with the raw name
        mock_resolve.assert_awaited_once()
        call_args = mock_resolve.call_args
        content_arg = call_args[0][0]
        assert isinstance(content_arg, str), f'Expected str, got {type(content_arg)}'
        assert call_args[0][1] == 'content.md'  # provided_name

        # Verify the resolved title was passed into RetainContent payload
        retain_call = api.memory.retain.call_args
        retain_contents = retain_call[1]['contents']
        assert retain_contents[0].payload['note_name'] == 'Resolved Title From Content'


@pytest.mark.asyncio
async def test_process_chunk_uses_fresh_staging_txn_id(
    api, mock_metastore, mock_filestore, mock_session
):
    """Regression: the staging txn_id must not be derived from the note UUID.

    Two concurrent ingests of the same note content share an idempotency key (note UUID).
    If the staging txn_id is derived from it, both transactions collide in the filestore's
    process-wide _active_stages dict ("Staging transaction already active"). The txn_id must
    be fresh per transaction, and must differ across two calls that ingest identical content.
    """
    vault_id = uuid4()
    api._ingestion._vaults.resolve_vault_identifier = AsyncMock(return_value=vault_id)

    note_dto = NoteCreateDTO(
        name='Same content',
        description='Same desc',
        content='c2FtZS1ieXRlcw==',  # "same-bytes"
    )
    expected_note_uuid = UUID(NoteInput.calculate_idempotency_key_from_dto(note_dto))

    captured_txn_ids: list[str] = []

    def _capture_txn(metastore, filestore, txn_id):
        captured_txn_ids.append(txn_id)
        m = AsyncMock()
        m.db_session = MagicMock()
        m.__aenter__.return_value = m
        return m

    mock_vault = MagicMock()
    mock_vault.name = 'test-vault'
    mock_session.get.return_value = mock_vault
    api.memory.retain = AsyncMock(return_value={'unit_ids': [uuid4()]})

    with patch('memex_core.services.ingestion.AsyncTransaction', side_effect=_capture_txn):
        async for _ in api.ingest_batch_internal(notes=[note_dto], vault_id=vault_id, batch_size=1):
            pass
        async for _ in api.ingest_batch_internal(notes=[note_dto], vault_id=vault_id, batch_size=1):
            pass

    assert len(captured_txn_ids) == 2, f'Expected 2 transactions, got {captured_txn_ids}'
    assert captured_txn_ids[0] != captured_txn_ids[1], (
        'Staging txn_id is identical across two ingests of the same content — '
        'concurrent ingestions will collide in the filestore._active_stages dict.'
    )
    assert str(expected_note_uuid) not in captured_txn_ids, (
        f'Staging txn_id must not be the note UUID {expected_note_uuid}; got {captured_txn_ids}.'
    )
