import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


@pytest.mark.asyncio
async def test_mcp_get_resource_image(mock_api, mcp_client):
    """Test retrieving an image resource returns file:// URI for local stores."""
    mock_api.get_resource_path = MagicMock(return_value='/data/images/test.png')

    result = await mcp_client.call_tool('memex_get_resources', {'paths': ['images/test.png']})

    assert len(result.content) == 1
    content = result.content[0]

    # Local images now return file:// URI as text
    assert content.type == 'text'
    assert 'file:///data/images/test.png' in content.text


@pytest.mark.asyncio
async def test_mcp_get_resource_text(mock_api, mcp_client):
    """Test retrieving a text file (should be returned as File/EmbeddedResource)."""
    mock_api.get_resource_path = MagicMock(return_value=None)
    mock_api.get_resource.return_value = b'Hello World'

    result = await mcp_client.call_tool('memex_get_resources', {'paths': ['notes/test.txt']})

    # Batch returns a list — first item should be an EmbeddedResource
    assert len(result.content) >= 1
    content = result.content[0]

    # FastMCP File -> EmbeddedResource
    assert content.type == 'resource'
    assert content.resource.mimeType == 'text/plain'


@pytest.mark.asyncio
async def test_mcp_get_resource_with_vault_id(mock_api, mcp_client):
    """Test memex_get_resources accepts vault_id parameter."""
    vault_id = uuid4()

    mock_api.resolve_vault_identifier = AsyncMock(return_value=vault_id)
    mock_api.get_resource_path = MagicMock(return_value='/data/images/test.png')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/test.png'], 'vault_id': str(vault_id)}
    )

    assert len(result.content) == 1
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))


@pytest.mark.asyncio
async def test_mcp_get_resource_multiple_paths(mock_api, mcp_client):
    """Batch retrieval of multiple resources."""
    mock_api.get_resource_path = MagicMock(side_effect=['/data/img1.png', '/data/img2.png'])

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/img1.png', 'images/img2.png']}
    )

    # Should get two file:// URIs
    texts = [c.text for c in result.content if hasattr(c, 'text')]
    combined = ' '.join(texts)
    assert 'file:///data/img1.png' in combined
    assert 'file:///data/img2.png' in combined


@pytest.mark.asyncio
async def test_mcp_get_resource_partial_failure(mock_api, mcp_client):
    """One failing resource should not prevent others from being returned."""
    mock_api.get_resource_path = MagicMock(side_effect=['/data/ok.png', None])
    mock_api.get_resource.side_effect = RuntimeError('not found')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/ok.png', 'images/bad.txt']}
    )

    texts = [c.text for c in result.content if hasattr(c, 'text')]
    combined = ' '.join(texts)
    assert 'file:///data/ok.png' in combined
    assert 'Error fetching' in combined
