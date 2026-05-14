"""Template name constants for Hermes-originated notes."""

from __future__ import annotations

from memex_common.agent_surface import (
    HISTORICAL_ROUTING as _CANONICAL_HISTORICAL_ROUTING,
    LAYER_ROUTING_PRIMER_FRAGMENT as _CANONICAL_LAYER_ROUTING_PRIMER_FRAGMENT,
    RESOLUTION_FLOW as _CANONICAL_RESOLUTION_FLOW,
)

# Template used for the per-session transcript note ingested on exit.
HERMES_SESSION_TEMPLATE = 'hermes-session'

# Template used for explicit ``memex_add_note`` captures with no other template.
HERMES_USER_NOTE_TEMPLATE = 'hermes-user-note'

# Template for future ``memex_retro`` structured postmortems (v2).
AGENT_REFLECTION_TEMPLATE = 'agent_reflection'

__all__ = [
    'AGENT_REFLECTION_TEMPLATE',
    'HERMES_SESSION_TEMPLATE',
    'HERMES_USER_NOTE_TEMPLATE',
]


# ============================================================
# Tier A — Prompt-fragment templates
# F4:  WS-quick-wins  (deprioritize/restore disclosure)
# F5:  WS-quick-wins  (summarize_node prompt)
# F8:  WS-linter      (get_lint_flags discoverability)
# F9:  WS-locks       (reconsolidate/consolidate disclosure)
# F32: WS-diagnostics (diagnostics_summary disclosure)
# ============================================================

# --- F4 ---  (filled by WS-quick-wins)

# --- F5 ---  (filled by WS-quick-wins)

# --- F8 ---  (filled by WS-linter)

# --- F9 ---  (filled by WS-locks)

# --- F32 --- (filled by WS-diagnostics)
DIAGNOSTICS_SUMMARY_PROMPT_FRAGMENT = (
    'Diagnostics: call `memex_get_diagnostics_summary(vault_id=...)` to inspect '
    "a vault's health — unit counts by status (active/stale/deprioritized), "
    'pending lint counts by type, cluster_count (null when manifold cache is '
    'cold), avg MW score, and top retrieved entities. Synchronous; surfaces '
    'manifold status without waiting on UMAP compute.'
)


# --- F43 --- 5-step user-confirmed-fix resolution flow.
# SSOT is `memex_common.agent_surface.RESOLUTION_FLOW`. Templates re-export
# by identity so any per-turn scaffolding prompt that names this constant
# pulls the canonical bytes — no drift possible.
RESOLUTION_FLOW_PROMPT_FRAGMENT = _CANONICAL_RESOLUTION_FLOW


# --- F3 --- (4-layer memory-routing primer — single source of truth lives in
# `memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT`. Re-exported here
# under the `_PROMPT_FRAGMENT` suffix to match the templates.py naming
# convention for prompt fragments. The briefing.py `_LAYER_ROUTING_PRIMER`
# (table form) is sourced from the same module.)
LAYER_ROUTING_PROMPT_FRAGMENT = _CANONICAL_LAYER_ROUTING_PRIMER_FRAGMENT


# --- F43 --- historical / audit-query routing rule.
# SSOT is `memex_common.agent_surface.HISTORICAL_ROUTING`.
HISTORICAL_ROUTING_PROMPT_FRAGMENT = _CANONICAL_HISTORICAL_ROUTING


__all__ += [
    'HISTORICAL_ROUTING_PROMPT_FRAGMENT',
    'LAYER_ROUTING_PROMPT_FRAGMENT',
    'RESOLUTION_FLOW_PROMPT_FRAGMENT',
]
