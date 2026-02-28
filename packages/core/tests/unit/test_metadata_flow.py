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
async def test_ingest_payload_source_uri(api, mock_metastore, mock_session):
    from uuid import UUID

    # Verify ingest puts source_uri into payload
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
    # We need to mock the AsyncTransaction context manager
    with patch('memex_core.services.ingestion.AsyncTransaction', return_value=mock_txn):
        note = NoteInput('name', 'desc', b'content', source_uri='http://source.com')
        await api.ingest(note)

    # Check retain call
    call_args = api.memory.retain.call_args
    assert call_args is not None, 'memory.retain was not called'

    # call_args is (args, kwargs)
    # retain signature: (session, contents, ...)
    # contents is the second arg or kwargs['contents']

    if 'contents' in call_args[1]:
        contents = call_args[1]['contents']
    else:
        contents = call_args[0][1]

    assert len(contents) == 1
    rc = contents[0]
    assert rc.payload['source_uri'] == 'http://source.com'
