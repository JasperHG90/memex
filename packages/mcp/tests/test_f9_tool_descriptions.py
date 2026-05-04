"""F9 — MCP tool description verbatim test.

TC9: tool description == cognitive-memory-research-report.md §4 F9 step 6
verbatim (lines 758-775 as of 2026-05-01).

The expected text is hardcoded here, NOT loaded from the spec markdown
(non-circular: when the spec changes, this test fails — that is the
contract).
"""

from __future__ import annotations

import pytest

from memex_mcp._reconsolidate_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
)


# Source: cognitive-memory-research-report.md §4 F9 step 6 (lines 758-775 as
# of 2026-05-01 against memory_augmentation @ 1c0a464). When the spec changes,
# this constant must be re-synced and this test is the failing contract.
F9_RECONSOLIDATE_VERBATIM = (
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

F9_CONSOLIDATE_VERBATIM = (
    'memory_consolidate — Vault-wide batch curation. Identifies low-MW + stale units\n'
    'and deprioritizes them. Writes findings to the maintenance ledger.\n'
    '\n'
    '- vault_id: target vault\n'
    '- dry_run (default false): if true, preview without making changes\n'
    '\n'
    'Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer\n'
    'memory_reconsolidate.'
)


def test_reconsolidate_description_constant_matches_spec_verbatim():
    """TC9: MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION matches §4 F9 step 6 char-for-char."""
    assert MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION == F9_RECONSOLIDATE_VERBATIM


def test_consolidate_description_constant_matches_spec_verbatim():
    """TC9: MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION matches §4 F9 step 6 char-for-char."""
    assert MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION == F9_CONSOLIDATE_VERBATIM


@pytest.mark.asyncio
async def test_reconsolidate_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim reconsolidate description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_reconsolidate')
    assert tool is not None, 'memex_memory_reconsolidate tool not registered'
    assert tool.description == F9_RECONSOLIDATE_VERBATIM


@pytest.mark.asyncio
async def test_consolidate_tool_registered_with_verbatim_description():
    """The MCP tool registry surfaces the verbatim consolidate description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_consolidate')
    assert tool is not None, 'memex_memory_consolidate tool not registered'
    assert tool.description == F9_CONSOLIDATE_VERBATIM
