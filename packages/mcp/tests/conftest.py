import json
from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID
from fastmcp import Client
from memex_mcp.server import mcp
from memex_mcp.models import AppContext
from memex_common.config import MemexConfig


def parse_tool_result(result) -> list[dict] | dict | None:
    """Parse a tool call result into structured data.

    Returns a list of dicts for list results, a dict for single model results,
    or None for empty/null results.
    """
    # FastMCP may return structured_content with empty content list
    if not result.content:
        if hasattr(result, 'structured_content') and result.structured_content:
            return result.structured_content.get('result', [])
        return None
    text = result.content[0].text
    if not text or text == 'null':
        return None
    return json.loads(text)


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


@pytest.fixture(autouse=True)
def _disable_config_loading(monkeypatch):
    """Prevent MemexConfig from loading local/global config files in tests."""
    monkeypatch.setenv('MEMEX_LOAD_LOCAL_CONFIG', 'false')
    monkeypatch.setenv('MEMEX_LOAD_GLOBAL_CONFIG', 'false')


@asynccontextmanager
async def _mock_lifespan(_server):
    """No-op lifespan that skips the HTTP health check."""
    ctx = AppContext(config=MemexConfig())
    ctx._api = AsyncMock()
    yield ctx


@pytest.fixture(autouse=True)
def _mock_mcp_lifespan():
    """Replace the real MCP lifespan for all tests.

    The real lifespan makes an HTTP call to verify the active vault,
    blocking for up to 120 s when no server is running.
    """
    original = mcp._lifespan
    mcp._lifespan = _mock_lifespan
    yield
    mcp._lifespan = original


@pytest.fixture
async def mcp_client():
    """Fixture providing a connected FastMCP client."""
    async with Client(mcp) as client:
        yield client
