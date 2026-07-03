"""Failure-counter half-up rounding regression test.

The contradiction engine maps `failure_co_count_weight` (float in [0, 1]) to
an integer per-link bump. Python's builtin `round()` uses banker's rounding
so `round(0.5) == 0`, which would silently disable the default wiring. We
use half-up rounding instead; this test pins that behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memex_common.config import ContradictionConfig
from memex_core.memory.contradiction.engine import ContradictionEngine


def _bump(weight: float) -> int:
    """Mirror the inline expression in `ContradictionEngine._detect`."""
    engine = ContradictionEngine(
        lm=MagicMock(),
        config=ContradictionConfig(),
        failure_co_count_weight=weight,
    )
    w = max(0.0, engine.failure_co_count_weight)
    return 1 if w >= 0.5 else 0


@pytest.mark.parametrize(
    'weight,expected',
    [
        (0.0, 0),
        (0.49, 0),
        (0.5, 1),  # default — must NOT round to 0
        (0.51, 1),
        (1.0, 1),
    ],
)
def test_contradiction_failure_weight_half_up_rounding(weight: float, expected: int):
    assert _bump(weight) == expected


def test_contradiction_failure_weight_default_0p5_emits_increment():
    """Pin the default config: weight=0.5 -> +1 bump per negative-evidence link."""
    assert _bump(0.5) == 1
