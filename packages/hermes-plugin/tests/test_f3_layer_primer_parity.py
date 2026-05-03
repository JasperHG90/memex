"""F3 — cross-surface parity test for the 4-layer memory-routing primer.

Asserts that the canonical §2.3 / §4 F3 layer-routing primer concepts appear
in ALL FOUR agent surfaces:

  1. MCP tool descriptions (the 5 search tools — `memex_note_search`,
     `memex_memory_search`, `memex_survey`, `memex_get_entity_mentions`,
     `memex_kv_search`)
  2. Hermes session-briefing primer
     (``memex_hermes_plugin.memex.briefing._LAYER_ROUTING_PRIMER``)
  3. Hermes templates fragment
     (``memex_hermes_plugin.memex.templates.LAYER_ROUTING_PROMPT_FRAGMENT``)
  4. Claude Code plugin rule
     (``packages/claude-code-plugin/rules/memory-layers.md``)
  5. Root ``CLAUDE.md`` "Memory layers and tool routing" section

Per CLAUDE.md rule 24 (agent-surface parity): the layer-routing guidance must
not drift between surfaces. A failing assertion here indicates a real surface
drift — fix the surface, do not relax the assertion.

Source: cognitive-memory-research-report.md §2.3 + §4 F3 (added 2026-05-03).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip('memex_mcp')

from memex_hermes_plugin.memex.briefing import _LAYER_ROUTING_PRIMER  # noqa: E402
from memex_hermes_plugin.memex.templates import (  # noqa: E402
    LAYER_ROUTING_PROMPT_FRAGMENT,
)
from memex_mcp._f3_descriptions import (  # noqa: E402
    LAYER_ROUTING_PRIMER_PROSE,
    LAYER_ROUTING_PRIMER_TABLE,
)
from memex_mcp.server import mcp  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CC_RULE_PATH = Path(__file__).parents[2] / 'claude-code-plugin' / 'rules' / 'memory-layers.md'
_CLAUDE_MD_PATH = _REPO_ROOT / 'CLAUDE.md'

_F3_TOOLS = (
    'memex_memory_search',
    'memex_note_search',
    'memex_survey',
    'memex_get_entity_mentions',
    'memex_kv_search',
)


async def _gather_mcp_descriptions() -> dict[str, str]:
    """Fetch descriptions for the 5 F3 search tools via the public async API.

    Mirrors F43's round-2 fix to use ``mcp.get_tool`` rather than poking at
    private attributes, so the contract stays stable across FastMCP versions.
    """
    out: dict[str, str] = {}
    for name in _F3_TOOLS:
        tool = await mcp.get_tool(name)
        out[name] = getattr(tool, 'description', '') or ''
    return out


def _mcp_concatenated_tool_descriptions() -> str:
    descriptions = asyncio.run(_gather_mcp_descriptions())
    return '\n'.join(descriptions.values())


def _surfaces() -> dict[str, str]:
    """Return the four surface texts keyed by surface name."""
    return {
        'mcp': _mcp_concatenated_tool_descriptions(),
        'hermes_briefing': _LAYER_ROUTING_PRIMER,
        'hermes_template': LAYER_ROUTING_PROMPT_FRAGMENT,
        'claude_code_rule': _CC_RULE_PATH.read_text(),
        'claude_md_root': _CLAUDE_MD_PATH.read_text(),
    }


_LAYER_NAMES = ('Episodic', 'Semantic', 'Conceptual', 'Procedural-observations')

_CANONICAL_TOOL_PER_LAYER = {
    'Episodic': 'memex_note_search',
    'Semantic': 'memex_memory_search',
    'Conceptual': 'memex_survey',
    'Procedural-observations': 'memex_kv_search',
}


@pytest.mark.parametrize('layer', _LAYER_NAMES)
def test_layer_name_present_in_all_surfaces(layer: str) -> None:
    """Every layer name must appear in every agent surface."""
    failures: list[str] = []
    for surface_name, text in _surfaces().items():
        if layer not in text:
            failures.append(f'{surface_name!r}: missing layer name {layer!r}')
    assert not failures, (
        f'F3 cross-surface parity broke for layer name {layer!r}:\n'
        + '\n'.join('  - ' + f for f in failures)
        + '\nSee cognitive-memory-research-report.md §2.3 + §4 F3.'
    )


@pytest.mark.parametrize('layer,tool_name', list(_CANONICAL_TOOL_PER_LAYER.items()))
def test_canonical_tool_present_in_all_surfaces(layer: str, tool_name: str) -> None:
    """Each layer's canonical retrieval tool must be named in every surface."""
    failures: list[str] = []
    for surface_name, text in _surfaces().items():
        if tool_name not in text:
            failures.append(
                f'{surface_name!r}: missing canonical tool {tool_name!r} (layer {layer!r})'
            )
    assert not failures, (
        f'F3 cross-surface parity broke for {layer!r} → {tool_name!r}:\n'
        + '\n'.join('  - ' + f for f in failures)
        + '\nSee cognitive-memory-research-report.md §2.3 + §4 F3.'
    )


def test_mcp_search_tools_carry_prose_primer() -> None:
    """All 5 F3 MCP search tools must include the prose primer verbatim."""
    descriptions = asyncio.run(_gather_mcp_descriptions())
    missing = [
        name for name, desc in descriptions.items() if LAYER_ROUTING_PRIMER_PROSE not in desc
    ]
    assert not missing, (
        'F3 MCP tool description drift — these tools do not carry the '
        f'canonical prose primer verbatim: {missing!r}. The canonical text '
        'lives in `memex_mcp._f3_descriptions.LAYER_ROUTING_PRIMER_PROSE`.'
    )


def test_table_surfaces_carry_table_primer_verbatim() -> None:
    """Hermes briefing, Claude Code rule, and CLAUDE.md must all carry the
    canonical markdown table from `LAYER_ROUTING_PRIMER_TABLE`.

    This test pins the table-shape surfaces to a single source of truth —
    drift in any of the three files trips this assertion.
    """
    table_body = LAYER_ROUTING_PRIMER_TABLE.split('\n', 1)[1].strip()

    surfaces = {
        'hermes_briefing': _LAYER_ROUTING_PRIMER,
        'claude_code_rule': _CC_RULE_PATH.read_text(),
        'claude_md_root': _CLAUDE_MD_PATH.read_text(),
    }

    failures: list[str] = []
    for name, text in surfaces.items():
        for canonical_row in (
            '| **Episodic** ',
            '| **Semantic** ',
            '| **Conceptual** ',
            '| **Procedural-observations** ',
        ):
            if canonical_row not in text:
                failures.append(f'{name!r}: missing canonical row prefix {canonical_row!r}')
    assert not failures, (
        'F3 markdown-table parity broke:\n'
        + '\n'.join('  - ' + f for f in failures)
        + f'\nCanonical table:\n{table_body[:200]}...'
    )
