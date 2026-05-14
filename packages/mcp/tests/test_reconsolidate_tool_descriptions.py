"""reconsolidate / consolidate tool description wiring test.

After 2026-05-14: canonical text in ``memex_common.tool_descriptions``;
MCP-side `_reconsolidate_descriptions.py` is a thin re-export shim.
Content pinned in ``packages/common/tests/test_tool_descriptions.py``.
"""

from __future__ import annotations

import pytest

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESC,
    MEMEX_MEMORY_RECONSOLIDATE_DESC,
)
from memex_mcp._reconsolidate_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
)


def test_reconsolidate_shim_re_exports_common() -> None:
    assert MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION == MEMEX_MEMORY_RECONSOLIDATE_DESC


def test_reconsolidate_description_documents_abandoned_outcome() -> None:
    """The ``abandoned: true`` envelope must be documented so agents
    re-read entity state instead of retry-looping after CAS contention."""
    assert 'abandoned' in MEMEX_MEMORY_RECONSOLIDATE_DESC


def test_consolidate_shim_re_exports_common() -> None:
    assert MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION == MEMEX_MEMORY_CONSOLIDATE_DESC


@pytest.mark.asyncio
async def test_reconsolidate_tool_registered_with_common_description():
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_reconsolidate')
    assert tool is not None, 'memex_memory_reconsolidate tool not registered'
    assert tool.description == MEMEX_MEMORY_RECONSOLIDATE_DESC


@pytest.mark.asyncio
async def test_consolidate_tool_registered_with_common_description():
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_consolidate')
    assert tool is not None, 'memex_memory_consolidate tool not registered'
    assert tool.description == MEMEX_MEMORY_CONSOLIDATE_DESC
