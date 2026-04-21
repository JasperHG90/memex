"""Session note key lifecycle.

Each Hermes session gets a unique ``note_key`` that the plugin exposes to the
model via ``system_prompt_block``. The model calls ``memex_retain(note_key=...)``
to append meaningful progress; ``on_session_end`` finalizes the note with the
full transcript. Memex's note-key upsert semantics handle idempotency.
"""

from __future__ import annotations

from datetime import datetime, timezone

SESSION_KEY_PREFIX = 'hermes:session:'


def make_session_note_key(now: datetime | None = None) -> str:
    """Return a unique session note key.

    Format: ``hermes:session:<ISO-UTC-timestamp>``. The timestamp includes
    milliseconds to disambiguate sessions started within the same second.
    """
    now = now or datetime.now(timezone.utc)
    # Milliseconds to 3 decimals, trailing Z to mark UTC.
    iso = now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}Z'
    return f'{SESSION_KEY_PREFIX}{iso}'


def is_session_note_key(key: str) -> bool:
    """True if ``key`` looks like a session key this plugin issued."""
    return key.startswith(SESSION_KEY_PREFIX)


__all__ = ['SESSION_KEY_PREFIX', 'is_session_note_key', 'make_session_note_key']
