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

    tools = await mcp._list_tools()
    tool_names = [t.name for t in tools]
    assert 'memex_get_unit_history' in tool_names, (
        'F49 (memex_get_unit_history MCP tool) is not registered on the base branch. '
        f'F43 sequencing-gate failed: ship F49 first. Registered tools: {sorted(tool_names)[:10]}...'
    )
