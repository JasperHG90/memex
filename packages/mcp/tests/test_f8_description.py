"""F8 — verbatim description contract (AC-F8-2).

The agent prompt is the public surface. Drift between
``cognitive-memory-research-report.md`` §4 F8 step 6 and the registered
tool description is a contract violation — this test catches a single
character of drift.
"""

from __future__ import annotations

from memex_mcp._f8_descriptions import MEMEX_GET_LINT_FLAGS_DESCRIPTION
from memex_mcp.server import mcp


_EXPECTED = (
    'memex_get_lint_flags — List pending memory-hygiene findings the linter has detected.\n'
    'Use periodically (e.g., once per long session) or when the user asks about memory state.\n'
    '\n'
    '- vault_id (optional): scope to a single vault. Omit for all-vault view.\n'
    '- lint_type (optional): structural | quality | governance | schema\n'
    '- status (optional): pending | resolved | dismissed (default: pending)\n'
    '- limit (default 20)\n'
    '\n'
    'Each finding includes: target_id, lint_type, evidence (why detected), suggested_action.\n'
    'Most findings can be auto-resolved by calling the relevant tool (e.g., memory_deprioritize\n'
    'for low-MW units). Surface high-confidence findings to the user; act autonomously on\n'
    'low-risk ones (deprioritize, mark stale).'
)


def test_f8_description_constant_matches_section_4_verbatim() -> None:
    """Single character of drift fails. Source: §4 F8 step 6."""
    assert MEMEX_GET_LINT_FLAGS_DESCRIPTION == _EXPECTED


async def test_f8_tool_uses_the_description_constant() -> None:
    """The registered MCP tool MUST source its description from
    ``MEMEX_GET_LINT_FLAGS_DESCRIPTION`` — preventing copy-paste drift."""
    tools = await mcp._list_tools()
    by_name = {t.name: t for t in tools}
    assert 'memex_get_lint_flags' in by_name
    assert by_name['memex_get_lint_flags'].description == MEMEX_GET_LINT_FLAGS_DESCRIPTION


async def test_f8_tool_is_tagged_diagnostics_and_read_only() -> None:
    """Progressive-disclosure surface: tool appears under the 'diagnostics'
    bucket so agents discover it via memex_search(query='lint', tags=[...])."""
    tools = await mcp._list_tools()
    by_name = {t.name: t for t in tools}
    flags = by_name['memex_get_lint_flags']
    assert flags.tags == {'diagnostics'}
    assert flags.annotations.readOnlyHint is True
