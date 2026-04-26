"""Tests for the memex_resize_image MCP tool."""

import json
import pytest
from pathlib import Path

from PIL import Image
from fastmcp.exceptions import ToolError


def _write_png(path: Path, size: tuple[int, int] = (2048, 1536)) -> None:
    """Write a deterministic PNG of ``size`` to ``path``."""
    Image.new('RGB', size, color=(255, 0, 0)).save(path, format='PNG')


@pytest.mark.asyncio
async def test_resize_writes_smaller_file(asset_cache, mcp_client):
    """Happy path: resizing a cached PNG produces a smaller sibling file."""
    src = asset_cache.tempdir / 'big.png'
    _write_png(src, size=(2048, 1536))
    src_size = src.stat().st_size

    result = await mcp_client.call_tool(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 512, 'max_height': 512},
    )

    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    dest_path = Path(payload['local_path'])
    assert dest_path.exists()
    assert dest_path.stat().st_size < src_size
    assert dest_path.is_relative_to(asset_cache.tempdir)
    assert payload['size_bytes'] == dest_path.stat().st_size


@pytest.mark.asyncio
async def test_resize_rejects_path_outside_tempdir(asset_cache, mcp_client):
    """AC-009: paths outside the session cache are rejected with ToolError."""
    with pytest.raises(ToolError, match='session asset cache'):
        await mcp_client.call_tool(
            'memex_resize_image',
            {'local_path': '/etc/passwd'},
        )


@pytest.mark.asyncio
async def test_resize_rejects_path_via_relative_traversal(asset_cache, tmp_path, mcp_client):
    """AC-009: a `..` traversal that escapes the cache is rejected after resolve()."""
    escape_target = tmp_path / 'escape.png'
    _write_png(escape_target, size=(64, 64))
    traversal = str(asset_cache.tempdir / '..' / 'escape.png')

    with pytest.raises(ToolError, match='session asset cache'):
        await mcp_client.call_tool(
            'memex_resize_image',
            {'local_path': traversal},
        )


@pytest.mark.asyncio
async def test_resize_rejects_unsupported_format(asset_cache, mcp_client):
    """Unsupported source format raises ToolError carrying the helper's message."""
    svg = asset_cache.tempdir / 'diagram.svg'
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    with pytest.raises(ToolError, match='Unsupported'):
        await mcp_client.call_tool(
            'memex_resize_image',
            {'local_path': str(svg)},
        )
