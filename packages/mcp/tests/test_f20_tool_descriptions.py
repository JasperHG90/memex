"""F20 — MCP tool description verbatim drift guard.

Mirrors the F4/F5 pattern (test_f4_tool_descriptions.py,
test_f5_tool_description.py): assert each F20 description constant in
``_revisit_descriptions.py`` matches a hardcoded verbatim block here. When
either side changes, the test fails — that's the contract.

Why hardcode the expected text in the test rather than load from
``_revisit_descriptions.py``? Because the test would then be circular: any
edit to the source description would silently update the "expected"
side too. Two independent copies pin the wording.

The descriptions are agent-facing prompt text — drift here changes how
the LLM picks which verb to call and what the user-visible behaviour
becomes. AC-X-10 boot canary catches registration regressions; this
test catches *content* regressions.
"""

from __future__ import annotations

import pytest

from memex_mcp._revisit_descriptions import (
    MEMEX_GET_DUE_FOR_REVIEW_DESCRIPTION,
    MEMEX_MEMORY_REVIEW_DESCRIPTION,
)


F20_GET_DUE_VERBATIM = (
    'memex_get_due_for_review — List memories that are due for revisit in a vault.\n'
    '\n'
    'Use when the user asks something like "what memories are due for review?",\n'
    '"what should I revisit?", or "show me my review queue". Returns the units\n'
    'whose `revisit_due_at <= now()` AND that pass the 5-gate eligibility\n'
    'predicate (intent_class IN (permanent, durable), status=active, not\n'
    'deprioritized, confidence >= 0.5, mw_score >= 0.4).\n'
    '\n'
    '- vault_id: vault UUID or name (defaults to active vault if omitted)\n'
    '- limit: maximum number of due units to return (default 20)\n'
    '\n'
    'Returns a list of {unit_id, text_preview, revisit_due_at, intent_class}.\n'
    'This is a READ verb — it does NOT advance any schedule. To record a\n'
    'review outcome, use memex_memory_review.'
)


F20_REVIEW_VERBATIM = (
    'memex_memory_review — Record a review outcome on a memory unit.\n'
    '\n'
    'Use when the user says something like "I just reviewed memory X, it was\n'
    '\'good\'", "mark X as easy", or "I forgot X" (which maps to quality=again).\n'
    'Advances the FSRS-5 schedule, increments success/failure outcome counters,\n'
    'maintains the sticky-deprioritize streak, and writes an audit row — all\n'
    'in a single transaction.\n'
    '\n'
    '- unit_id: the memory unit being reviewed\n'
    '- quality: one of "again" (forgotten), "hard", "good", or "easy"\n'
    '         (or the FSRS-5 IntEnum value 1/2/3/4)\n'
    '- vault_id: REQUIRED — the vault the memory unit belongs to.\n'
    '         Cross-vault review is rejected (Wave 0 vault-scoping invariant).\n'
    '\n'
    'Quality mapping for outcome counters:\n'
    '  "again" / "hard" → recorded as a failure outcome\n'
    '  "good" / "easy"  → recorded as a success outcome\n'
    '\n'
    'Sticky-deprioritize: 5 consecutive "again" ratings (without an intervening\n'
    'hard/good/easy) automatically flip the unit to is_deprioritized=true.\n'
    'Once deprioritized, positive outcomes do NOT auto-restore — the user\n'
    'must explicitly run `memex memory restore` to bring it back.\n'
    '\n'
    'Returns: {unit_id, quality, next_review_at, interval_days, review_count,\n'
    'auto_deprioritized}. Use auto_deprioritized to inform the user when the\n'
    'sticky gate has been triggered.'
)


def test_get_due_for_review_description_constant_matches_spec_verbatim():
    """MEMEX_GET_DUE_FOR_REVIEW_DESCRIPTION matches the verbatim spec block."""
    assert MEMEX_GET_DUE_FOR_REVIEW_DESCRIPTION == F20_GET_DUE_VERBATIM


def test_memory_review_description_constant_matches_spec_verbatim():
    """MEMEX_MEMORY_REVIEW_DESCRIPTION matches the verbatim spec block."""
    assert MEMEX_MEMORY_REVIEW_DESCRIPTION == F20_REVIEW_VERBATIM


@pytest.mark.asyncio
async def test_get_due_for_review_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim description on the live tool."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_get_due_for_review')
    assert tool is not None, 'memex_get_due_for_review tool not registered'
    assert tool.description == F20_GET_DUE_VERBATIM


@pytest.mark.asyncio
async def test_memory_review_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim description on the live tool."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_review')
    assert tool is not None, 'memex_memory_review tool not registered'
    assert tool.description == F20_REVIEW_VERBATIM


@pytest.mark.asyncio
async def test_f20_tools_tagged_correctly():
    """Tags are part of the wire contract: read tools advertise readOnlyHint=True
    so consumers can plan parallel dispatch; write tools advertise
    readOnlyHint=False + idempotentHint=False (review IS a state mutation).
    """
    from memex_mcp.server import mcp

    read_tool = await mcp.get_tool('memex_get_due_for_review')
    assert read_tool is not None
    assert 'read' in read_tool.tags
    assert 'storage' in read_tool.tags

    write_tool = await mcp.get_tool('memex_memory_review')
    assert write_tool is not None
    assert 'write' in write_tool.tags
    assert 'storage' in write_tool.tags
