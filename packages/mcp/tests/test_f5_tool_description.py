"""F5 — MCP tool description verbatim test (TC5).

The expected text is hardcoded here, NOT loaded from the spec markdown
(non-circular: when the spec changes, this test fails — that is the
contract). Sourced from cognitive-memory-research-report.md §4 F5 step 6
(lines 653-666 as of 2026-04-30 against memory_augmentation @ f79c313).
"""

from __future__ import annotations

import pytest

from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


F5_DESCRIPTION_VERBATIM = (
    'memory_summarize_node — Trigger reflection synchronously on a specific entity or\n'
    'note set. Use when you notice mid-conversation that retrieved facts about a topic\n'
    'are conflicting, incomplete, or scattered, and you want Memex to consolidate them\n'
    'into a coherent mental model before continuing.\n'
    '\n'
    '- entity_id: focus reflection on a single entity (preferred for per-topic work)\n'
    '- note_ids: alternatively, focus on a specific set of notes\n'
    '- scope: "incremental" (default — only new evidence) or "full" (re-evaluate all)\n'
    '\n'
    'Returns a ReflectionResult with the updated/new MentalModel(s). Use sparingly;\n'
    'reflection is LLM-intensive. Default to background reflection unless you have a\n'
    'specific in-session reason to trigger now.'
)


def test_summarize_node_description_constant_matches_spec_verbatim():
    """TC5: MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION matches §4 F5 step 6 char-for-char."""
    assert MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION == F5_DESCRIPTION_VERBATIM


@pytest.mark.asyncio
async def test_summarize_node_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None, 'memex_memory_summarize_node tool not registered'
    assert tool.description == F5_DESCRIPTION_VERBATIM


@pytest.mark.asyncio
async def test_summarize_node_tool_tagged_write_storage():
    """Tool registered with the standard write/storage tags (per F4 taxonomy)."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_summarize_node')
    assert tool is not None
    assert tool.tags == {'write', 'storage'}
