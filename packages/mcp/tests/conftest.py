from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp import Client
from memex_mcp.server import mcp
from memex_mcp.models import AppContext
from memex_common.asset_cache import SessionAssetCache
from memex_common.config import MemexConfig


from helpers import TEST_VAULT_UUID  # noqa: F401


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
    mock.get_related_notes = AsyncMock(return_value={})
    mock.get_memory_links = AsyncMock(return_value={})
    # Vault resolution (required by all vault-scoped tools)
    mock.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    with patch('memex_mcp.server.get_api', return_value=mock):
        yield mock


@pytest.fixture
def asset_cache(tmp_path: Path):
    """Real SessionAssetCache rooted in tmp_path, patched in for MCP tools."""
    cache = SessionAssetCache(tempdir=tmp_path / 'asset-cache')
    with patch('memex_mcp.server.get_asset_cache', return_value=cache):
        yield cache
    cache.cleanup()


@pytest.fixture(autouse=True)
def _autouse_asset_cache(request):
    """Auto-provide a stub SessionAssetCache for tests that don't request it.

    Tools that fetch resources patch ``get_asset_cache`` and expect a real
    cache. Tests that don't touch the cache get an isolated tempdir under
    the pytest tmp factory.
    """
    if 'asset_cache' in request.fixturenames:
        yield
        return
    tmp_path_factory = request.getfixturevalue('tmp_path_factory')
    cache = SessionAssetCache(tempdir=tmp_path_factory.mktemp('mcp-cache'))
    with patch('memex_mcp.server.get_asset_cache', return_value=cache):
        yield
    cache.cleanup()


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
    ctx._asset_cache = SessionAssetCache()
    try:
        yield ctx
    finally:
        if ctx._asset_cache is not None:
            ctx._asset_cache.cleanup()


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


@pytest.fixture(autouse=True)
def _enable_discovery_mode():
    """Enable progressive disclosure for all MCP tests.

    The server only adds the transform when MEMEX_MCP_PROGRESSIVE_DISCLOSURE is
    set at import time. Tests assume it's active, so we add/remove it here.
    """
    from memex_mcp.transforms import DiscoveryMode

    transform = DiscoveryMode()
    mcp.add_transform(transform)
    yield
    # Remove only the transform we added
    mcp._transforms = [t for t in mcp._transforms if t is not transform]


@pytest.fixture
async def mcp_client():
    """Fixture providing a connected FastMCP client."""
    async with Client(mcp) as client:
        yield client
