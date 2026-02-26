import datetime as dt
import pytest
from uuid import uuid4
from memex_common.schemas import NoteDTO


@pytest.mark.asyncio
async def test_mcp_list_assets(mock_api, mcp_client):
    """Test memex_list_assets returns file list."""
    doc_id = uuid4()

    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Architecture Diagram'},
        assets=['assets/docs/diagram.png', 'assets/docs/spec.pdf'],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
    )

    result = await mcp_client.call_tool('memex_list_assets', {'note_id': str(doc_id)})

    text = result.content[0].text

    mock_api.get_note.assert_called_once_with(doc_id)

    assert 'diagram.png' in text
    assert 'spec.pdf' in text
    assert 'assets/docs/diagram.png' in text
