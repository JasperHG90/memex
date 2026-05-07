"""Cross-surface parity test for the 4-layer memory-routing primer.

Asserts that the canonical §2.3 layer-routing primer concepts appear in
the agent surfaces that are still expected to carry them:

  1. MCP tool descriptions (the 5 search tools — `memex_note_search`,
     `memex_memory_search`, `memex_survey`, `memex_get_entity_mentions`,
     `memex_kv_search`)
  2. Hermes session-briefing primer
     (``memex_hermes_plugin.memex.briefing._LAYER_ROUTING_PRIMER``)
  3. Hermes templates fragment
     (``memex_hermes_plugin.memex.templates.LAYER_ROUTING_PROMPT_FRAGMENT``)
  4. Claude Code plugin rule
     (``packages/claude-code-plugin/rules/memory-layers.md``)

The root ``AGENTS.md`` (and its symlink ``CLAUDE.md``) used to carry the
table verbatim but the repo-wide compression refactor intentionally pruned
it from the root agent instructions. AGENTS.md is therefore no longer a
parity surface for this primer; the canonical text lives in
``memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE``.

Per the agent-surface parity rule: the layer-routing guidance must not
drift between the remaining surfaces. A failing assertion here indicates a
real surface drift — fix the surface, do not relax the assertion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip('memex_mcp')

from memex_common.agent_surface import (  # noqa: E402
    LAYER_ROUTING_PRIMER_FRAGMENT,
    LAYER_ROUTING_PRIMER_PROSE,
    LAYER_ROUTING_PRIMER_TABLE,
)
from memex_hermes_plugin.memex.briefing import _LAYER_ROUTING_PRIMER  # noqa: E402
from memex_hermes_plugin.memex.templates import (  # noqa: E402
    LAYER_ROUTING_PROMPT_FRAGMENT,
)
from memex_mcp.server import mcp  # noqa: E402

_CC_RULE_PATH = Path(__file__).parents[2] / 'claude-code-plugin' / 'rules' / 'memory-layers.md'
# AGENTS.md (and its symlink CLAUDE.md) used to carry the layer-routing
# table verbatim, but the repo-wide compression refactor intentionally
# removed the table from AGENTS.md to keep the root agent instructions
# concise. The canonical text now lives in
# `memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE` and is rendered
# into the Hermes briefing + Claude Code rule, so AGENTS.md is no longer a
# parity surface for this primer.

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


@pytest.fixture(scope='module')
def mcp_tool_descriptions() -> dict[str, str]:
    """Return the per-tool description dict for the 5 F3 search tools.

    Computed once per module: `_gather_mcp_descriptions` is async and runs
    via `asyncio.run(...)` so the event-loop spin-up cost is paid exactly
    once for all tests that need to inspect MCP tool descriptions
    (`test_mcp_search_tools_carry_prose_primer` and the `surfaces` fixture
    which derives `mcp` from this dict).
    """
    return asyncio.run(_gather_mcp_descriptions())


@pytest.fixture(scope='module')
def surfaces(mcp_tool_descriptions: dict[str, str]) -> dict[str, str]:
    """Return the five surface texts keyed by surface name.

    Sourced from the module-scoped `mcp_tool_descriptions` fixture (which
    runs the `asyncio.run(...)` event loop exactly once), so the
    parametrized layer × tool tests pay zero per-case event-loop cost.
    """
    return {
        'mcp': '\n'.join(mcp_tool_descriptions.values()),
        'hermes_briefing': _LAYER_ROUTING_PRIMER,
        'hermes_template': LAYER_ROUTING_PROMPT_FRAGMENT,
        'claude_code_rule': _CC_RULE_PATH.read_text(),
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


def test_mcp_search_tools_carry_prose_primer(
    mcp_tool_descriptions: dict[str, str],
) -> None:
    """All 5 F3 MCP search tools must include the prose primer verbatim."""
    missing = [
        name
        for name, desc in mcp_tool_descriptions.items()
        if LAYER_ROUTING_PRIMER_PROSE not in desc
    ]
    assert not missing, (
        'F3 MCP tool description drift — these tools do not carry the '
        f'canonical prose primer verbatim: {missing!r}. The canonical text '
        'lives in `memex_common.agent_surface.LAYER_ROUTING_PRIMER_PROSE`.'
    )


_CANONICAL_TABLE_ROWS = (
    '| **Episodic** ("what happened, when") | Timestamped, source-attributed Notes — sessions, reflections, decisions | `memex_note_search` / `memex_recent_notes` / `memex_find_note` | "Find yesterday\'s reflection about the deploy regression" |',
    '| **Semantic** ("decontextualised facts") | MemoryUnits — short fact/observation/event statements extracted from notes | `memex_memory_search` / `memex_get_memory_units` / `memex_get_entity_mentions` | "What does v2 use for auth?" |',
    '| **Conceptual** ("synthesised mental models") | MentalModels — reflection output bundling per-entity observations with trend tracking (new/strengthening/stable/weakening/stale) | `memex_survey` / `memex_get_entities` (with `mental_models=True`) | "What do you know about Project X overall?" |',
    '| **Procedural-observations** ("adaptations to context") | KV entries under `procedure:<verb>:<context-tag>` — observations about how to adapt your existing skills to a context, NOT the procedures themselves | `memex_kv_search` / `memex_kv_get` with `prefix=\'procedure:\'` | "For this user, `deploy` means staging — never prod after 6pm" |',
)


def test_table_surfaces_carry_full_canonical_rows() -> None:
    """Hermes briefing, Claude Code rule, and AGENTS.md must all carry every
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
    """`_LAYER_ROUTING_PRIMER` (briefing.py) MUST be the same string as the
    canonical `memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE`.

    Pins the import-from-source-of-truth structure introduced in
    Hermes round-1: drift can no longer happen because there is only one
    string. If a future change splits them apart, this test fails before any
    column-level drift can sneak in.

    Both ``is`` (object identity — Python's import cache means re-importing
    the same name from the same module returns the same object) and ``==``
    (content equality — survives any future test setup that legitimately
    forces a module reload) are asserted. The ``==`` fallback gives the test
    a graceful degradation path while the ``is`` check stays primary.
    """
    assert _LAYER_ROUTING_PRIMER == LAYER_ROUTING_PRIMER_TABLE, (
        '`_LAYER_ROUTING_PRIMER` content drifted from canonical '
        '`memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE`.'
    )
    assert _LAYER_ROUTING_PRIMER is LAYER_ROUTING_PRIMER_TABLE, (
        '`_LAYER_ROUTING_PRIMER` must be imported from '
        '`memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE` so the table '
        'has a single source of truth (object identity, not just equality).'
    )


def test_mcp_shim_single_source_of_truth() -> None:
    """The `memex_mcp._layer_primer_descriptions` shim MUST re-export the canonical
    primer strings from `memex_common.agent_surface` (object identity, not
    just equality).

    Pins the architecture: `_layer_primer_descriptions.py` is intentionally a thin
    same-package shim for `memex_mcp.server`. If a future change replaces
    a re-export with a locally-defined copy, this test fails — even if the
    new copy passes the `LAYER_ROUTING_PRIMER_PROSE in description`
    substring check used by `test_mcp_search_tools_carry_prose_primer`
    (which would miss whitespace-only or formatting drift that preserves
    substring containment).
    """
    from memex_mcp import _layer_primer_descriptions as mcp_shim

    failures: list[str] = []
    for name, canonical in (
        ('LAYER_ROUTING_PRIMER_PROSE', LAYER_ROUTING_PRIMER_PROSE),
        ('LAYER_ROUTING_PRIMER_TABLE', LAYER_ROUTING_PRIMER_TABLE),
        ('LAYER_ROUTING_PRIMER_FRAGMENT', LAYER_ROUTING_PRIMER_FRAGMENT),
    ):
        shim_value = getattr(mcp_shim, name)
        if shim_value is not canonical:
            failures.append(
                f'{name!r}: shim value is not the canonical '
                '`memex_common.agent_surface` object (re-export drifted '
                'into a locally-defined copy)'
            )
    assert not failures, (
        '`memex_mcp._layer_primer_descriptions` SoT shim drifted:\n'
        + '\n'.join('  - ' + f for f in failures)
        + '\nThe shim must re-export from `memex_common.agent_surface` '
        'with no local redefinition.'
    )


def test_prompt_fragment_single_source_of_truth() -> None:
    """`templates.LAYER_ROUTING_PROMPT_FRAGMENT` MUST be the same string as
    the canonical `memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT`.

    Pins the import-from-source-of-truth structure for the prompt fragment.
    The PRIMER_FRAGMENT (canonical name in `agent_surface`) and the
    PROMPT_FRAGMENT (templates.py-side alias) must point at the same string.
    Asserts both ``==`` (content equality) and ``is`` (object identity) —
    see ``test_table_primer_single_source_of_truth`` for the rationale.
    """
    assert LAYER_ROUTING_PROMPT_FRAGMENT == LAYER_ROUTING_PRIMER_FRAGMENT, (
        '`templates.LAYER_ROUTING_PROMPT_FRAGMENT` content drifted from '
        'canonical `memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT`.'
    )
    assert LAYER_ROUTING_PROMPT_FRAGMENT is LAYER_ROUTING_PRIMER_FRAGMENT, (
        '`templates.LAYER_ROUTING_PROMPT_FRAGMENT` must be imported from '
        '`memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT` so the '
        'fragment has a single source of truth (object identity, not just '
        'equality).'
    )
