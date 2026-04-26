import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from helpers import parse_tool_result


def _local_paths(result) -> list[Path]:
    """Strip file:// prefix and return Path objects from a memex_get_resources result."""
    items = parse_tool_result(result)
    assert isinstance(items, list)
    return [Path(s.removeprefix('file://')) for s in items if s.startswith('file://')]


@pytest.mark.asyncio
async def test_mcp_get_resource_image(mock_api, asset_cache, mcp_client):
    """Disk-handoff: bytes are written to the session cache and a file:// URI is returned."""
    mock_api.get_resource = AsyncMock(return_value=b'fake-png-bytes')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/test.png'], 'vault_id': 'test-vault'}
    )

    paths = _local_paths(result)
    assert len(paths) == 1
    assert paths[0].exists()
    assert paths[0].read_bytes() == b'fake-png-bytes'
    mock_api.get_resource.assert_awaited_once_with('images/test.png')


@pytest.mark.asyncio
async def test_mcp_get_resource_text(mock_api, asset_cache, mcp_client):
    """Non-image assets also flow through the cache and come back as file:// URIs."""
    mock_api.get_resource = AsyncMock(return_value=b'Hello World')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['notes/test.txt'], 'vault_id': 'test-vault'}
    )

    paths = _local_paths(result)
    assert len(paths) == 1
    assert paths[0].read_bytes() == b'Hello World'


@pytest.mark.asyncio
async def test_mcp_get_resource_with_vault_id(mock_api, asset_cache, mcp_client):
    """Test memex_get_resources accepts vault_id parameter."""
    vault_id = uuid4()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=vault_id)
    mock_api.get_resource = AsyncMock(return_value=b'bytes')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/test.png'], 'vault_id': str(vault_id)}
    )

    paths = _local_paths(result)
    assert len(paths) == 1
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))


@pytest.mark.asyncio
async def test_mcp_get_resource_multiple_paths(mock_api, asset_cache, mcp_client):
    """Batch retrieval of multiple resources."""
    mock_api.get_resource = AsyncMock(side_effect=[b'one', b'two'])

    result = await mcp_client.call_tool(
        'memex_get_resources',
        {'paths': ['images/img1.png', 'images/img2.png'], 'vault_id': 'test-vault'},
    )

    paths = _local_paths(result)
    assert len(paths) == 2
    assert {p.read_bytes() for p in paths} == {b'one', b'two'}


@pytest.mark.asyncio
async def test_mcp_get_resource_partial_failure(mock_api, asset_cache, mcp_client):
    """One failing resource should not prevent others from being returned."""

    async def fake_fetch(path: str) -> bytes:
        if path == 'images/ok.png':
            return b'ok'
        raise RuntimeError('not found')

    mock_api.get_resource = AsyncMock(side_effect=fake_fetch)

    result = await mcp_client.call_tool(
        'memex_get_resources',
        {'paths': ['images/ok.png', 'images/bad.txt'], 'vault_id': 'test-vault'},
    )

    items = parse_tool_result(result)
    assert isinstance(items, list)
    assert len(items) == 2
    assert any(s.startswith('file://') for s in items)
    assert any('Error fetching' in s for s in items)


# ── New disk-handoff tests (AC-003, AC-004, AC-005) ──


@pytest.mark.asyncio
async def test_get_resources_returns_file_uri_paths(mock_api, asset_cache, mcp_client):
    """AC-003: returned values are file:// strings (not Image/Audio/File objects)."""
    mock_api.get_resource = AsyncMock(return_value=b'data')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/x.png'], 'vault_id': 'test-vault'}
    )

    items = parse_tool_result(result)
    assert items == [items[0]]  # single-item list
    assert isinstance(items[0], str)
    assert items[0].startswith('file://')


@pytest.mark.asyncio
async def test_get_resources_uses_session_tempdir(mock_api, asset_cache, mcp_client):
    """AC-003: returned path lives under the session asset cache tempdir."""
    mock_api.get_resource = AsyncMock(return_value=b'data')

    result = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/x.png'], 'vault_id': 'test-vault'}
    )

    paths = _local_paths(result)
    assert len(paths) == 1
    local = paths[0].resolve()
    cache_root = asset_cache.tempdir.resolve()
    assert local.is_relative_to(cache_root), f'{local} is not under {cache_root}'


@pytest.mark.asyncio
async def test_get_resources_caches_repeat_calls(mock_api, asset_cache, mcp_client):
    """AC-004: repeat calls for the same path hit the cache and don't re-fetch."""
    mock_api.get_resource = AsyncMock(return_value=b'data')

    first = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/x.png'], 'vault_id': 'test-vault'}
    )
    second = await mcp_client.call_tool(
        'memex_get_resources', {'paths': ['images/x.png'], 'vault_id': 'test-vault'}
    )

    assert mock_api.get_resource.await_count == 1
    assert parse_tool_result(first) == parse_tool_result(second)


@pytest.mark.asyncio
async def test_lifespan_cleanup_removes_tempdir():
    """AC-005: exiting the lifespan unlinks the session tempdir.

    Drives the lifespan context manager directly instead of relying on real
    process atexit firing.
    """
    from memex_mcp.lifespan import lifespan

    fake_vault = type('V', (), {'id': 'v', 'name': 'v'})()

    with (
        patch(
            'memex_common.client.RemoteMemexAPI.get_active_vault',
            new=AsyncMock(return_value=fake_vault),
        ),
        patch('httpx.AsyncClient.aclose', new=AsyncMock(return_value=None)),
    ):
        async with lifespan(server=None) as ctx:
            tempdir = ctx._asset_cache.tempdir
            assert tempdir.exists()

    assert not tempdir.exists()
