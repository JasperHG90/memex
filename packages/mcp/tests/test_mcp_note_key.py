import pytest
from uuid import uuid4
from memex_common.schemas import NoteDTO


@pytest.mark.asyncio
async def test_mcp_add_note_with_key(mock_api, mcp_client):
    """Test memex_add_note with document_key."""
    doc_id = str(uuid4())
    key = 'my-stable-key'

    mock_api.ingest.return_value = {'status': 'success', 'document_id': doc_id, 'unit_ids': []}

    result = await mcp_client.call_tool(
        'memex_add_note',
        {
            'title': 'Keyed Note',
            'markdown_content': '# Content',
            'description': 'Description',
            'author': 'tester',
            'tags': ['tag1'],
            'document_key': key,
        },
    )

    assert f'ID: {doc_id}' in str(result)
    mock_api.ingest.assert_called_once()

    args, _ = mock_api.ingest.call_args
    note_dto = args[0]
    assert isinstance(note_dto, NoteDTO)
    assert note_dto.document_key == key
