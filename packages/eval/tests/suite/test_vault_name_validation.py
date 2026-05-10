"""Unit tests for ``vault_name`` validation on ``SourceNote`` and
``Scenario``.

The value becomes a directory name under the snapshot cache's
``vaults/`` subdir, so it must be filesystem-safe AND must not collide
with the reserved ``_default`` logical name used for the primary
vault. These tests pin the boundary explicitly so a future loosening
of the regex doesn't silently reintroduce path-traversal / collision
hazards in the cache layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memex_eval.suite.base import GoldUnitIds, Scenario
from memex_eval.suite.sources import SourceNote


def _src(vault_name: str | None) -> SourceNote:
    return SourceNote(
        path=Path('/tmp/x.md'),
        note_key='x',
        content='c',
        vault_name=vault_name,
    )


def _scn(vault_name: str | None) -> Scenario:
    return Scenario(
        id='s',
        description='d',
        query='q',
        expected=GoldUnitIds(type='gold_unit_ids', note_keys=[]),
        vault_name=vault_name,
    )


@pytest.mark.parametrize(
    'good',
    ['bench-vault-a', 'bench_vault_a', 'a', 'v0', 'a0_b1-c2', None, ''],
)
def test_accepts_safe_vault_names(good: str | None) -> None:
    src = _src(good)
    scn = _scn(good)
    # Empty string and None both coerce to None — the populate layout
    # treats absent/None as the primary vault.
    expected = None if good in (None, '') else good
    assert src.vault_name == expected
    assert scn.vault_name == expected


@pytest.mark.parametrize('reserved', ['_default', '_DEFAULT', '_Default'])
def test_refuses_reserved_default_name(reserved: str) -> None:
    with pytest.raises(ValueError, match='reserved'):
        _src(reserved)
    with pytest.raises(ValueError, match='reserved'):
        _scn(reserved)


@pytest.mark.parametrize(
    'bad',
    [
        '..',  # path traversal
        '../escape',
        'a/b',  # slash → multi-level dir
        'a\\b',  # backslash
        '_starts_underscore',  # underscore-prefix is allowed only for reserved set
        'UPPER',  # uppercase
        '-leading-dash',
        '.dotfile',
        'with space',
        'special!char',
    ],
)
def test_refuses_unsafe_or_malformed_vault_names(bad: str) -> None:
    with pytest.raises(ValueError):
        _src(bad)
    with pytest.raises(ValueError):
        _scn(bad)
