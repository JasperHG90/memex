"""deprioritize / restore tool description wiring test.

After 2026-05-14: canonical text moved to ``memex_common.tool_descriptions``.
The MCP-side `_deprioritize_descriptions.py` is now a thin re-export shim.
This test pins the wiring (MCP description = common constant), not the
content (content pinned in ``packages/common/tests/test_tool_descriptions.py``).
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESC,
    MEMEX_MEMORY_RESTORE_DESC,
)
from memex_mcp._deprioritize_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_MEMORY_RESTORE_DESCRIPTION,
)


def test_deprioritize_shim_re_exports_common() -> None:
    """The MCP shim must re-export the common SSOT constant byte-for-byte."""
    assert MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION == MEMEX_MEMORY_DEPRIORITIZE_DESC


def test_restore_shim_re_exports_common() -> None:
    assert MEMEX_MEMORY_RESTORE_DESCRIPTION == MEMEX_MEMORY_RESTORE_DESC


@pytest.mark.asyncio
async def test_deprioritize_tool_registered_with_common_description() -> None:
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_deprioritize')
    assert tool is not None, 'memex_memory_deprioritize tool not registered'
    assert tool.description == MEMEX_MEMORY_DEPRIORITIZE_DESC


@pytest.mark.asyncio
async def test_restore_tool_registered_with_common_description() -> None:
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_restore')
    assert tool is not None, 'memex_memory_restore tool not registered'
    assert tool.description == MEMEX_MEMORY_RESTORE_DESC
