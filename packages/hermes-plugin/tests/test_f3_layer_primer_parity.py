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


@pytest.fixture(scope='module')
def surfaces() -> dict[str, str]:
    """Return the five surface texts keyed by surface name.

    Computed once per module: `_mcp_concatenated_tool_descriptions` runs an
    `asyncio.run(...)` event loop, so a module-scoped fixture amortises that
    cost across the parametrized layer × tool tests instead of paying it on
    every parametrized case.
    """
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
def test_layer_name_present_in_all_surfaces(layer: str, surfaces: dict[str, str]) -> None:
    """Every layer name must appear in every agent surface."""
    failures: list[str] = []
    for surface_name, text in surfaces.items():
        if layer not in text:
            failures.append(f'{surface_name!r}: missing layer name {layer!r}')
    assert not failures, (
        f'F3 cross-surface parity broke for layer name {layer!r}:\n'
        + '\n'.join('  - ' + f for f in failures)
        + '\nSee cognitive-memory-research-report.md §2.3 + §4 F3.'
    )


@pytest.mark.parametrize('layer,tool_name', list(_CANONICAL_TOOL_PER_LAYER.items()))
def test_canonical_tool_present_in_all_surfaces(
    layer: str, tool_name: str, surfaces: dict[str, str]
) -> None:
    """Each layer's canonical retrieval tool must be named in every surface."""
    failures: list[str] = []
    for surface_name, text in surfaces.items():
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


_CANONICAL_TABLE_ROWS = (
    '| **Episodic** ("what happened, when") | Timestamped, source-attributed Notes — sessions, reflections, decisions | `memex_note_search` / `memex_recent_notes` / `memex_find_note` | "Find yesterday\'s reflection about the deploy regression" |',
    '| **Semantic** ("decontextualised facts") | MemoryUnits — short fact/observation/event statements extracted from notes | `memex_memory_search` / `memex_get_memory_units` / `memex_get_entity_mentions` | "What does v2 use for auth?" |',
    '| **Conceptual** ("synthesised mental models") | MentalModels — reflection output bundling per-entity observations with trend tracking (new/strengthening/stable/weakening/stale) | `memex_survey` / `memex_get_entities` (with `mental_models=True`) | "What do you know about Project X overall?" |',
    '| **Procedural-observations** ("adaptations to context") | KV entries under `procedure:<verb>:<context-tag>` — observations about how to adapt your existing skills to a context, NOT the procedures themselves | `memex_kv_search` / `memex_kv_get` with `prefix=\'procedure:\'` | "For this user, `deploy` means staging — never prod after 6pm" |',
)


def test_table_surfaces_carry_full_canonical_rows() -> None:
    """Hermes briefing, Claude Code rule, and CLAUDE.md must all carry every
    canonical markdown row from `LAYER_ROUTING_PRIMER_TABLE` verbatim.

    Row matching is full-row (layer name + What it stores + Retrieve with +
    Tiny example), so drift in any column on any of the three surfaces trips
    this assertion. The four canonical rows live in `_CANONICAL_TABLE_ROWS`
    above and are derived from `LAYER_ROUTING_PRIMER_TABLE` (the test asserts
    each row is present in that source first, then in every surface).
    """
    for canonical_row in _CANONICAL_TABLE_ROWS:
        assert canonical_row in LAYER_ROUTING_PRIMER_TABLE, (
            'Canonical row drifted from `LAYER_ROUTING_PRIMER_TABLE` — update '
            f'`_CANONICAL_TABLE_ROWS` to mirror the source: {canonical_row!r}'
        )

    surfaces_to_check = {
        'hermes_briefing': _LAYER_ROUTING_PRIMER,
        'claude_code_rule': _CC_RULE_PATH.read_text(),
        'claude_md_root': _CLAUDE_MD_PATH.read_text(),
    }

    failures: list[str] = []
    for name, text in surfaces_to_check.items():
        for canonical_row in _CANONICAL_TABLE_ROWS:
            if canonical_row not in text:
                failures.append(f'{name!r}: missing canonical row {canonical_row!r}')
    assert not failures, 'F3 markdown-table parity broke (full-row check):\n' + '\n'.join(
        '  - ' + f for f in failures
    )


def test_table_primer_single_source_of_truth() -> None:
    """`_LAYER_ROUTING_PRIMER` (briefing.py) MUST be the same string object as
    `LAYER_ROUTING_PRIMER_TABLE` (`_f3_descriptions.py`).

    Pins the import-from-source-of-truth structure introduced in
    Hermes round-1: drift can no longer happen because there is only one
    string. If a future change splits them apart, this test fails before any
    column-level drift can sneak in.
    """
    assert _LAYER_ROUTING_PRIMER is LAYER_ROUTING_PRIMER_TABLE, (
        '`_LAYER_ROUTING_PRIMER` must be imported from '
        '`memex_mcp._f3_descriptions.LAYER_ROUTING_PRIMER_TABLE` so the table '
        'has a single source of truth.'
    )


def test_prompt_fragment_single_source_of_truth() -> None:
    """`templates.LAYER_ROUTING_PROMPT_FRAGMENT` MUST be the same string
    object as `_f3_descriptions.LAYER_ROUTING_PRIMER_FRAGMENT`.

    Pins the import-from-source-of-truth structure for the prompt fragment.
    The PRIMER_FRAGMENT (canonical name in `_f3_descriptions`) and the
    PROMPT_FRAGMENT (templates.py-side alias) must point at the same string.
    """
    from memex_mcp._f3_descriptions import LAYER_ROUTING_PRIMER_FRAGMENT

    assert LAYER_ROUTING_PROMPT_FRAGMENT is LAYER_ROUTING_PRIMER_FRAGMENT, (
        '`templates.LAYER_ROUTING_PROMPT_FRAGMENT` must be imported from '
        '`memex_mcp._f3_descriptions.LAYER_ROUTING_PRIMER_FRAGMENT` so the '
        'fragment has a single source of truth.'
    )
