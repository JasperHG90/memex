import pytest
from unittest.mock import AsyncMock
from uuid import uuid4


@pytest.mark.asyncio
async def test_mcp_get_resource_image(mock_api, mcp_client):
    """Test retrieving an image resource."""
    # Mock returning PNG bytes
    mock_api.get_resource.return_value = b'\x89PNG\r\n\x1a\n'

    # We need to inspect the raw result or trust the client wrapper
    result = await mcp_client.call_tool('memex_get_resource', {'path': 'images/test.png'})

    # FastMCP client should return a list of content
    assert len(result.content) == 1
    content = result.content[0]

    # Check if it is an image content
    assert content.type == 'image'
    # The data is base64 encoded in the protocol, but the client might decode it or present it as is
    # content.data should be the base64 string
    assert content.mimeType == 'image/png'


@pytest.mark.asyncio
async def test_mcp_get_resource_text(mock_api, mcp_client):
    """Test retrieving a text file (should be returned as File/EmbeddedResource)."""
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
    mock_api.get_resource.return_value = b'\x89PNG\r\n\x1a\n'

    result = await mcp_client.call_tool(
        'memex_get_resource', {'path': 'images/test.png', 'vault_id': str(vault_id)}
    )

    assert len(result.content) == 1
    content = result.content[0]
    assert content.type == 'image'
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))
