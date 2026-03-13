import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from memex_core.api import NoteInput


@pytest.mark.asyncio
async def test_note_source_uri():
    note = NoteInput('name', 'desc', b'content', source_uri='http://example.com')
    assert note.source_uri == 'http://example.com'


@pytest.mark.asyncio
async def test_note_from_file_source_uri(tmp_path):
    f = tmp_path / 'test.md'
    f.write_text('# content')

    note = await NoteInput.from_file(f)
    assert note.source_uri == str(f.absolute())


@pytest.mark.asyncio
async def test_ingest_payload_fields(api, mock_metastore, mock_session):
    from uuid import UUID

    # Verify ingest populates the full RetainContent payload
    api.memory.retain = AsyncMock(return_value={'status': 'success'})

    # Mock resolve_vault_identifier on the ingestion service's vault service
    vault_uuid = UUID('123e4567-e89b-12d3-a456-426614174000')
    api._ingestion._vaults.resolve_vault_identifier = AsyncMock(return_value=vault_uuid)

    # Mock document existence check: return None (not found)
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    # Mock Transaction
    mock_txn = AsyncMock()
    mock_txn.db_session = mock_session
    mock_txn.__aenter__.return_value = mock_txn
    with patch('memex_core.services.ingestion.AsyncTransaction', return_value=mock_txn):
        note = NoteInput(
            'my-note',
            'a test description',
            b'content',
            source_uri='http://source.com',
            tags=['alpha', 'beta'],
        )
        await api.ingest(note)

    # Use keyword-based access so this test is resilient to retain() signature changes
    call_args = api.memory.retain.call_args
    assert call_args is not None, 'memory.retain was not called'
    contents = call_args.kwargs['contents']

    assert len(contents) == 1
    rc = contents[0]
    payload = rc.payload

    # Verify all expected payload fields
    assert payload['source'] == 'note'
    assert payload['note_name'] == 'my-note'
    assert payload['note_description'] == 'a test description'
    assert payload['source_uri'] == 'http://source.com'
    assert payload['tags'] == ['alpha', 'beta']
    assert payload['content_fingerprint'] is not None
    assert 'uuid' in payload
    assert 'filestore_path' in payload
    assert 'assets' in payload
