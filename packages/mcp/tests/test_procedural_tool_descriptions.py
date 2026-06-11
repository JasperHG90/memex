"""Procedural tool description wiring test.

The agent-facing procedural surface is READ-ONLY plus case submission:
``memex_procedural_get`` / ``memex_procedural_get_by_identity`` /
``memex_procedural_search`` + ``memex_case_submit``. There is NO
agent-facing procedural WRITE tool (create/update/upsert/deprecate) —
procedures/strategies are DERIVED from cases (design §5/§8/§9); the
agent's only procedural write is ``memex_case_submit``. Direct authoring
stays on the operator surfaces (CLI / TUI / HTTP). There is also NO
briefing tool — pinned cards arrive inside the session briefing (JG
decision 2026-06-10).

This file pins the wiring — the MCP tool description = the common SSOT
constant. Content is pinned in
``packages/common/tests/test_tool_descriptions.py``.
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import (
    MEMEX_CASE_SUBMIT_DESC,
    MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC,
    MEMEX_PROCEDURAL_GET_DESC,
    MEMEX_PROCEDURAL_SEARCH_DESC,
)


_ALL_TOOL_WIRING = (
    ('memex_case_submit', MEMEX_CASE_SUBMIT_DESC),
    ('memex_procedural_get', MEMEX_PROCEDURAL_GET_DESC),
    ('memex_procedural_get_by_identity', MEMEX_PROCEDURAL_GET_BY_IDENTITY_DESC),
    ('memex_procedural_search', MEMEX_PROCEDURAL_SEARCH_DESC),
)

#: The procedural WRITE tools that MUST NOT be registered on the agent
#: surface. Probed explicitly so a future refactor can't quietly re-add
#: agent-facing authoring (procedures are derived from cases).
_FORBIDDEN_WRITE_TOOLS = (
    'memex_procedural_create',
    'memex_procedural_update',
    'memex_procedural_upsert',
    'memex_procedural_deprecate',
)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name,expected_desc', _ALL_TOOL_WIRING)
async def test_procedural_tool_registered_with_common_description(
    tool_name: str, expected_desc: str
) -> None:
    """Each ``memex_procedural_*`` tool is registered and uses the
    common-SSOT description byte-for-byte."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool(tool_name)
    assert tool is not None, f'{tool_name} not registered'
    assert tool.description == expected_desc, (
        f'{tool_name} description drifted from common SSOT — '
        'edit `memex_common.tool_descriptions` and re-run.'
    )


@pytest.mark.asyncio
async def test_procedural_surface_tool_set() -> None:
    """Sanity check on the surface — guards against silent drops of any
    single tool in a future refactor, AND against the briefing tool
    sneaking back (cards arrive in the session briefing; agents never
    call a tool for them)."""
    from memex_mcp.server import mcp

    expected = sorted(name for name, _ in _ALL_TOOL_WIRING)
    found: list[str] = []
    for name in expected:
        tool = await mcp.get_tool(name)
        assert tool is not None, f'{name} not registered'
        found.append(name)
    assert len(found) == 4, f'Expected 4 procedural tools, got {len(found)}: {found}'
    assert found == expected, (
        f'Procedural tool surface drift.\n  Expected: {expected}\n  Got:      {found}'
    )
    try:
        gone = await mcp.get_tool('memex_procedural_briefing_cards')
    except Exception:
        gone = None
    assert gone is None, (
        'memex_procedural_briefing_cards must NOT be registered — pinned '
        'cards arrive inside the session briefing (JG decision 2026-06-10).'
    )
    # The agent surface is read-only + case_submit: no procedural writes.
    for write_tool in _FORBIDDEN_WRITE_TOOLS:
        try:
            present = await mcp.get_tool(write_tool)
        except Exception:
            present = None
        assert present is None, (
            f'{write_tool} must NOT be registered — procedures are derived '
            'from cases; the agent writes via memex_case_submit only.'
        )
