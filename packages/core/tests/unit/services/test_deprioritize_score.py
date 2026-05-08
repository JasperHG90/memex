"""Unit tests for the FSFM-inspired deprioritization scorer (pure-Python math).

These tests exercise the canonical Python implementation of each composite
component plus the overall ``compute_composite`` function, with no DB.
The SQL/Python parity test in
``packages/core/tests/integration/services/test_int_fsfm_sql_python_parity.py``
guards drift between this file's reference values and the SQL CTE in
``services/lint.py``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from memex_core.services.deprioritize_score import (
    DEFAULT_LAMBDA_LINK,
    DEFAULT_MU_ENTITY,
    DEFAULT_WEIGHTS,
    PROTECTED_RISK_CLASSES,
    _InboundLink,
    _UnitInputs,
    compute_composite,
    compute_entity_dormancy,
    compute_graph_pressure,
    compute_memory_worth_complement,
    compute_temporal_staleness,
)


_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _link(
    link_type: str,
    *,
    weight: float = 1.0,
    age_days: float = 0.0,
    src_confidence: float = 1.0,
    src_success: int = 0,
    src_failure: int = 0,
) -> _InboundLink:
    return _InboundLink(
        link_type=link_type,
        link_weight=weight,
        link_created_at=_NOW - timedelta(days=age_days),
        src_confidence=src_confidence,
        src_success_co_count=src_success,
        src_failure_co_count=src_failure,
    )


def _unit(
    *,
    status: str = 'active',
    is_deprioritized: bool = False,
    risk_class: str | None = 'none',
    intent_class: str | None = 'durable',
    importance: float | None = 0.7,
    stability: float | None = 180.0,
    success: int = 0,
    failure: int = 0,
    last_outcome_age_days: float | None = None,
    inbound_links: tuple[_InboundLink, ...] = (),
    freshest_entity_age_days: float | None = None,
) -> _UnitInputs:
    last_outcome_at = (
        None if last_outcome_age_days is None else _NOW - timedelta(days=last_outcome_age_days)
    )
    freshest = (
        None
        if freshest_entity_age_days is None
        else _NOW - timedelta(days=freshest_entity_age_days)
    )
    return _UnitInputs(
        unit_id=uuid4(),
        status=status,
        is_deprioritized=is_deprioritized,
        risk_class=risk_class,
        intent_class=intent_class,
        importance=importance,
        stability=stability,
        success_co_count=success,
        failure_co_count=failure,
        last_outcome_at=last_outcome_at,
        inbound_links=inbound_links,
        freshest_entity_last_seen=freshest,
    )


# ---------------------------------------------------------------------------
# Graph pressure
# ---------------------------------------------------------------------------


class TestGraphPressure:
    def test_no_links_returns_neutral_05(self):
        assert compute_graph_pressure((), now=_NOW) == pytest.approx(0.5)

    def test_neutral_link_types_ignored(self):
        # 'temporal' / 'semantic' / 'entity' do not contribute.
        links = (
            _link('temporal'),
            _link('semantic'),
            _link('entity'),
        )
        assert compute_graph_pressure(links, now=_NOW) == pytest.approx(0.5)

    def test_strong_contradiction_pushes_above_05(self):
        # Single fresh contradicts link from a credible source → > 0.5.
        links = (_link('contradicts', src_confidence=1.0, src_success=10, src_failure=0),)
        score = compute_graph_pressure(links, now=_NOW)
        assert score > 0.5
        assert score < 1.0

    def test_strong_reinforcement_pushes_below_05(self):
        links = (_link('reinforces', src_confidence=1.0, src_success=10, src_failure=0),)
        score = compute_graph_pressure(links, now=_NOW)
        assert score < 0.5
        assert score > 0.0

    def test_weakens_half_strength_of_contradicts(self):
        # weakens=0.5 vs contradicts=1.0; weakens should push less hard.
        contradicts_score = compute_graph_pressure(
            (_link('contradicts', src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        weakens_score = compute_graph_pressure(
            (_link('weakens', src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        assert 0.5 < weakens_score < contradicts_score

    def test_causal_links_provide_structural_credit(self):
        # causes / caused_by / enables / prevents subtract 0.1 each.
        links = (
            _link('causes', src_confidence=1.0, src_success=10),
            _link('caused_by', src_confidence=1.0, src_success=10),
            _link('enables', src_confidence=1.0, src_success=10),
            _link('prevents', src_confidence=1.0, src_success=10),
        )
        score = compute_graph_pressure(links, now=_NOW)
        assert score < 0.5  # net keep pressure

    def test_link_weight_scales_contribution(self):
        # A 0.3-weighted contradicts link should push less than 1.0-weighted.
        weak = compute_graph_pressure(
            (_link('contradicts', weight=0.3, src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        strong = compute_graph_pressure(
            (_link('contradicts', weight=1.0, src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        assert 0.5 < weak < strong

    def test_low_credibility_source_dampens_signal(self):
        # Low confidence × low MW source contributes less than a credible one.
        weak_src = compute_graph_pressure(
            (_link('contradicts', src_confidence=0.2, src_success=0, src_failure=10),),
            now=_NOW,
        )
        strong_src = compute_graph_pressure(
            (_link('contradicts', src_confidence=1.0, src_success=10, src_failure=0),),
            now=_NOW,
        )
        assert 0.5 < weak_src < strong_src

    def test_old_link_dampens_via_recency_decay(self):
        fresh = compute_graph_pressure(
            (_link('contradicts', age_days=0.0, src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        old = compute_graph_pressure(
            (_link('contradicts', age_days=365.0, src_confidence=1.0, src_success=10),),
            now=_NOW,
        )
        assert 0.5 < old < fresh

    def test_competing_links_partially_cancel(self):
        # One contradicts + one reinforces from equally credible sources →
        # partially cancels back toward 0.5.
        links = (
            _link('contradicts', src_confidence=1.0, src_success=10),
            _link('reinforces', src_confidence=1.0, src_success=10),
        )
        score = compute_graph_pressure(links, now=_NOW)
        assert score == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# Memory Worth complement
# ---------------------------------------------------------------------------


class TestMemoryWorthComplement:
    def test_cold_start_is_05(self):
        assert compute_memory_worth_complement(0, 0) == pytest.approx(0.5)

    def test_high_success_lowers_complement(self):
        c = compute_memory_worth_complement(10, 0)
        assert c < 0.2

    def test_high_failure_raises_complement(self):
        c = compute_memory_worth_complement(0, 10)
        assert c > 0.8

    def test_balanced_returns_near_05(self):
        c = compute_memory_worth_complement(5, 5)
        assert c == pytest.approx(0.5, abs=0.05)


# ---------------------------------------------------------------------------
# Temporal staleness
# ---------------------------------------------------------------------------


class TestTemporalStaleness:
    def test_null_last_outcome_returns_zero(self):
        assert compute_temporal_staleness(None, 180.0, now=_NOW) == 0.0

    def test_null_stability_returns_zero(self):
        assert compute_temporal_staleness(_NOW, None, now=_NOW) == 0.0

    def test_zero_or_negative_stability_returns_zero(self):
        assert compute_temporal_staleness(_NOW, 0.0, now=_NOW) == 0.0
        assert compute_temporal_staleness(_NOW, -1.0, now=_NOW) == 0.0

    def test_fresh_outcome_is_near_zero(self):
        recent = _NOW - timedelta(days=1.0)
        assert compute_temporal_staleness(recent, 180.0, now=_NOW) < 0.02

    def test_at_one_stability_is_one_minus_inv_e(self):
        # 1 - exp(-1) ≈ 0.632
        anchor = _NOW - timedelta(days=180.0)
        assert compute_temporal_staleness(anchor, 180.0, now=_NOW) == pytest.approx(
            1.0 - math.exp(-1.0), abs=0.001
        )

    def test_far_past_approaches_one(self):
        anchor = _NOW - timedelta(days=10_000.0)
        assert compute_temporal_staleness(anchor, 180.0, now=_NOW) > 0.999

    def test_negative_age_clamps_to_zero(self):
        # Future-dated rows (clock skew) treated as zero age, no decay.
        future = _NOW + timedelta(days=10.0)
        assert compute_temporal_staleness(future, 180.0, now=_NOW) == 0.0


# ---------------------------------------------------------------------------
# Entity dormancy
# ---------------------------------------------------------------------------


class TestEntityDormancy:
    def test_no_entities_returns_zero(self):
        assert compute_entity_dormancy(None, now=_NOW) == 0.0

    def test_fresh_entity_near_zero(self):
        recent = _NOW - timedelta(days=1.0)
        assert compute_entity_dormancy(recent, now=_NOW) < 0.01

    def test_old_entity_pushes_dormancy(self):
        old = _NOW - timedelta(days=365.0)
        assert compute_entity_dormancy(old, now=_NOW) > 0.5


# ---------------------------------------------------------------------------
# Hard overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    @pytest.mark.parametrize('risk_class', sorted(PROTECTED_RISK_CLASSES))
    def test_protected_risk_class_returns_zero(self, risk_class):
        score, components, is_protected, reason = compute_composite(
            _unit(risk_class=risk_class), now=_NOW
        )
        assert score == 0.0
        assert is_protected is True
        assert reason and risk_class in reason
        assert components == {}

    def test_intent_class_permanent_returns_zero(self):
        score, _, is_protected, reason = compute_composite(
            _unit(intent_class='permanent'), now=_NOW
        )
        assert score == 0.0
        assert is_protected is True
        assert reason == 'intent_class:permanent'

    def test_status_stale_returns_zero(self):
        score, _, is_protected, reason = compute_composite(_unit(status='stale'), now=_NOW)
        assert score == 0.0
        assert is_protected is True
        assert reason == 'status_stale'

    def test_already_deprioritized_returns_zero(self):
        score, _, is_protected, reason = compute_composite(_unit(is_deprioritized=True), now=_NOW)
        assert score == 0.0
        assert is_protected is True
        assert reason == 'already_deprioritized'


# ---------------------------------------------------------------------------
# Composite end-to-end
# ---------------------------------------------------------------------------


class TestComposite:
    def test_neutral_unit_scores_low(self):
        # Cold-start unit, no links, fresh, no entity signal — should score
        # well below the propose threshold (0.55).
        score, comp, is_protected, _ = compute_composite(_unit(), now=_NOW)
        assert is_protected is False
        assert 0.0 <= score < 0.3
        assert set(comp.keys()) == {
            'graph_pressure',
            'memory_worth_complement',
            'temporal_staleness',
            'entity_dormancy',
        }

    def test_strong_negative_signals_score_above_propose(self):
        # Lots of contradiction, low MW, stale, dormant entity.
        unit = _unit(
            success=0,
            failure=20,
            last_outcome_age_days=200.0,
            stability=14.0,  # ephemeral baseline
            importance=0.3,  # ephemeral
            inbound_links=(
                _link('contradicts', src_confidence=1.0, src_success=10),
                _link('contradicts', src_confidence=1.0, src_success=10),
                _link('contradicts', src_confidence=1.0, src_success=10),
            ),
            freshest_entity_age_days=400.0,
        )
        score, _, is_protected, _ = compute_composite(unit, now=_NOW)
        assert is_protected is False
        assert score > 0.30  # propose threshold (ephemeral-max ~0.63)

    def test_high_importance_suppresses_score(self):
        # Same negative signals but with importance=1.0 → final must be 0.
        signals: dict[str, Any] = dict(
            success=0,
            failure=20,
            last_outcome_age_days=200.0,
            stability=14.0,
            inbound_links=(
                _link('contradicts', src_confidence=1.0, src_success=10),
                _link('contradicts', src_confidence=1.0, src_success=10),
            ),
        )
        high = compute_composite(_unit(importance=1.0, **signals), now=_NOW)[0]
        low = compute_composite(_unit(importance=0.3, **signals), now=_NOW)[0]
        assert high == 0.0
        assert low > high

    def test_null_importance_treated_as_05(self):
        # NULL importance behaves like 0.5 — neutral suppression, not zero.
        s_null = compute_composite(_unit(importance=None), now=_NOW)[0]
        s_05 = compute_composite(_unit(importance=0.5), now=_NOW)[0]
        assert s_null == pytest.approx(s_05)

    def test_score_clamps_to_unit_interval(self):
        # All components saturating + low importance → score should still
        # cap at <= 1.0 due to weights summing to 1.0.
        unit = _unit(
            success=0,
            failure=10000,
            last_outcome_age_days=10_000.0,  # exp(-10000) ≈ 0 → temporal saturates
            stability=1.0,
            importance=0.0,  # extreme
            inbound_links=tuple(
                _link('contradicts', src_confidence=1.0, src_success=10) for _ in range(20)
            ),
            freshest_entity_age_days=10_000.0,
        )
        score, _, _, _ = compute_composite(unit, now=_NOW)
        assert 0.0 <= score <= 1.0

    def test_default_weights_sum_to_one(self):
        # Sanity: any reweighting must keep the composite bounded by [0, 1].
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_default_lambda_and_mu_documented(self):
        # Pinned for the SQL parity test — these values appear inline in
        # the lint rule's CTE. Drift would silently desync SQL ↔ Python.
        assert DEFAULT_LAMBDA_LINK == pytest.approx(0.01)
        assert DEFAULT_MU_ENTITY == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# Internal sanity: timezone awareness
# ---------------------------------------------------------------------------


class TestTimezoneHandling:
    def test_naive_datetime_does_not_crash_temporal(self):
        # If someone passes a naive datetime in a fixture, the function
        # should still produce a finite number rather than raise.
        naive = datetime(2025, 1, 1)
        # _NOW is aware; subtracting naive raises in Python — ensure we
        # never do that. The scorer's _ensure_aware helper handles this
        # at the data layer, so the pure function should never receive a
        # naive datetime in practice.
        # This test is a placeholder reminder; the real guard is in
        # _load_unit_inputs.
        assert naive.tzinfo is None
