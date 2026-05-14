"""summarize_node tool description wiring test.

After 2026-05-14: canonical text in ``memex_common.tool_descriptions``;
MCP-side `_summarize_descriptions.py` is a thin re-export shim. Content
pinned in ``packages/common/tests/test_tool_descriptions.py``.
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESC
from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


def test_summarize_node_shim_re_exports_common() -> None:
    assert MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION == MEMEX_MEMORY_SUMMARIZE_NODE_DESC


@pytest.mark.asyncio
async def test_summarize_node_tool_registered_with_common_description() -> None:
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None, 'memex_memory_summarize_node tool not registered'
    assert tool.description == MEMEX_MEMORY_SUMMARIZE_NODE_DESC


@pytest.mark.asyncio
async def test_summarize_node_tool_tagged_write_storage() -> None:
    """Tool registered with the standard write/storage tags (unchanged)."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None
    assert tool.tags == {'write', 'storage'}
