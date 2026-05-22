"""Shared note identity utilities."""

from __future__ import annotations

import hashlib
from uuid import UUID


def derive_note_uuid_from_key(note_key: str) -> UUID:
    """Derive a deterministic Note.id UUID from a user-supplied note_key.

    Mirrors NoteInput.note_key (api.py): if the caller passed an actual UUID
    string, use it; otherwise, hash the key with MD5.
    """
    try:
        return UUID(note_key)
    except ValueError:
        return UUID(hashlib.md5(note_key.encode('utf-8')).hexdigest())
