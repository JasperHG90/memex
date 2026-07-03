"""F11 — F40 SQL parameter-binding pinning tests.

Per BACKLOG.md F11 load-bearing note: every numeric the F40 SQL builder
substitutes must flow through ``bindparams(...)`` (asyncpg ``$N``
placeholder), never f-string interpolation. Pinning test: assert the
rendered SQL string is INDEPENDENT of ``STABILITY_SECONDS_PER_DAY``'s
value — i.e., the integer never appears textually in the rendered SQL.
Mutating the constant via monkeypatch must not change the rendered SQL.

The reason this is load-bearing: the same builder will, at the next
iteration, take values that ARE user-controlled (per-vault override,
per-class stability map). Routing some literals through string
interpolation while others go through parameter binding creates an
inconsistent surface where the wrong code path can leak. Convention:
every numeric is a bound parameter, regardless of provenance.
"""

from __future__ import annotations

import pytest

from memex_core.memory.retrieval import constants as _constants_mod
from memex_core.memory.retrieval import engine as _engine_mod
from memex_core.memory.retrieval.engine import _build_pre_filter_clause


@pytest.mark.parametrize(
    'replacement_value',
    [86400.0, 3600.0, 1.0, 999999.0],
)
def test_rendered_sql_is_independent_of_stability_seconds_per_day(
    monkeypatch: pytest.MonkeyPatch,
    replacement_value: float,
) -> None:
    """Mutating STABILITY_SECONDS_PER_DAY must NOT change the rendered SQL
    string. The constant lives in the parameter list, not the SQL text."""
    monkeypatch.setattr(_engine_mod, 'STABILITY_SECONDS_PER_DAY', replacement_value)
    monkeypatch.setattr(_constants_mod, 'STABILITY_SECONDS_PER_DAY', replacement_value)

    clause = _build_pre_filter_clause(apply_pre_filter=True, fsfm_branch_enabled=True)
    assert clause is not None
    sql = str(clause)

    assert ':stability_seconds_per_day' in sql, (
        'FSFM branch must reference STABILITY_SECONDS_PER_DAY via bound '
        'parameter (``:stability_seconds_per_day``), not f-string interpolation.'
    )
    if replacement_value not in (1.0, 0.0):
        assert str(int(replacement_value)) not in sql, (
            f'STABILITY_SECONDS_PER_DAY literal ({int(replacement_value)}) '
            f'leaked into the rendered SQL: {sql!r}. The constant must flow '
            f'through bindparams.'
        )


def test_two_renders_under_different_constants_are_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strongest form of the invariant: two renders under two
    different constant values produce IDENTICAL SQL strings."""
    monkeypatch.setattr(_engine_mod, 'STABILITY_SECONDS_PER_DAY', 86400.0)
    monkeypatch.setattr(_constants_mod, 'STABILITY_SECONDS_PER_DAY', 86400.0)
    sql_a = str(_build_pre_filter_clause(apply_pre_filter=True, fsfm_branch_enabled=True))

    monkeypatch.setattr(_engine_mod, 'STABILITY_SECONDS_PER_DAY', 999999.0)
    monkeypatch.setattr(_constants_mod, 'STABILITY_SECONDS_PER_DAY', 999999.0)
    sql_b = str(_build_pre_filter_clause(apply_pre_filter=True, fsfm_branch_enabled=True))

    assert sql_a == sql_b, (
        'Rendered SQL must be invariant under STABILITY_SECONDS_PER_DAY '
        f'mutation. Got:\n  a={sql_a!r}\n  b={sql_b!r}'
    )


def test_rendered_sql_is_independent_of_stability_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STABILITY_THRESHOLD must also be parameter-bound, not interpolated."""
    monkeypatch.setattr(_engine_mod, 'STABILITY_THRESHOLD', 0.10)
    monkeypatch.setattr(_constants_mod, 'STABILITY_THRESHOLD', 0.10)
    sql_a = str(_build_pre_filter_clause(apply_pre_filter=True, fsfm_branch_enabled=True))

    monkeypatch.setattr(_engine_mod, 'STABILITY_THRESHOLD', 0.99)
    monkeypatch.setattr(_constants_mod, 'STABILITY_THRESHOLD', 0.99)
    sql_b = str(_build_pre_filter_clause(apply_pre_filter=True, fsfm_branch_enabled=True))

    assert sql_a == sql_b, 'Rendered SQL must be invariant under STABILITY_THRESHOLD mutation.'
    assert ':stability_threshold' in sql_a
