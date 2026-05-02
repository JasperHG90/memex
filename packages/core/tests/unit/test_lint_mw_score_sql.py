"""Drift guard between the inline MW-score SQL and the Python implementation.

The F6 ``cold_low_mw_unit`` rule embeds the Beta-Bernoulli posterior-mean
formula directly in SQL (so the database can apply the predicate without
a round-trip). That formula MUST match
:func:`memex_core.services.outcomes.compute_mw_score`. If either changes
without the other, this test fails.

Implementation strategy: parse the inline SQL expression (which is a
fixed shape — `(s + 1.0) / (s + f + 2)`), substitute integers for the
column names, and compare the result against the Python function for a
small grid of (s, f) pairs.
"""

from __future__ import annotations

import re

import pytest

from memex_core.services.lint import _MW_SCORE_EXPR
from memex_core.services.outcomes import compute_mw_score


def _evaluate_sql_expr(expr: str, s: int, f: int) -> float:
    """Evaluate the inline MW-score SQL expression in Python.

    The expression uses the column names ``success_co_count`` and
    ``failure_co_count``; we substitute them with literal integers and
    eval safely (the expression is from our own source, not user input).
    """
    py = expr.replace('success_co_count', str(s)).replace('failure_co_count', str(f))
    # Allow only digits, parens, +-*/, dots and spaces.
    assert re.fullmatch(r'[\d\s\(\)\+\-\*\/\.\,]+', py), (
        f'Unexpected characters in MW-score SQL expression after substitution: {py!r}'
    )
    return float(eval(py))  # noqa: S307 — expression is from our own source code


@pytest.mark.parametrize(
    's, f',
    [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 5),
        (5, 1),
        (3, 3),
        (10, 0),
        (0, 10),
        (8, 7),
    ],
)
def test_inline_mw_score_sql_matches_python(s: int, f: int) -> None:
    """For every (s, f) pair, SQL expression == compute_mw_score(s, f)."""
    sql_value = _evaluate_sql_expr(_MW_SCORE_EXPR, s, f)
    py_value = compute_mw_score(s, f)
    assert sql_value == pytest.approx(py_value, rel=1e-12, abs=1e-12), (
        f'Drift detected at s={s}, f={f}: SQL={sql_value}, Python={py_value}'
    )
