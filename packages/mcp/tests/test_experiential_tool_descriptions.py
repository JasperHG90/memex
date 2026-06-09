"""Experiential tool description wiring test.

The 8 ``memex_experiential_*`` tools (V7) are Tier 1a MCP surfaces. Their
per-tool description text is the SSOT constant from
``memex_common.tool_descriptions`` (mirrors the deprioritize pattern in
``test_deprioritize_tool_descriptions.py``).

This file pins the wiring — the MCP tool description = the common SSOT
constant. Content is pinned in
``packages/common/tests/test_tool_descriptions.py``.
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import (
    MEMEX_EXPERIENTIAL_BRIEFING_CARDS_DESC,
    MEMEX_EXPERIENTIAL_CREATE_DESC,
    MEMEX_EXPERIENTIAL_DEPRECATE_DESC,
    MEMEX_EXPERIENTIAL_GET_BY_IDENTITY_DESC,
    MEMEX_EXPERIENTIAL_GET_DESC,
    MEMEX_EXPERIENTIAL_SEARCH_DESC,
    MEMEX_EXPERIENTIAL_UPDATE_DESC,
    MEMEX_EXPERIENTIAL_UPSERT_DESC,
)


_ALL_TOOL_WIRING = (
    ('memex_experiential_briefing_cards', MEMEX_EXPERIENTIAL_BRIEFING_CARDS_DESC),
    ('memex_experiential_create', MEMEX_EXPERIENTIAL_CREATE_DESC),
    ('memex_experiential_deprecate', MEMEX_EXPERIENTIAL_DEPRECATE_DESC),
    ('memex_experiential_get', MEMEX_EXPERIENTIAL_GET_DESC),
    ('memex_experiential_get_by_identity', MEMEX_EXPERIENTIAL_GET_BY_IDENTITY_DESC),
    ('memex_experiential_search', MEMEX_EXPERIENTIAL_SEARCH_DESC),
    ('memex_experiential_update', MEMEX_EXPERIENTIAL_UPDATE_DESC),
    ('memex_experiential_upsert', MEMEX_EXPERIENTIAL_UPSERT_DESC),
)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name,expected_desc', _ALL_TOOL_WIRING)
async def test_experiential_tool_registered_with_common_description(
    tool_name: str, expected_desc: str
) -> None:
    """Each ``memex_experiential_*`` tool is registered and uses the
    common-SSOT description byte-for-byte."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool(tool_name)
    assert tool is not None, f'{tool_name} not registered'
    assert tool.description == expected_desc, (
        f'{tool_name} description drifted from common SSOT — '
        'edit `memex_common.tool_descriptions` and re-run.'
    )


@pytest.mark.asyncio
async def test_eight_experiential_tools_registered() -> None:
    """Sanity check on the count — guards against silent drops of any
    single tool in a future refactor. Uses ``get_tool`` per name rather
    than ``list_tools`` because the latter requires a freshly-spun
    event loop in this pytest-asyncio=auto config; ``get_tool`` works
    in either loop context."""
    from memex_mcp.server import mcp

    expected = sorted(name for name, _ in _ALL_TOOL_WIRING)
    found: list[str] = []
    for name in expected:
        tool = await mcp.get_tool(name)
        assert tool is not None, f'{name} not registered'
        found.append(name)
    assert len(found) == 8, f'Expected 8 memex_experiential_* tools, got {len(found)}: {found}'
    assert found == expected, (
        f'Experiential tool surface drift.\n  Expected: {expected}\n  Got:      {found}'
    )
