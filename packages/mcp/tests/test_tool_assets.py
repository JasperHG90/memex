import datetime as dt
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock
from conftest import parse_tool_result
from memex_common.schemas import NoteDTO
import httpx


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

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
    )

    data = parse_tool_result(result)
    mock_api.get_note.assert_called_once_with(doc_id)

    assert len(data) == 2
    assert data[0]['filename'] == 'diagram.png'
    assert data[0]['path'] == 'assets/docs/diagram.png'
    assert data[1]['filename'] == 'spec.pdf'


@pytest.mark.asyncio
async def test_mcp_list_assets_not_found(mock_api, mcp_client):
    """Test memex_list_assets raises ToolError when note is not found (HTTP 404)."""
    from fastmcp.exceptions import ToolError

    doc_id = uuid4()

    response = httpx.Response(404, request=httpx.Request('GET', f'http://test/notes/{doc_id}'))
    mock_api.get_note.side_effect = httpx.HTTPStatusError(
        'Not Found', request=response.request, response=response
    )

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )


@pytest.mark.asyncio
async def test_mcp_list_assets_with_vault_id(mock_api, mcp_client):
    """Test memex_list_assets accepts vault_id parameter."""
    doc_id = uuid4()
    vault_id = uuid4()

    mock_api.resolve_vault_identifier = AsyncMock(return_value=vault_id)
    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Test Note'},
        assets=['assets/test.png'],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=vault_id,
    )

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': str(vault_id)}
    )

    data = parse_tool_result(result)
    assert len(data) == 1
    assert data[0]['filename'] == 'test.png'
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))


@pytest.mark.asyncio
async def test_mcp_list_assets_no_assets(mock_api, mcp_client):
    """Test memex_list_assets returns empty list when note has no assets."""
    doc_id = uuid4()

    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Empty Note'},
        assets=[],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
    )

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
    )

    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_mcp_list_assets_http_500_propagates(mock_api, mcp_client):
    """Test that non-404 HTTP errors are not swallowed."""
    from fastmcp.exceptions import ToolError

    doc_id = uuid4()

    response = httpx.Response(500, request=httpx.Request('GET', f'http://test/notes/{doc_id}'))
    mock_api.get_note.side_effect = httpx.HTTPStatusError(
        'Server Error', request=response.request, response=response
    )

    with pytest.raises(ToolError, match='List assets failed'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )


@pytest.mark.asyncio
async def test_mcp_list_assets_invalid_uuid(mock_api, mcp_client):
    """Test memex_list_assets rejects invalid UUIDs."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='Invalid Note UUID'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': 'not-a-uuid', 'vault_id': 'test-vault'}
        )
