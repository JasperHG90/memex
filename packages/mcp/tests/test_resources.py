import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


@pytest.mark.asyncio
async def test_mcp_get_resource_image(mock_api, mcp_client):
    """Test retrieving an image resource returns file:// URI for local stores."""
    mock_api.get_resource_path = MagicMock(return_value='/data/images/test.png')

    result = await mcp_client.call_tool('memex_get_resource', {'path': 'images/test.png'})

    assert len(result.content) == 1
    content = result.content[0]

    # Local images now return file:// URI as text
    assert content.type == 'text'
    assert content.text == 'file:///data/images/test.png'


@pytest.mark.asyncio
async def test_mcp_get_resource_text(mock_api, mcp_client):
    """Test retrieving a text file (should be returned as File/EmbeddedResource)."""
    mock_api.get_resource_path = MagicMock(return_value=None)
    mock_api.get_resource.return_value = b'Hello World'

    result = await mcp_client.call_tool('memex_get_resource', {'path': 'notes/test.txt'})

    assert len(result.content) == 1
    content = result.content[0]

    # FastMCP File -> EmbeddedResource
    assert content.type == 'resource'
    assert content.resource.mimeType == 'text/plain'


@pytest.mark.asyncio
async def test_mcp_get_resource_with_vault_id(mock_api, mcp_client):
    """Test memex_get_resource accepts vault_id parameter."""
    vault_id = uuid4()

    mock_api.resolve_vault_identifier = AsyncMock(return_value=vault_id)
    mock_api.get_resource_path = MagicMock(return_value='/data/images/test.png')

    result = await mcp_client.call_tool(
        'memex_get_resource', {'path': 'images/test.png', 'vault_id': str(vault_id)}
    )

    assert len(result.content) == 1
    content = result.content[0]
    assert content.type == 'text'
    assert 'file://' in content.text
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))
