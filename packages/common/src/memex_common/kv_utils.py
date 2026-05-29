"""Shared KV-key utilities (cross-package SSOT).

Procedure-key detection logic that needs to run in both ``memex_core``
(write-side validator, briefing classifier) and ``memex_common`` (HTTP
client DTO routing). Extracted here to remove the previous inline-duplicate
in ``client.py`` that drifted from ``parse_procedure_key`` in core.

The canonical procedure key shape is ``<scope>:procedure:<verb>:<context>``
where ``<scope>`` is one of:

- ``global`` — flat (no id segment)
- ``user`` — flat (no id segment)
- ``project:<id>`` — id segment REQUIRED
- ``app:<app-id>`` — id segment REQUIRED

Bare ``procedure:*`` is REJECTED — procedures are not a top-level namespace.
"""

from __future__ import annotations

import re

# Mirrors memex_core.services.kv.VALID_NAMESPACES + _NAMESPACES_WITH_ID.
# Kept in sync via the identity tests in test_kv_utils.py.
_VALID_NAMESPACES = ('global', 'user', 'project', 'app')
_NAMESPACES_WITH_ID = ('project', 'app')

_VERB_CONTEXT_RE = re.compile(r'^[a-z][a-z0-9_-]*$')


def parse_procedure_key(key: str) -> tuple[str, str, str] | None:
    """Return ``(scope, verb, context)`` if ``key`` is a procedure key.

    See module docstring for the canonical shape. Returns ``None`` for any
    key that is not a valid procedure key (including bare ``procedure:*``,
    keys with malformed verb/context, keys whose scope prefix itself
    contains ``:procedure:`` — ambiguity is refused rather than guessed).

    Uses ``rsplit`` to peel ``:procedure:<verb>:<context>`` from the right
    so arbitrary characters in the scope prefix (slashes, dots, ``@``,
    embedded ``:`` — e.g. SSH-form git remotes ``git@github.com:acme/foo``)
    flow through unmodified.
    """
    rsplit = key.rsplit(':procedure:', 1)
    if len(rsplit) != 2:
        return None
    scope, suffix = rsplit
    if not scope or ':procedure:' in scope:
        return None

    matched_ns = None
    for ns in _VALID_NAMESPACES:
        if scope == ns:
            if ns in _NAMESPACES_WITH_ID:
                return None  # bare project / app — id segment required
            matched_ns = ns
            break
        if scope.startswith(f'{ns}:'):
            if ns not in _NAMESPACES_WITH_ID:
                return None  # global:foo / user:foo — flat namespaces
            if scope == f'{ns}:':
                return None  # empty id segment
            matched_ns = ns
            break
    if matched_ns is None:
        return None

    suffix_parts = suffix.split(':')
    if len(suffix_parts) != 2:
        return None
    verb, context = suffix_parts
    if not (_VERB_CONTEXT_RE.match(verb) and _VERB_CONTEXT_RE.match(context)):
        return None
    return (scope, verb, context)


def is_procedure_key(key: str) -> bool:
    """True if ``key`` is a valid procedure key under any scope namespace."""
    return parse_procedure_key(key) is not None


__all__ = ['is_procedure_key', 'parse_procedure_key']
