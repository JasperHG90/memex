"""F11 — compute_decay_boost split-guard NULL-handling tests.

Each NULL input maps to a different semantic; the function MUST distinguish
them rather than collapse all three to a single early-return. Cases are
covered independently per the BACKLOG load-bearing test plan:

  (a) importance is None             -> 1.0 (regardless of other fields)
  (b) last_outcome_at is None        -> 1.0 (regardless of other fields)
  (c) stability is None, importance=1.0, last_outcome_at=now-30d, decay_alpha=0.3
      -> 1.15 (permanent unit importance-lifted, NOT decay-suppressed)
  (d) all three None                 -> 1.0 (covered by (a)+(b))
  (e) recently-touched durable       -> mild lift (~1.06)
  (f) 3-year-old ephemeral           -> ~0.85 penalty
  (g) decay_alpha = 0.0              -> exactly 1.0 for every above case (regression guard)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from memex_core.memory.retrieval.decay import compute_decay_boost


@dataclass
class _UnitStub:
    importance: float | None
    stability: float | None
    last_outcome_at: datetime | None


_NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)


class TestSplitGuardNullHandling:
    """Each NULL input maps to a *different* semantic — assert independently."""

    @pytest.mark.parametrize(
        ('stability', 'last_outcome_at'),
        [
            (None, None),
            (None, _NOW - timedelta(days=30)),
            (180.0, None),
            (180.0, _NOW - timedelta(days=30)),
        ],
    )
    def test_a_importance_none_returns_neutral(
        self, stability: float | None, last_outcome_at: datetime | None
    ) -> None:
        unit = _UnitStub(importance=None, stability=stability, last_outcome_at=last_outcome_at)
        assert compute_decay_boost(unit, decay_alpha=0.3, now=_NOW) == 1.0

    @pytest.mark.parametrize(
        ('importance', 'stability'),
        [
            (None, None),
            (1.0, None),
            (None, 180.0),
            (0.7, 180.0),
        ],
    )
    def test_b_last_outcome_at_none_returns_neutral(
        self, importance: float | None, stability: float | None
    ) -> None:
        unit = _UnitStub(importance=importance, stability=stability, last_outcome_at=None)
        assert compute_decay_boost(unit, decay_alpha=0.3, now=_NOW) == 1.0

    def test_c_permanent_unit_is_importance_lifted_not_decay_suppressed(self) -> None:
        """stability=None, importance=1.0, last_outcome_at=now-30d, decay_alpha=0.3
        → boost = 1.0 + 0.3 × (1.0 × 1.0 − 0.5) = 1.15."""
        unit = _UnitStub(
            importance=1.0,
            stability=None,
            last_outcome_at=_NOW - timedelta(days=30),
        )
        boost = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        assert math.isclose(boost, 1.15, abs_tol=1e-9)

    def test_d_all_three_none_returns_neutral(self) -> None:
        unit = _UnitStub(importance=None, stability=None, last_outcome_at=None)
        assert compute_decay_boost(unit, decay_alpha=0.3, now=_NOW) == 1.0


class TestActiveDecay:
    def test_e_recently_touched_durable_mild_lift(self) -> None:
        """importance=0.7, stability=180.0, last_outcome_at=now-7d, decay_alpha=0.3
        → decay_term = exp(-7/180) ≈ 0.9620
        → boost = 1.0 + 0.3 × (0.7 × 0.9620 − 0.5) ≈ 1.0520."""
        unit = _UnitStub(
            importance=0.7,
            stability=180.0,
            last_outcome_at=_NOW - timedelta(days=7),
        )
        boost = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        decay_term = math.exp(-7.0 / 180.0)
        expected = 1.0 + 0.3 * (0.7 * decay_term - 0.5)
        assert math.isclose(boost, expected, abs_tol=1e-9)
        assert 1.0 < boost < 1.10

    def test_f_three_year_old_ephemeral_penalty(self) -> None:
        """importance=0.3, stability=14.0, last_outcome_at=now-1095d, decay_alpha=0.3
        → decay_term = exp(-1095/14) ≈ 0 (deep decay)
        → boost = 1.0 + 0.3 × (0.3 × ~0 − 0.5) ≈ 0.85."""
        unit = _UnitStub(
            importance=0.3,
            stability=14.0,
            last_outcome_at=_NOW - timedelta(days=1095),
        )
        boost = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        decay_term = math.exp(-1095.0 / 14.0)
        expected = 1.0 + 0.3 * (0.3 * decay_term - 0.5)
        assert math.isclose(boost, expected, abs_tol=1e-9)
        assert math.isclose(boost, 0.85, abs_tol=1e-3)


class TestDecayAlphaZeroRegressionGuard:
    """decay_alpha = 0.0 must collapse every case (including active decay) to 1.0."""

    @pytest.mark.parametrize(
        'unit',
        [
            _UnitStub(importance=None, stability=None, last_outcome_at=None),
            _UnitStub(importance=1.0, stability=None, last_outcome_at=_NOW - timedelta(days=30)),
            _UnitStub(importance=0.7, stability=180.0, last_outcome_at=_NOW - timedelta(days=7)),
            _UnitStub(importance=0.3, stability=14.0, last_outcome_at=_NOW - timedelta(days=1095)),
            _UnitStub(importance=1.0, stability=180.0, last_outcome_at=_NOW),
        ],
    )
    def test_alpha_zero_yields_exactly_neutral(self, unit: _UnitStub) -> None:
        assert compute_decay_boost(unit, decay_alpha=0.0, now=_NOW) == 1.0


class TestNoSyntheticDefaults:
    """Guard against future regressions that substitute synthetic defaults
    for ``importance`` / ``last_outcome_at`` (forbidden per BACKLOG)."""

    def test_importance_none_with_active_other_fields_does_not_synthesize(self) -> None:
        """A unit with importance=None and a recent last_outcome_at must NOT be
        treated as importance=0.5 (or any other synthetic default). The
        early-return is the contract."""
        unit = _UnitStub(
            importance=None,
            stability=180.0,
            last_outcome_at=_NOW - timedelta(days=1),
        )
        assert compute_decay_boost(unit, decay_alpha=0.3, now=_NOW) == 1.0

    def test_last_outcome_at_none_does_not_synthesize_from_now(self) -> None:
        """A unit with last_outcome_at=None must NOT be silently treated as
        last_outcome_at = now() (which would yield decay_term=1.0 and a
        boost of 1.06 for a durable unit) — the early-return preserves
        cold-start neutrality."""
        unit = _UnitStub(
            importance=0.7,
            stability=180.0,
            last_outcome_at=None,
        )
        assert compute_decay_boost(unit, decay_alpha=0.3, now=_NOW) == 1.0


class TestInjectedNowDeterminism:
    """``now`` is a parameter — never datetime.now() inside the function.
    Tests can pin time without monkeypatching."""

    def test_two_calls_with_pinned_now_match(self) -> None:
        unit = _UnitStub(
            importance=0.7,
            stability=180.0,
            last_outcome_at=_NOW - timedelta(days=10),
        )
        b1 = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        b2 = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        assert b1 == b2

    def test_advancing_now_increases_decay(self) -> None:
        """All else equal, a later ``now`` deepens decay (boost shrinks)."""
        unit = _UnitStub(
            importance=0.7,
            stability=14.0,
            last_outcome_at=_NOW - timedelta(days=10),
        )
        b_now = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW)
        b_later = compute_decay_boost(unit, decay_alpha=0.3, now=_NOW + timedelta(days=30))
        assert b_later < b_now
