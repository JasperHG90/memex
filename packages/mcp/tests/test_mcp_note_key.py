import pytest
from uuid import uuid4
from conftest import parse_tool_result
from memex_common.schemas import IngestResponse, NoteCreateDTO


@pytest.mark.asyncio
async def test_mcp_add_note_with_key(mock_api, mcp_client):
    """Test memex_add_note with explicit note_key preserves the key."""
    doc_id = str(uuid4())
    key = 'my-stable-key'

    mock_api.ingest.return_value = IngestResponse(note_id=doc_id, status='success', unit_ids=[])

    result = await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'Keyed Note',
            'markdown_content': '# Content',
            'description': 'Description',
            'author': 'tester',
            'tags': ['tag1'],
            'note_key': key,
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    assert data['note_id'] == doc_id
    mock_api.ingest.assert_called_once()

    args, _ = mock_api.ingest.call_args
    note_dto = args[0]
    assert isinstance(note_dto, NoteCreateDTO)
    assert note_dto.note_key == key


@pytest.mark.asyncio
async def test_mcp_add_note_auto_derives_note_key(mock_api, mcp_client):
    """Test memex_add_note auto-derives note_key from title when not provided."""
    doc_id = str(uuid4())
    mock_api.ingest.return_value = IngestResponse(note_id=doc_id, status='success', unit_ids=[])

    await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'My Important Note',
            'markdown_content': '# Content',
            'description': 'Description',
            'author': 'tester',
            'tags': ['tag1'],
            'vault_id': 'test-vault',
        },
    )

    mock_api.ingest.assert_called_once()
    args, _ = mock_api.ingest.call_args
    note_dto = args[0]
    assert isinstance(note_dto, NoteCreateDTO)
    assert note_dto.note_key == 'mcp:add_note:My Important Note'
