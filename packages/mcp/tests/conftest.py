import pytest
from unittest.mock import AsyncMock, patch
from fastmcp import Client
from memex_mcp.server import mcp


@pytest.fixture
def mock_api():
    """Shared RemoteMemexAPI mock for MCP tool tests."""
    mock = AsyncMock()
    # Pre-mock common methods to avoid AttributeError in some tests
    mock.get_lineage = AsyncMock()
    mock.get_note = AsyncMock()
    mock.get_note_page_index = AsyncMock()
    mock.get_node = AsyncMock()
    mock.search = AsyncMock()
    mock.search_notes = AsyncMock()
    mock.get_resource = AsyncMock()
    mock.reflect_batch = AsyncMock()
    mock.ingest = AsyncMock()
    # New tool methods
    mock.list_vaults = AsyncMock()
    mock.list_notes = AsyncMock()
    mock.search_entities = AsyncMock()
    mock.list_entities_ranked = AsyncMock()
    mock.get_entity = AsyncMock()
    mock.get_entity_mentions = AsyncMock()
    mock.get_entity_cooccurrences = AsyncMock()
    mock.get_entities = AsyncMock()
    mock.get_memory_unit = AsyncMock()
    mock.get_nodes = AsyncMock()
    mock.get_note_metadata = AsyncMock()
    mock.get_notes_metadata = AsyncMock()

    with patch('memex_mcp.server.get_api', return_value=mock):
        yield mock


@pytest.fixture
async def mcp_client():
    """Fixture providing a connected FastMCP client."""
    async with Client(mcp) as client:
        yield client
