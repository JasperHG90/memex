"""Security/hardening tests for ``memex_get_resources`` and ``memex_resize_image``.

Each test in this module proves a specific Phase 3 adversarial finding:

- Finding 1  : per-asset 50 MiB size cap on get_resources.
- Finding 2  : ``resolve(strict=True)`` + clean ToolError on missing path.
- Finding 6  : positive-dimension validation.
- Finding 7  : decompression-bomb protection.
- Finding 10 : post-resize TOCTOU re-check.
- Finding 11 : ``format`` parameter renamed to ``output_format``.
- Finding 4  : resized destination registered into the LRU cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from fastmcp.exceptions import ToolError

from memex_common.asset_cache import MAX_RESOURCE_BYTES
from memex_mcp.server import _MAX_GET_RESOURCES_PATHS
from helpers import parse_tool_result


def _resize_payload(result) -> dict:
    return json.loads(result.content[0].text)


def _write_png(path: Path, size: tuple[int, int] = (64, 64)) -> Path:
    Image.new('RGB', size, color=(0, 128, 255)).save(path, format='PNG')
    return path


# Finding 1 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_resources_oversize_rejected_after_download(mock_api, asset_cache, mcp_client):
    """A response exceeding ``MAX_RESOURCE_BYTES`` is reported as an error
    string (the wrapper turns ToolError into an entry in the result list)."""
    big_blob = b'\x00' * (MAX_RESOURCE_BYTES + 1)
    mock_api.get_resource = AsyncMock(return_value=big_blob)

    result = await mcp_client.call_tool(
        'memex_get_resources',
        {'paths': ['images/huge.bin'], 'vault_id': 'test-vault'},
    )
    items = parse_tool_result(result)
    assert isinstance(items, list)
    assert len(items) == 1
    msg = items[0]
    assert msg.startswith('Error fetching ')
    assert 'Resource exceeds max size' in msg
    assert str(MAX_RESOURCE_BYTES) in msg


@pytest.mark.asyncio
async def test_oversize_path_invalidated_so_retry_refetches(mock_api, asset_cache, mcp_client):
    """A rejected oversize asset must be evicted from the cache; a second
    call for the same path must re-invoke the underlying fetch rather than
    serve a stale tracked-but-missing entry."""
    big_blob = b'\x00' * (MAX_RESOURCE_BYTES + 1)
    mock_api.get_resource = AsyncMock(return_value=big_blob)

    await mcp_client.call_tool(
        'memex_get_resources',
        {'paths': ['images/huge.bin'], 'vault_id': 'test-vault'},
    )
    assert 'images/huge.bin' not in asset_cache

    await mcp_client.call_tool(
        'memex_get_resources',
        {'paths': ['images/huge.bin'], 'vault_id': 'test-vault'},
    )
    assert mock_api.get_resource.call_count == 2


# Finding 2 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_rejects_nonexistent_path(asset_cache, mcp_client):
    """``resolve(strict=True)`` should surface a clean ``ToolError`` instead
    of leaking ``FileNotFoundError`` from underneath."""
    missing = asset_cache.tempdir / 'does-not-exist.png'
    with pytest.raises(ToolError, match='does not exist'):
        await mcp_client.call_tool(
            'memex_resize_image',
            {'local_path': str(missing)},
        )


# Finding 6 ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mw, mh',
    [(0, 64), (64, 0), (-1, 64), (64, -1)],
)
async def test_resize_rejects_negative_dimensions(asset_cache, mcp_client, mw, mh):
    """Zero and negative dimensions must be rejected before Pillow is invoked."""
    src = _write_png(asset_cache.tempdir / 'tiny.png', size=(32, 32))
    with pytest.raises(ToolError, match='positive'):
        await mcp_client.call_tool(
            'memex_resize_image',
            {'local_path': str(src), 'max_width': mw, 'max_height': mh},
        )


# Finding 7 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_rejects_decompression_bomb(asset_cache, mcp_client):
    """An image whose pixel count exceeds the shared ``_MAX_DECODED_PIXELS``
    budget must be rejected by ``resize_image`` before any pixels are
    decoded — surfacing as a clean ``ToolError`` to the caller."""
    # 200x200 = 40k pixels, well above the patched cap below.
    src = _write_png(asset_cache.tempdir / 'bomb.png', size=(200, 200))

    with patch('memex_common.asset_resize._MAX_DECODED_PIXELS', 1000):
        with pytest.raises(ToolError, match='too large to safely decode'):
            await mcp_client.call_tool(
                'memex_resize_image',
                {'local_path': str(src), 'max_width': 16, 'max_height': 16},
            )


@pytest.mark.asyncio
async def test_get_resources_rejects_too_many_paths(asset_cache, mcp_client):
    """Parity with Hermes: a paths list above ``_MAX_GET_RESOURCES_PATHS``
    is rejected up front so a misbehaving caller cannot fan out unbounded
    fetches."""
    paths = [f'images/asset-{i}.png' for i in range(_MAX_GET_RESOURCES_PATHS + 1)]
    with pytest.raises(ToolError, match='Too many paths'):
        await mcp_client.call_tool(
            'memex_get_resources',
            {'paths': paths, 'vault_id': 'test-vault'},
        )


# Finding 10 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_post_check_rejects_dest_outside_cache(asset_cache, mcp_client, tmp_path):
    """If ``resize_image`` (or a symlink swap during it) returns a path
    outside the session cache, the post-resize confinement check rejects it
    and unlinks the offending file."""
    src = _write_png(asset_cache.tempdir / 'src.png', size=(64, 64))

    rogue = tmp_path / 'rogue.png'
    _write_png(rogue, size=(16, 16))
    assert rogue.exists()
    assert not rogue.is_relative_to(asset_cache.tempdir)

    captured: list[Path] = []

    def _fake_resize(*_args, **_kwargs):
        captured.append(rogue)
        return rogue, rogue.stat().st_size

    with patch('memex_common.asset_resize.resize_image', side_effect=_fake_resize):
        with pytest.raises(ToolError, match='escaped session cache'):
            await mcp_client.call_tool(
                'memex_resize_image',
                {'local_path': str(src), 'max_width': 32, 'max_height': 32},
            )

    # The rogue path returned by the patched resize_image is the one that
    # validate_and_resize must unlink — verify that explicitly.
    assert captured == [rogue]
    assert not rogue.exists()
    assert rogue not in asset_cache


# Finding 4 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_registers_dest_in_cache(asset_cache, mcp_client):
    """A successful resize must register the destination in the LRU so it
    participates in eviction and session cleanup."""
    src = _write_png(asset_cache.tempdir / 'ok.png', size=(256, 256))
    result = await mcp_client.call_tool(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 64, 'max_height': 64},
    )
    payload = _resize_payload(result)
    assert payload['local_path'] in asset_cache
    assert payload['size_bytes'] > 0


# Finding 11 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_accepts_output_format_kwarg(asset_cache, mcp_client):
    """The renamed parameter is the public name; calling with
    ``output_format`` must succeed end-to-end."""
    src = _write_png(asset_cache.tempdir / 'src.png', size=(128, 128))
    result = await mcp_client.call_tool(
        'memex_resize_image',
        {
            'local_path': str(src),
            'max_width': 32,
            'max_height': 32,
            'output_format': 'JPEG',
        },
    )
    payload = _resize_payload(result)
    dest = Path(payload['local_path'])
    assert dest.exists()
    assert dest.suffix == '.jpg'
    assert payload['size_bytes'] == dest.stat().st_size
