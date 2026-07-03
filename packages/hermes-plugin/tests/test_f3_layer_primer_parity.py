"""F3 — Layer-routing primer SSOT pinning (post-2026-05-14 architecture).

Before 2026-05-14: the 4-layer routing primer was duplicated across five
surfaces (MCP tool descriptions × 5 tools, hermes briefing constant, hermes
templates fragment, Claude Code plugin rule, repo AGENTS.md). The test pinned
cross-surface parity to catch drift between independent copies.

After 2026-05-14 (three-tier agent-surface architecture): there are
exactly two canonical renderings — ``LAYER_ROUTING_PRIMER_TABLE``
(markdown table for briefing / claude-code rule) and
``LAYER_ROUTING_PRIMER_FRAGMENT`` (bullet-list for MCP search-tool
descriptions and Hermes per-turn templates). Downstream surfaces
re-export by object identity (no local string redefinition). Drift is
impossible because each rendering has exactly one source of truth.

If a future surface intentionally wants the table, it should:
1. Import ``LAYER_ROUTING_PRIMER_TABLE`` from ``memex_common.agent_surface``.
2. Add a dedicated ``test_<surface>_layer_primer_is_ssot_object`` here that
   pins ``surface.X is memex_common.agent_surface.X`` (object identity).
   Equality is not enough — a copy can silently drift.

Universal content (the 5-step flow, storage model, KV scope qualifier) is
covered by ``packages/common/tests/test_agent_surface.py`` and surfaced via
``compose_universal()``. The MCP tool descriptions are Tier 1a (terse,
per-tool contract only) and INTENTIONALLY do NOT carry the primer; the
boundary fence for that lives in ``test_briefing_f43_resolution_flow.py``.
"""

from __future__ import annotations

from memex_common.agent_surface import (
    LAYER_ROUTING_PRIMER_FRAGMENT,
    LAYER_ROUTING_PRIMER_TABLE,
)
from memex_hermes_plugin.memex.briefing import _LAYER_ROUTING_PRIMER
from memex_hermes_plugin.memex.templates import LAYER_ROUTING_PROMPT_FRAGMENT


# ---------------------------------------------------------------------------
# Section 1: the SSOT itself carries every canonical concept.
# ---------------------------------------------------------------------------


_LAYER_NAMES = ('Episodic', 'Semantic', 'Conceptual', 'Procedural')

_CANONICAL_TOOL_PER_LAYER = {
    'Episodic': 'memex_note_search',
    'Semantic': 'memex_memory_search',
    'Conceptual': 'memex_survey',
    'Procedural': 'memex_procedural_search',
}


def test_table_carries_every_layer_name() -> None:
    """SSOT (table form) must carry every layer name."""
    for layer in _LAYER_NAMES:
        assert layer in LAYER_ROUTING_PRIMER_TABLE, (
            f'LAYER_ROUTING_PRIMER_TABLE missing canonical layer name {layer!r}'
        )


def test_table_carries_every_canonical_tool() -> None:
    """SSOT (table form) must name each layer's canonical retrieval tool."""
    for layer, tool in _CANONICAL_TOOL_PER_LAYER.items():
        assert tool in LAYER_ROUTING_PRIMER_TABLE, (
            f'LAYER_ROUTING_PRIMER_TABLE missing canonical tool {tool!r} for layer {layer!r}'
        )


def test_fragment_carries_every_layer_name() -> None:
    """SSOT (fragment form) must carry every layer name."""
    for layer in _LAYER_NAMES:
        assert layer in LAYER_ROUTING_PRIMER_FRAGMENT, (
            f'LAYER_ROUTING_PRIMER_FRAGMENT missing canonical layer name {layer!r}'
        )


def test_fragment_carries_every_canonical_tool() -> None:
    """SSOT (fragment form, for templates) names each canonical tool."""
    for layer, tool in _CANONICAL_TOOL_PER_LAYER.items():
        assert tool in LAYER_ROUTING_PRIMER_FRAGMENT, (
            f'LAYER_ROUTING_PRIMER_FRAGMENT missing canonical tool {tool!r} for layer {layer!r}'
        )


# ---------------------------------------------------------------------------
# Section 2: downstream re-export identity (no local copies).
# ---------------------------------------------------------------------------


def test_hermes_briefing_layer_primer_is_ssot_object() -> None:
    """``briefing._LAYER_ROUTING_PRIMER`` must be the canonical SSOT object
    (identity, not just equality). A local copy would re-introduce drift."""
    assert _LAYER_ROUTING_PRIMER is LAYER_ROUTING_PRIMER_TABLE, (
        '`briefing._LAYER_ROUTING_PRIMER` is not the canonical '
        '`memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE` object — '
        'this means a local copy was re-introduced; replace with a re-export.'
    )


def test_hermes_templates_fragment_is_ssot_object() -> None:
    """``templates.LAYER_ROUTING_PROMPT_FRAGMENT`` must be the canonical
    SSOT fragment object (identity)."""
    assert LAYER_ROUTING_PROMPT_FRAGMENT is LAYER_ROUTING_PRIMER_FRAGMENT, (
        '`templates.LAYER_ROUTING_PROMPT_FRAGMENT` is not the canonical '
        '`memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT` object — '
        'replace with a re-export.'
    )


def test_mcp_shim_reexports_are_ssot_objects() -> None:
    """The ``memex_mcp._layer_primer_descriptions`` shim must re-export the
    canonical primer strings from ``memex_common.agent_surface`` by identity."""
    from memex_mcp import _layer_primer_descriptions as mcp_shim

    failures: list[str] = []
    for name, canonical in (
        ('LAYER_ROUTING_PRIMER_TABLE', LAYER_ROUTING_PRIMER_TABLE),
        ('LAYER_ROUTING_PRIMER_FRAGMENT', LAYER_ROUTING_PRIMER_FRAGMENT),
    ):
        shim_value = getattr(mcp_shim, name, None)
        if shim_value is not canonical:
            failures.append(
                f'{name!r}: shim value is not the canonical '
                '`memex_common.agent_surface` object (re-export drifted '
                'into a locally-defined copy)'
            )
    assert not failures, '`memex_mcp._layer_primer_descriptions` SoT shim drifted:\n' + '\n'.join(
        '  - ' + f for f in failures
    )
