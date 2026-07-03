"""Pre-merge sequencing-gate tests.

Verifies that:
  (a) `apply_pre_filter` is a valid parameter on `RetrievalRequest`.
  (b) `memex_get_unit_history` is a registered MCP tool.

These two trivial assertions catch the sequencing failure (dependencies not
yet on base) before the real-LLM golden tests fail for the wrong reason.
"""

from __future__ import annotations

import pytest


def test_apply_pre_filter_exists_on_retrieval_request():
    """apply_pre_filter must be on the base branch — pre-filter parameter on retrieval."""
    from memex_common.schemas import RetrievalRequest

    has_attr = hasattr(RetrievalRequest, 'apply_pre_filter')
    in_fields = 'apply_pre_filter' in getattr(RetrievalRequest, 'model_fields', {})
    assert has_attr or in_fields, (
        'apply_pre_filter on RetrievalRequest is not on the base branch. '
        'Sequencing-gate failed: ship apply_pre_filter first.'
    )


@pytest.mark.asyncio
async def test_get_unit_history_tool_registered():
    """memex_get_unit_history must be registered — graph-walk timeline tool."""
    from memex_mcp.server import mcp

    # Use the public ``get_tool`` API rather than ``list_tools``: the latter is
    # filtered by the DiscoveryMode transform under progressive disclosure
    # (auto-applied in this package's conftest), so it would only show the
    # discovery surface (memex_tags / memex_search / memex_get_schema).
    # ``get_tool`` returns the registered Tool object regardless of transforms,
    # which is the right semantics for "is this tool registered?".
    tool = await mcp.get_tool('memex_get_unit_history')
    assert tool is not None and tool.name == 'memex_get_unit_history', (
        'memex_get_unit_history MCP tool is not registered on the base branch. '
        'Sequencing-gate failed: ship memex_get_unit_history first.'
    )
