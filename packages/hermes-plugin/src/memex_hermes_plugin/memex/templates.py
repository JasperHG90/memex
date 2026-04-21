"""Template name constants for Hermes-originated notes."""

from __future__ import annotations

# Template used for the per-session transcript note ingested on exit.
HERMES_SESSION_TEMPLATE = 'hermes-session'

# Template used for explicit ``memex_retain`` captures with no other template.
HERMES_USER_NOTE_TEMPLATE = 'hermes-user-note'

# Template for future ``memex_retro`` structured postmortems (v2).
AGENT_REFLECTION_TEMPLATE = 'agent_reflection'

__all__ = [
    'AGENT_REFLECTION_TEMPLATE',
    'HERMES_SESSION_TEMPLATE',
    'HERMES_USER_NOTE_TEMPLATE',
]
