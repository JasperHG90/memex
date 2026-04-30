"""Unit tests for F14 ``validate_procedure_key`` (TC-F14-1).

Locks the strict ``procedure:<verb>:<context-tag>`` format against
regression. Per RFC-007 §53-61: regex ``^procedure:[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$``,
underscores ARE allowed (RFC-007 is canonical; an earlier RFC-012 sketch
without underscore was superseded).
"""

from __future__ import annotations

import pytest

from memex_core.services.kv import VALID_NAMESPACES, validate_procedure_key


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
