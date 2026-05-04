"""reconsolidate/consolidate tool description verbatim test.

The expected text is hardcoded here, NOT loaded from the source
(non-circular: when the source changes, this test fails — that is the
contract).
"""

from __future__ import annotations

import pytest

from memex_mcp._reconsolidate_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
)


# When the source description changes, this constant must be re-synced
# and this test is the failing contract.
RECONSOLIDATE_VERBATIM = (
    'memory_reconsolidate — Re-evaluate memories for a specific entity, detecting\n'
    'contradictions and updating mental models. Use when you notice retrieved facts\n'
    'about an entity disagree.\n'
    '\n'
    '- entity_id: target entity\n'
    '\n'
    'Runs contradiction detection across all units linked to the entity, then triggers\n'
    'reflection to produce updated mental models. LLM-intensive — use only when there\n'
    'is concrete evidence of conflicting information.'
)

CONSOLIDATE_VERBATIM = (
    'memory_consolidate — Vault-wide batch curation. Identifies low-Memory-Worth + stale units\n'
    'and deprioritizes them. Writes findings to the maintenance ledger.\n'
    '\n'
    '- vault_id: target vault\n'
    '- dry_run (default false): if true, preview without making changes\n'
    '\n'
    'Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer\n'
    'memory_reconsolidate.'
)


def test_reconsolidate_description_constant_matches_spec_verbatim():
    """MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION matches spec char-for-char."""
    assert MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION == RECONSOLIDATE_VERBATIM


def test_consolidate_description_constant_matches_spec_verbatim():
    """MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION matches spec char-for-char."""
    assert MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION == CONSOLIDATE_VERBATIM


@pytest.mark.asyncio
async def test_reconsolidate_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim reconsolidate description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_reconsolidate')
    assert tool is not None, 'memex_memory_reconsolidate tool not registered'
    assert tool.description == RECONSOLIDATE_VERBATIM


@pytest.mark.asyncio
async def test_consolidate_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim consolidate description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_consolidate')
    assert tool is not None, 'memex_memory_consolidate tool not registered'
    assert tool.description == CONSOLIDATE_VERBATIM
