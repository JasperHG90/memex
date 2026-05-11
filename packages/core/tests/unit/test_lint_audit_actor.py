"""Audit-actor resolution for the lint apply / reverse endpoints.

When ``auth.enabled=False`` (dev/test/CI), ``auth_middleware`` never runs
and the ``actor`` contextvar keeps its default ``'anonymous'`` value. The
service layer requires a non-empty actor, so the lint apply / reverse
endpoints would be unreachable without a fallback. ``_audit_actor`` promotes
the anonymous default to ``'system:auth-disabled'`` so the endpoints stay
reachable, the audit trail still identifies the call path, and the value
cannot collide with a real principal literally named ``'system'``.

When auth IS enabled, ``auth_middleware`` sets the contextvar to
``f'{key_name} ({key_prefix})'``; ``_audit_actor`` passes that through
unchanged so audit-string shape matches every other audit-emitting code path.
"""

from __future__ import annotations

from memex_core.context import set_actor
from memex_core.server.lint import _audit_actor


def test_audit_actor_promotes_anonymous_default_to_system():
    set_actor('anonymous')
    assert _audit_actor() == 'system:auth-disabled'


def test_audit_actor_promotes_empty_to_system():
    set_actor('')
    assert _audit_actor() == 'system:auth-disabled'


def test_audit_actor_passes_through_authenticated_label():
    set_actor('scoped-writer (abc123de...)')
    assert _audit_actor() == 'scoped-writer (abc123de...)'
