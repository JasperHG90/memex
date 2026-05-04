"""Verbatim agent prompt text for 4-layer memory-routing primer.

The canonical strings live in ``memex_common.agent_surface`` so all four
agent surfaces import from a single module. This file is a same-package
re-export shim: ``memex_mcp.server`` imports from here (a sibling module
within the MCP package) so the only memex_common dependency surface for the
MCP package is the well-typed ``memex_common.agent_surface`` module rather
than a bare ``from memex_common.agent_surface import ...`` line scattered
through ``server.py``.

When the descriptions change, edit ``memex_common/agent_surface.py``; the
parity test will fail until every surface is updated.
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
