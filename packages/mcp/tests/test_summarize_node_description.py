"""summarize_node tool description verbatim test.

The expected text is hardcoded here, NOT loaded from the source
(non-circular: when the source changes, this test fails — that is the
contract).
"""

from __future__ import annotations

import pytest

from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


SUMMARIZE_NODE_DESCRIPTION_VERBATIM = (
    'memory_summarize_node — Trigger reflection SYNCHRONOUSLY on a specific entity or\n'
    'note set. This is the synchronous counterpart to the background reflect loop.\n'
    'Use when you notice mid-conversation that retrieved facts about a topic\n'
    'are conflicting, incomplete, or scattered, and you want Memex to consolidate them\n'
    'into a coherent mental model before continuing.\n'
    '\n'
    '- entity_id: focus reflection on a single entity (preferred for per-topic work)\n'
    '- note_ids: alternatively, focus on a specific set of notes\n'
    '- scope: "incremental" (default — only new evidence) or "full" (re-evaluate all,\n'
    '  capped at 1000 units)\n'
    '\n'
    'Returns a ReflectionResult with the updated/new MentalModel(s). Use sparingly;\n'
    'reflection is LLM-intensive. Default to background reflection unless you have a\n'
    'specific in-session reason to trigger now.\n'
    '\n'
    'Rate-limit per (entity, vault). On rejection the response carries a structured\n'
    '`retry_after_seconds` envelope — honor it rather than retry-looping.'
)


def test_summarize_node_description_constant_matches_spec_verbatim():
    """MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION matches the verbatim constant."""
    assert MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION == SUMMARIZE_NODE_DESCRIPTION_VERBATIM


@pytest.mark.asyncio
async def test_summarize_node_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None, 'memex_memory_summarize_node tool not registered'
    assert tool.description == SUMMARIZE_NODE_DESCRIPTION_VERBATIM


@pytest.mark.asyncio
async def test_summarize_node_tool_tagged_write_storage():
    """Tool registered with the standard write/storage tags."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None
    assert tool.tags == {'write', 'storage'}
