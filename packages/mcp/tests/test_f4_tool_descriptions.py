"""F4 — MCP tool description verbatim test.

T4: tool description == cognitive-memory-research-report.md §4 F4 step 6
verbatim (lines 615-628 as of 2026-04-30).

The expected text is hardcoded here, NOT loaded from the spec markdown
(non-circular: when the spec changes, this test fails — that is the
contract).
"""

from __future__ import annotations

import pytest

from memex_mcp._deprioritize_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_MEMORY_RESTORE_DESCRIPTION,
)


# Source: cognitive-memory-research-report.md §4 F4 step 6 (lines 615-628 as
# of 2026-04-30 against memory_augmentation @ 1c0a464). When the spec changes,
# this constant must be re-synced and this test is the failing contract.
F4_DESCRIPTION_VERBATIM = (
    "memory_deprioritize — Lower a memory unit's retrieval rank without deleting it.\n"
    'Use when a memory is misleading, outdated, or noise that contaminates retrieval.\n'
    '\n'
    '- unit_id: the unit to deprioritize\n'
    '- reason: brief text explanation (logged to maintenance ledger). Use this field\n'
    '  liberally to capture WHY (e.g., "user confirmed issue fixed", "superseded by\n'
    '  v2.3 release", "was wrong about deploy target"). Free text is sufficient — no\n'
    '  enum needed.\n'
    '\n'
    'The unit remains accessible via include_deprioritized=true retrieval. To restore,\n'
    'the user runs `memex memory restore <id>`. This is non-destructive — prefer it\n'
    'over hard delete in almost all cases. Use sparingly: a small number of high-quality\n'
    'deprioritizations is more valuable than aggressive pruning.'
)


def test_deprioritize_description_constant_matches_spec_verbatim():
    """T4: MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION matches §4 F4 step 6 char-for-char."""
    assert MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION == F4_DESCRIPTION_VERBATIM


@pytest.mark.asyncio
async def test_mcp_tool_registered_with_verbatim_description_preamble():
    """The MCP tool description starts with the F4 verbatim preamble.

    F43 (cognitive-memory-research-report.md §3.5 + §3.4.2) extends the
    deprioritize description with the 5-step user-confirmed-fix flow + the
    historical-routing rule. The original F4 verbatim description is kept as
    the preamble so the curate-memory-after-the-fact discoverability is
    unchanged. The full description is asserted by F43's verbatim test
    (test_f43_descriptions.py).
    """
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_deprioritize')
    assert tool is not None, 'memex_memory_deprioritize tool not registered'
    assert tool.description.startswith(F4_DESCRIPTION_VERBATIM), (
        'Registered description must start with the F4 verbatim preamble '
        '(§4 F4 step 6); F43 may append additional sections after it.'
    )


@pytest.mark.asyncio
async def test_restore_tool_registered():
    """memex_memory_restore is also registered with its description constant."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_restore')
    assert tool is not None, 'memex_memory_restore tool not registered'
    assert tool.description == MEMEX_MEMORY_RESTORE_DESCRIPTION
