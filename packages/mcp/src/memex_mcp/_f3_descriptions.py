"""Verbatim agent prompt text for F3 — 4-layer memory-routing primer.

The canonical strings live in ``memex_common.agent_surface`` so all four
agent surfaces (MCP, Hermes briefing, Hermes templates, Claude Code rule)
import from a single module. This file re-exports them under their
historical names so existing call sites in ``memex_mcp.server`` keep
working without churn.

Sourced from cognitive-memory-research-report.md §2.3 (Memory types and
how Memex covers them) + §4 F3 ("Agent prompt text"). When the spec
changes, edit ``memex_common/agent_surface.py``; the parity test in
``packages/hermes-plugin/tests/test_f3_layer_primer_parity.py`` will fail
until every surface is updated.
"""

from __future__ import annotations

from memex_common.agent_surface import (
    LAYER_ROUTING_PRIMER_FRAGMENT,
    LAYER_ROUTING_PRIMER_PROSE,
    LAYER_ROUTING_PRIMER_TABLE,
)

__all__ = [
    'LAYER_ROUTING_PRIMER_FRAGMENT',
    'LAYER_ROUTING_PRIMER_PROSE',
    'LAYER_ROUTING_PRIMER_TABLE',
]
