"""Shared KV-key utilities (cross-package SSOT).

The valid KV namespace prefixes, shared by the write-side validator
(``memex_core``) and any client-side checks (``memex_common``) so the
two can't drift.

Procedures and strategies are NOT a KV namespace — they live in the
procedural plane (``procedural_entries``). KV holds only the simple
namespaced key→value entries below.
"""

from __future__ import annotations

VALID_NAMESPACES = ('global', 'user', 'project', 'app')

__all__ = [
    'VALID_NAMESPACES',
]
