"""F20 unit: 5-gate eligibility predicate (Python form).

TC-24-3: every gate is independent — flipping any one to its
non-eligible state must drop a unit out of the eligible set, even if
all four others are pass.

The SQL form is exercised separately at integration (it requires a
real Postgres for the `(s+1)/(s+f+2)` arithmetic on the actual MW
columns); this test locks the Python predicate against the same
spec.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from memex_core.services.revisitation import (
    CONFIDENCE_FLOOR_DEFAULT,
    MW_THRESHOLD_DEFAULT,
    is_eligible_for_review,
)


def _baseline_unit(**overrides):
    base = dict(
        intent_class='permanent',
        status='active',
        is_deprioritized=False,
        confidence=1.0,
        success_co_count=0,
        failure_co_count=0,
    )
    base.update(overrides)
    return NS(**base)


def test_baseline_eligible() -> None:
    """Cold-start permanent unit (mw=0.5) passes all 5 gates."""
    assert is_eligible_for_review(_baseline_unit()) is True


@pytest.mark.parametrize(
    'override, expected',
    [
        ({'intent_class': 'ephemeral'}, False),
        ({'intent_class': 'durable'}, True),
        ({'status': 'stale'}, False),
        ({'is_deprioritized': True}, False),
        ({'confidence': CONFIDENCE_FLOOR_DEFAULT - 0.01}, False),
        ({'confidence': CONFIDENCE_FLOOR_DEFAULT}, True),
        ({'success_co_count': 0, 'failure_co_count': 20}, False),
        ({'success_co_count': 20, 'failure_co_count': 0}, True),
    ],
)
def test_each_gate_is_independent(override: dict, expected: bool) -> None:
    """Each gate flip changes eligibility independently."""
    assert is_eligible_for_review(_baseline_unit(**override)) is expected


def test_thresholds_are_module_level_constants() -> None:
    """The default thresholds match RFC-014 §5-gate values."""
    assert CONFIDENCE_FLOOR_DEFAULT == 0.5
    assert MW_THRESHOLD_DEFAULT == 0.4
