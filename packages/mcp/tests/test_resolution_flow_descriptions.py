"""Post-architecture-refactor: the resolution-flow composite is gone from MCP.

Before 2026-05-14: MCP tool descriptions for ``memex_record_outcome`` and
``memex_memory_deprioritize`` composed a ~12 KB block containing the
5-step flow, orthogonal axes table, historical routing rule, "imperfect
recall" framing, and a "do-NOT-add" scope-creep list. This file used to
pin that content.

After 2026-05-14 (three-tier agent-surface architecture):
- Tier 1a (MCP tool descriptions) carries per-tool contracts only.
- Tier 1b (``memex_common.agent_surface``) carries the universal flow,
  axes, historical routing, virtual-unit warning, etc.
- Agent system prompts compose Tier 1b on top of agent-specific Tier 2.

This test file enforces the architecture boundary: composition content
MUST NOT appear in MCP tool descriptions (drift fence). Positive presence
of the flow content in ``compose_universal()`` is pinned by
``packages/common/tests/test_agent_surface.py``.
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESC,
    MEMEX_RECORD_OUTCOME_DESC,
)


# Phrases that used to live in the composite MCP description but now live
# in ``memex_common.agent_surface`` (Tier 1b). If any of these reappear in
# a Tier 1a tool description, the architecture has drifted — fix the
# description, do not relax the assertion.
_BANNED_IN_MCP_DESCRIPTIONS: tuple[str, ...] = (
    'Option A',
    'Option B',
    'Option C',
    'orthogonal axes',
    'Imperfect recall',
    'historical-routing',
    'memex_get_unit_history',
    'apply_pre_filter=False',
)


@pytest.mark.parametrize(
    'description',
    [MEMEX_RECORD_OUTCOME_DESC, MEMEX_MEMORY_DEPRIORITIZE_DESC],
    ids=['record_outcome', 'deprioritize'],
)
@pytest.mark.parametrize('phrase', _BANNED_IN_MCP_DESCRIPTIONS)
def test_mcp_description_does_not_carry_universal_content(description: str, phrase: str) -> None:
    assert phrase not in description, (
        f'Phrase {phrase!r} appeared in an MCP tool description but belongs in '
        '``memex_common.agent_surface`` (Tier 1b). MCP tool descriptions carry '
        'per-tool contracts only.'
    )


@pytest.mark.asyncio
async def test_record_outcome_tool_registered_with_common_description() -> None:
    """The MCP server registers memex_record_outcome with the description
    sourced from ``memex_common.tool_descriptions`` — not a local composite."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_record_outcome')
    assert tool is not None, 'memex_record_outcome tool not registered'
    assert tool.description == MEMEX_RECORD_OUTCOME_DESC


@pytest.mark.asyncio
async def test_deprioritize_tool_registered_with_common_description() -> None:
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_deprioritize')
    assert tool is not None, 'memex_memory_deprioritize tool not registered'
    assert tool.description == MEMEX_MEMORY_DEPRIORITIZE_DESC
