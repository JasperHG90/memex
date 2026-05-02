"""TC-24-2 (trimmed): FSRS-5 adapter smoke parity.

py-fsrs IS the parity baseline now (the verification report at
`paper-cross-check.md` established that py-fsrs 4.1.2 implements FSRS-5
bit-exact); the full 75-case parity sweep lives in the POC. Production
just needs a small smoke set to detect adapter regressions — 5 cases
covering first-review for each rating + a subsequent-review smoke.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memex_core.memory.revisit import Quality, UnitState, schedule


_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    'quality, expected_stability, expected_difficulty',
    [
        (Quality.AGAIN, 0.40255, 7.1949),
        (Quality.HARD, 1.18385, 6.488305),
        (Quality.GOOD, 3.173, 5.282434),
        (Quality.EASY, 15.69105, 3.224502),
    ],
)
def test_first_review_initializes_with_fsrs5_defaults(
    quality: Quality,
    expected_stability: float,
    expected_difficulty: float,
) -> None:
    """First review (state=None) initializes stability/difficulty per the
    FSRS-5 19-tuple defaults from py-fsrs 4.1.2.

    These are NOT FSRS-4.5 values (17-tuple) — see paper-cross-check.md.
    Stability uses w[quality-1]; difficulty uses the FSRS-5 exponential
    init formula `w[4] - exp(w[5]*(quality-1)) + 1`, clamped to [1, 10].
    """
    next_due, interval, stability, difficulty = schedule(None, quality, _NOW)
    assert stability == pytest.approx(expected_stability, abs=1e-4)
    assert difficulty == pytest.approx(expected_difficulty, abs=1e-3)
    assert interval >= 1
    assert next_due > _NOW


def test_subsequent_review_advances_stability() -> None:
    """A second GOOD review on a unit with prior state advances stability monotonically."""
    _, _, init_stab, init_diff = schedule(None, Quality.GOOD, _NOW)

    later = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    state = UnitState(stability=init_stab, difficulty=init_diff, last_review=_NOW)
    _, _, new_stab, _new_diff = schedule(state, Quality.GOOD, later)

    assert new_stab > init_stab, 'subsequent GOOD review should grow stability'
