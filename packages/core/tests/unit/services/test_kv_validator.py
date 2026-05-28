"""Unit tests for F14 ``validate_procedure_key`` (TC-F14-1).

Locks the strict ``procedure:<verb>:<context-tag>`` format against
regression. Per RFC-007 §53-61: regex ``^procedure:[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$``,
underscores ARE allowed (RFC-007 is canonical; an earlier RFC-012 sketch
without underscore was superseded).
"""

from __future__ import annotations

import pytest

from memex_core.services.kv import (
    VALID_NAMESPACES,
    format_procedure_display_name,
    is_procedure_key,
    parse_procedure_key,
    validate_procedure_key,
)


def test_valid_namespaces_includes_procedure():
    """``procedure`` is part of the canonical namespace tuple."""
    assert 'procedure' in VALID_NAMESPACES


@pytest.mark.parametrize(
    'valid_key',
    [
        'procedure:write_pr:commit-style',
        'procedure:run_tests:python-monorepo',
        'procedure:edit_yaml:ci-config',
        'procedure:add-note:vault-binding',
        'procedure:reflect_on:f5-debug',
    ],
)
def test_validate_procedure_key_accepts_valid(valid_key: str) -> None:
    """RFC-007 §53-61 valid examples (underscores + hyphens, lowercase only)."""
    validate_procedure_key(valid_key)  # must not raise


@pytest.mark.parametrize(
    'malformed_key',
    [
        'foo:bar:baz',  # missing 'procedure:' prefix
        'procedure:verb',  # missing context-tag
        'procedure:verb:',  # trailing-colon empty context-tag
        'procedure::tag',  # empty verb
        'procedure:Verb:tag',  # uppercase verb
        'procedure:verb:Tag',  # uppercase context-tag
        'procedure:verb:tag with space',  # whitespace (regex char class disallows)
        'procedure:-verb:tag',  # leading hyphen on verb (anchor [a-z])
        'procedure:verb:-tag',  # leading hyphen on context-tag (anchor [a-z])
        'procedure:verb:tag:extra',  # triple-segment
        '',  # empty string
        'procedure:',  # single-colon
        'procedure:verb:9tag',  # leading digit on context-tag (anchor [a-z])
    ],
)
def test_validate_procedure_key_rejects_malformed(malformed_key: str) -> None:
    """RFC-007 §53-61 invalid examples — must raise ValueError."""
    with pytest.raises(ValueError, match='Invalid procedure key'):
        validate_procedure_key(malformed_key)


# ---------------------------------------------------------------------------
# Project-scoped procedure keys: `project:<id>:procedure:<verb>:<context>`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'valid_key,expected_project_id',
    [
        # Simple identifier
        ('project:memex:procedure:commit:pr-workflow', 'memex'),
        # HTTPS-form git remote (slashes + dots in project_id)
        (
            'project:github.com/JasperHG90/memex:procedure:commit:pr-workflow',
            'github.com/JasperHG90/memex',
        ),
        # SSH-form git remote (contains @ and embedded :)
        (
            'project:git@github.com:acme/foo:procedure:run_tests:python',
            'git@github.com:acme/foo',
        ),
        # Uppercase repo (the project_id segment is permissive)
        (
            'project:github.com/Acme/Repo:procedure:lint:strict',
            'github.com/Acme/Repo',
        ),
        # Path-fallback form (leading slash, embedded slashes)
        ('project:/home/me/work/foo:procedure:deploy:gitops', '/home/me/work/foo'),
    ],
)
def test_validate_project_procedure_key_accepts_valid(
    valid_key: str, expected_project_id: str
) -> None:
    validate_procedure_key(valid_key)  # must not raise
    parsed = parse_procedure_key(valid_key)
    assert parsed is not None
    project_id, _, _ = parsed
    assert project_id == expected_project_id


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
        # project_id contains ':procedure:' substring (ambiguous; refuse)
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
    assert parse_procedure_key('procedure:commit:pr') == (None, 'commit', 'pr')


def test_parse_procedure_key_returns_none_for_non_procedure() -> None:
    assert parse_procedure_key('user:editor') is None
    assert parse_procedure_key('project:memex:vault') is None
    assert parse_procedure_key('') is None


def test_is_procedure_key_both_forms() -> None:
    assert is_procedure_key('procedure:commit:pr')
    assert is_procedure_key('project:memex:procedure:commit:pr')
    assert not is_procedure_key('user:editor')
    assert not is_procedure_key('project:memex:vault')


def test_format_procedure_display_name_global() -> None:
    assert format_procedure_display_name('procedure:commit:pr') == 'commit:pr'


def test_format_procedure_display_name_project() -> None:
    assert (
        format_procedure_display_name('project:memex:procedure:commit:pr-workflow')
        == '[project:memex] commit:pr-workflow'
    )


def test_format_procedure_display_name_passthrough_for_non_procedure() -> None:
    assert format_procedure_display_name('user:editor') == 'user:editor'
