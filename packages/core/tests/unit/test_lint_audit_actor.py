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

Also covers ``_require_attended_mode``: when auth is disabled the apply /
reverse handlers refuse unless the operator opts in via
``MEMEX_LINT_ALLOW_UNATTENDED_APPLY``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from memex_core.context import set_actor
from memex_core.server.lint import _audit_actor, _require_attended_mode


def test_audit_actor_promotes_anonymous_default_to_system():
    set_actor('anonymous')
    assert _audit_actor() == 'system:auth-disabled'


def test_audit_actor_promotes_empty_to_system():
    set_actor('')
    assert _audit_actor() == 'system:auth-disabled'


def test_audit_actor_passes_through_authenticated_label():
    set_actor('scoped-writer (abc123de...)')
    assert _audit_actor() == 'scoped-writer (abc123de...)'


def _api_with_auth(enabled: bool):
    return SimpleNamespace(
        config=SimpleNamespace(server=SimpleNamespace(auth=SimpleNamespace(enabled=enabled)))
    )


def test_apply_requires_auth_enabled_or_opt_in(monkeypatch):
    monkeypatch.delenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', raising=False)
    with pytest.raises(HTTPException) as excinfo:
        _require_attended_mode(_api_with_auth(False))
    assert excinfo.value.status_code == 403
    assert 'MEMEX_LINT_ALLOW_UNATTENDED_APPLY' in excinfo.value.detail


def test_apply_allowed_when_opt_in_env_set(monkeypatch):
    monkeypatch.setenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', '1')
    _require_attended_mode(_api_with_auth(False))


@pytest.mark.parametrize('value', ['true', 'TRUE', 'yes', 'YES'])
def test_apply_opt_in_accepts_truthy_values(monkeypatch, value):
    monkeypatch.setenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', value)
    _require_attended_mode(_api_with_auth(False))


def test_apply_opt_in_rejects_other_values(monkeypatch):
    monkeypatch.setenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', 'maybe')
    with pytest.raises(HTTPException) as excinfo:
        _require_attended_mode(_api_with_auth(False))
    assert excinfo.value.status_code == 403


def test_apply_passthrough_when_auth_enabled(monkeypatch):
    monkeypatch.delenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', raising=False)
    _require_attended_mode(_api_with_auth(True))
