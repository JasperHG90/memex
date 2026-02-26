import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_mcp_list_assets(mock_api, mcp_client):
    """Test memex_list_assets returns file list."""
    doc_id = uuid4()

    # Mock Document Response (dict - API returns dict, not DTO)
    mock_api.get_document.return_value = {
        'id': doc_id,
        'doc_metadata': {'name': 'Architecture Diagram'},
        'assets': ['assets/docs/diagram.png', 'assets/docs/spec.pdf'],
        'created_at': '2024-01-01T00:00:00Z',
        'vault_id': uuid4(),
    }

    result = await mcp_client.call_tool('memex_list_assets', {'note_id': str(doc_id)})

    text = result.content[0].text

    mock_api.get_document.assert_called_once_with(doc_id)

    assert 'diagram.png' in text
    assert 'spec.pdf' in text
    assert 'assets/docs/diagram.png' in text
