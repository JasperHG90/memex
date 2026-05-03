"""F43 — Pre-merge sequencing-gate tests.

Per BACKLOG line 116: F43's PR CI MUST verify that:
  (a) `apply_pre_filter` is a valid parameter on `RetrievalRequest` (F40).
  (b) `memex_get_unit_history` is a registered MCP tool (F49).

These two trivial assertions catch the sequencing failure (F40 / F49 not yet
on base) before the F43 real-LLM golden tests fail for the wrong reason.

This is round-6 LOW deferral test sourced verbatim from the spec.
"""

from __future__ import annotations

import pytest


def test_apply_pre_filter_exists_on_retrieval_request():
    """F40 must be on the base branch — pre-filter parameter on retrieval."""
    from memex_common.schemas import RetrievalRequest

    has_attr = hasattr(RetrievalRequest, 'apply_pre_filter')
    in_fields = 'apply_pre_filter' in getattr(RetrievalRequest, 'model_fields', {})
    assert has_attr or in_fields, (
        'F40 (apply_pre_filter on RetrievalRequest) is not on the base branch. '
        'F43 sequencing-gate failed: ship F40 first.'
    )


@pytest.mark.asyncio
async def test_get_unit_history_tool_registered():
    """F49 must be on the base branch — graph-walk timeline tool registered."""
    from memex_mcp.server import mcp

    # Use the public ``get_tool`` API rather than ``list_tools``: the latter is
    # filtered by the DiscoveryMode transform under progressive disclosure
    # (auto-applied in this package's conftest), so it would only show the
    # discovery surface (memex_tags / memex_search / memex_get_schema).
    # ``get_tool`` returns the registered Tool object regardless of transforms,
    # which is the right semantics for "is this tool registered?".
    tool = await mcp.get_tool('memex_get_unit_history')
    assert tool is not None and tool.name == 'memex_get_unit_history', (
        'F49 (memex_get_unit_history MCP tool) is not registered on the base branch. '
        'F43 sequencing-gate failed: ship F49 first.'
    )
