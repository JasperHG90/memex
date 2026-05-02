"""TC-F38-6 — F38 ships NO agent-callable surface (AC-F38-5 invariant).

RFC-010 §"No agent-facing surface (AC-F38-5)": F38 is operator-facing only
(scheduler + CLI). It must NEVER expose:

* an MCP tool (would let an agent trigger consolidation or prune)
* a Hermes plugin schema (same risk via the Hermes provider)
* a Claude Code SKILL.md mention (same risk via the plugin)

This test locks the invariant against future drift. If a later PR
accidentally registers a `memex_consolidate` MCP verb or adds a
`MEMORY_CONSOLIDATE_SCHEMA` to Hermes' ALL_SCHEMAS, this test trips
before merge.

Plus AC-X-10 boot-discovery canary: the MCP tag taxonomy + tool count
remains unchanged because F38 ships zero MCP tools.
"""

from __future__ import annotations

import pathlib as plb

import pytest


_FORBIDDEN_AGENT_NAMES = {
    'memex_consolidate',
    'memex_consolidation_tick',
    'memex_consolidation_status',
    'memex_get_consolidation_status',
    'memex_prune_stale_evidence',
    'memex_prune',
}


# ---------------------------------------------------------------------------
# AC-F38-5 invariant — three surface boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f38_no_mcp_tool() -> None:
    """No F38 MCP verb is registered (the agent surface)."""
    from memex_mcp.server import mcp

    tools = await mcp._list_tools()
    names = {t.name for t in tools}
    intersect = names & _FORBIDDEN_AGENT_NAMES
    assert not intersect, (
        f'AC-F38-5 violation: F38 must NOT expose MCP tools, but found: {sorted(intersect)}'
    )


def test_f38_no_hermes_schema() -> None:
    """No F38 verb is mentioned in the Hermes plugin's tools.py module.

    Source-level check: the Hermes runtime's :mod:`tools.registry` is not
    available in the dev venv, so we can't import the module to inspect
    ALL_SCHEMAS at runtime. Reading the source covers the same surface —
    a schema with name='memex_consolidate' would have to land somewhere in
    this file as a string literal, and adding it would trip this test.
    """
    tools_path = (
        plb.Path(__file__).resolve().parents[3]
        / 'hermes-plugin'
        / 'src'
        / 'memex_hermes_plugin'
        / 'memex'
        / 'tools.py'
    )
    source = tools_path.read_text()
    found = sorted(name for name in _FORBIDDEN_AGENT_NAMES if name in source)
    assert not found, f'AC-F38-5 violation: F38 verb appears in Hermes tools.py: {found}'


def test_f38_no_claude_code_skill_mention() -> None:
    """No F38 verb is mentioned in the Claude Code plugin's SKILL.md files."""
    plugin_root = plb.Path(__file__).resolve().parents[3] / 'claude-code-plugin' / 'skills'
    skill_dirs = ('remember', 'recall', 'retro')
    found_in: dict[str, list[str]] = {}
    for skill in skill_dirs:
        path = plugin_root / skill / 'SKILL.md'
        if not path.exists():
            continue
        text = path.read_text()
        hits = sorted(name for name in _FORBIDDEN_AGENT_NAMES if name in text)
        if hits:
            found_in[skill] = hits
    assert not found_in, f'AC-F38-5 violation: F38 verb mentioned in Claude Code skills: {found_in}'


# ---------------------------------------------------------------------------
# AC-X-10 boot-discovery canary — MCP tag taxonomy + tool count unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f38_does_not_add_mcp_tag() -> None:
    """F38 must not add a new tag to the MCP tag taxonomy.

    The :mod:`memex_mcp` discovery layer treats the tag set as load-bearing
    surface area. F38 ships no MCP tool → no new tag. If a future change
    adds a 'consolidation' tag, the existing
    ``test_discovery_mode.py::test_tag_taxonomy`` trips — this test makes
    the invariant explicit at the F38 boundary instead of relying on the
    other suite to fail first.
    """
    from memex_mcp.server import mcp

    # Mirrors EXPECTED_TAGS in packages/mcp/tests/test_discovery_mode.py.
    # Kept inline because mcp/tests is not an importable package.
    expected_tags = {
        'search',
        'read',
        'write',
        'browse',
        'entities',
        'assets',
        'storage',
        'templates',
        'diagnostics',
    }

    tools = await mcp._list_tools()
    all_tags: set[str] = set()
    for t in tools:
        all_tags |= t.tags or set()

    forbidden_tags = {'consolidation', 'consolidate'}
    assert not (all_tags & forbidden_tags), (
        f'AC-X-10 violation: F38 introduced a new MCP tag: {all_tags & forbidden_tags}'
    )
    assert all_tags == expected_tags, (
        f'AC-X-10 violation: MCP tag taxonomy drifted. '
        f'Added: {sorted(all_tags - expected_tags)} '
        f'Removed: {sorted(expected_tags - all_tags)}'
    )
