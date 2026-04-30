"""Template name constants for Hermes-originated notes."""

from __future__ import annotations

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
# F20: WS-revisit     (memory_review prompt)
# F32: WS-diagnostics (diagnostics_summary disclosure)
# ============================================================

# --- F4 ---  (filled by WS-quick-wins)

# --- F5 ---  (filled by WS-quick-wins)

# --- F8 ---  (filled by WS-linter)

# --- F9 ---  (filled by WS-locks)

# --- F20 --- (filled by WS-revisit)

# --- F32 --- (filled by WS-diagnostics)
DIAGNOSTICS_SUMMARY_PROMPT_FRAGMENT = (
    'Diagnostics: call `memex_get_diagnostics_summary(vault_id=...)` to inspect '
    "a vault's health — unit counts by status (active/stale/deprioritized), "
    'pending lint counts by type, cluster_count (null when manifold cache is '
    'cold), avg MW score, and top retrieved entities. Synchronous; surfaces '
    'F32 manifold status without waiting on UMAP compute.'
)
