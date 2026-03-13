import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID
from fastmcp import Client
from memex_mcp.server import mcp

TEST_VAULT_UUID = UUID('00000000-0000-0000-0000-000000000001')


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
    # Vault resolution (required by all vault-scoped tools)
    mock.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    with patch('memex_mcp.server.get_api', return_value=mock):
        yield mock


@pytest.fixture
def mock_config():
    """Mock MemexConfig for tools that use get_config (e.g. vault defaults)."""
    config = MagicMock()
    config.write_vault = 'my-project'
    config.read_vaults = ['my-project', 'shared']
    config.server.default_active_vault = 'global'
    config.server.default_reader_vault = 'global'
    with patch('memex_mcp.server.get_config', return_value=config):
        yield config


@pytest.fixture
async def mcp_client():
    """Fixture providing a connected FastMCP client."""
    async with Client(mcp) as client:
        yield client
