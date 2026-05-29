"""Unit tests for procedure-key validation.

Procedures live UNDER an existing scope namespace (NOT a top-level
``procedure:`` namespace). Canonical forms:

- ``global:procedure:<verb>:<context>`` — default global procedure
- ``project:<id>:procedure:<verb>:<context>`` — project-scoped (explicit cue)
- ``user:procedure:<verb>:<context>`` — per-user procedure
- ``app:<app-id>:procedure:<verb>:<context>`` — per-app procedure

The verb and context-tag must match ``[a-z][a-z0-9_-]*``. The scope prefix
is permissive (project IDs can contain slashes, dots, ``@``, embedded ``:``
— necessary for SSH-form git remotes). Bare ``procedure:*`` keys are
REJECTED — procedures are never top-level.
"""

from __future__ import annotations

import pytest

from memex_core.services.kv import (
    VALID_NAMESPACES,
    _looks_like_procedure_key,
    format_procedure_display_name,
    is_procedure_key,
    parse_procedure_key,
    validate_procedure_key,
)


def test_valid_namespaces_does_not_include_procedure() -> None:
    """`procedure` is NOT a top-level namespace — procedures live under
    `global:`, `user:`, `project:`, or `app:`."""
    assert 'procedure' not in VALID_NAMESPACES


@pytest.mark.parametrize(
    'valid_key,expected_scope',
    [
        ('global:procedure:write_pr:commit-style', 'global'),
        ('global:procedure:run_tests:python-monorepo', 'global'),
        ('global:procedure:edit_yaml:ci-config', 'global'),
        ('global:procedure:add-note:vault-binding', 'global'),
        ('global:procedure:reflect_on:f5-debug', 'global'),
        ('user:procedure:greeting:friendly', 'user'),
        ('app:claude-code:procedure:remember:terse', 'app:claude-code'),
        ('app:hermes:procedure:capture:cadence', 'app:hermes'),
    ],
)
def test_validate_procedure_key_accepts_global_user_app(
    valid_key: str, expected_scope: str
) -> None:
    """Procedures under global/user/app scopes (no project_id segment)."""
    validate_procedure_key(valid_key)
    parsed = parse_procedure_key(valid_key)
    assert parsed is not None
    scope, _, _ = parsed
    assert scope == expected_scope


@pytest.mark.parametrize(
    'malformed_key',
    [
        'procedure:verb:context',  # bare — no scope namespace
        'procedure:verb',  # bare + missing context
        'foo:procedure:bar:baz',  # 'foo' is not a valid namespace
        'global:procedure:verb',  # missing context-tag
        'global:procedure:verb:',  # trailing-colon empty context-tag
        'global:procedure::tag',  # empty verb
        'global:procedure:Verb:tag',  # uppercase verb
        'global:procedure:verb:Tag',  # uppercase context-tag
        'global:procedure:verb:tag with space',  # whitespace
        'global:procedure:-verb:tag',  # leading hyphen on verb
        'global:procedure:verb:-tag',  # leading hyphen on context-tag
        'global:procedure:verb:tag:extra',  # triple-segment
        '',  # empty string
        'global:procedure:',  # single-colon
        'global:procedure:verb:9tag',  # leading digit on context-tag
        'global:lang:python',  # not a procedure (no :procedure: infix)
        'global:foo:procedure:verb:context',  # `global` is flat — no sub-id
        'user:foo:procedure:verb:context',  # `user` is flat — no sub-id
        'project:procedure:verb:context',  # bare `project` — needs id
        'app:procedure:verb:context',  # bare `app` — needs id
    ],
)
def test_validate_procedure_key_rejects_malformed(malformed_key: str) -> None:
    with pytest.raises(ValueError, match='Invalid procedure key'):
        validate_procedure_key(malformed_key)


# ---------------------------------------------------------------------------
# Project-scoped procedure keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'valid_key,expected_scope',
    [
        ('project:memex:procedure:commit:pr-workflow', 'project:memex'),
        (
            'project:github.com/JasperHG90/memex:procedure:commit:pr-workflow',
            'project:github.com/JasperHG90/memex',
        ),
        # SSH-form git remote (contains @ and embedded :)
        (
            'project:git@github.com:acme/foo:procedure:run_tests:python',
            'project:git@github.com:acme/foo',
        ),
        # Uppercase repo (the project_id segment is permissive)
        (
            'project:github.com/Acme/Repo:procedure:lint:strict',
            'project:github.com/Acme/Repo',
        ),
        # Path-fallback form (leading slash)
        ('project:/home/me/work/foo:procedure:deploy:gitops', 'project:/home/me/work/foo'),
    ],
)
def test_validate_project_procedure_key_accepts_valid(valid_key: str, expected_scope: str) -> None:
    validate_procedure_key(valid_key)
    parsed = parse_procedure_key(valid_key)
    assert parsed is not None
    scope, _, _ = parsed
    assert scope == expected_scope


@pytest.mark.parametrize(
    'malformed_key',
    [
        # Empty project_id segment
        'project::procedure:verb:context',
        # Uppercase verb under project scope
        'project:memex:procedure:Verb:context',
        # Uppercase context under project scope
        'project:memex:procedure:verb:Context',
        # Missing context-tag
        'project:memex:procedure:verb',
        # Too many segments after procedure
        'project:memex:procedure:verb:context:extra',
        # Scope contains ':procedure:' substring (ambiguous; refuse)
        'project:foo:procedure:bar:procedure:verb:context',
        # Whitespace in verb
        'project:memex:procedure:verb with space:context',
        # Leading digit on verb
        'project:memex:procedure:9verb:context',
    ],
)
def test_validate_project_procedure_key_rejects_malformed(malformed_key: str) -> None:
    with pytest.raises(ValueError, match='Invalid procedure key'):
        validate_procedure_key(malformed_key)


def test_parse_procedure_key_global() -> None:
    assert parse_procedure_key('global:procedure:commit:pr') == ('global', 'commit', 'pr')


def test_parse_procedure_key_returns_none_for_non_procedure() -> None:
    assert parse_procedure_key('user:editor') is None
    assert parse_procedure_key('project:memex:vault') is None
    assert parse_procedure_key('global:lang:python') is None
    assert parse_procedure_key('procedure:commit:pr') is None  # bare — no longer valid
    assert parse_procedure_key('') is None


def test_is_procedure_key_recognizes_all_scopes() -> None:
    assert is_procedure_key('global:procedure:commit:pr')
    assert is_procedure_key('user:procedure:greeting:friendly')
    assert is_procedure_key('project:memex:procedure:commit:pr')
    assert is_procedure_key('app:claude-code:procedure:remember:terse')
    assert not is_procedure_key('user:editor')
    assert not is_procedure_key('project:memex:vault')
    assert not is_procedure_key('procedure:commit:pr')  # bare form rejected


def test_format_procedure_display_name_global_strips_prefix() -> None:
    assert (
        format_procedure_display_name('global:procedure:commit:pr-workflow') == 'commit:pr-workflow'
    )


def test_format_procedure_display_name_project_carries_scope_tag() -> None:
    assert (
        format_procedure_display_name('project:memex:procedure:commit:pr-workflow')
        == '[project:memex] commit:pr-workflow'
    )


def test_format_procedure_display_name_user_app_scopes() -> None:
    assert (
        format_procedure_display_name('user:procedure:greeting:friendly')
        == '[user] greeting:friendly'
    )
    assert (
        format_procedure_display_name('app:claude-code:procedure:remember:terse')
        == '[app:claude-code] remember:terse'
    )


def test_format_procedure_display_name_passthrough_for_non_procedure() -> None:
    assert format_procedure_display_name('user:editor') == 'user:editor'


# ---------------------------------------------------------------------------
# `_looks_like_procedure_key` — gate between "reject as malformed procedure"
# (raise ValueError on write) vs "accept as plain KV write" (no validator).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'key,expected',
    [
        # Well-formed procedure-shaped keys — looks_like = True so that
        # validate_procedure_key actually runs on the write path.
        ('global:procedure:commit:lint', True),
        ('user:procedure:greeting:friendly', True),
        ('project:memex:procedure:commit:pr', True),
        ('app:claude-code:procedure:remember:terse', True),
        # Malformed procedure-shaped keys — looks_like = True so the
        # validator rejects them rather than silently storing as plain KV.
        ('global:procedure:Verb:ctx', True),  # uppercase verb
        ('global:procedure:verb', True),  # missing context
        ('project:procedure:verb:ctx', True),  # bare project — no id
        ('global:foo:procedure:verb:ctx', True),  # global with sub-id
        # NOT procedure-shaped — looks_like = False so writes pass through
        # as plain KV entries without invoking the procedure validator.
        ('global:lang:python', False),
        ('user:editor', False),
        ('project:memex:vault', False),
        ('procedure:commit:lint', False),  # bare — wrong namespace
        ('foo:procedure:verb:ctx', False),  # unknown namespace
        ('', False),
    ],
)
def test_looks_like_procedure_key_routing(key: str, expected: bool) -> None:
    """Pins the routing decision: only keys under a recognized scope WITH
    `:procedure:` infix are routed through `validate_procedure_key`.
    Malformed-but-procedure-shaped keys are still routed there (and rejected)
    rather than silently stored as plain KV."""
    assert _looks_like_procedure_key(key) is expected
