"""Unit tests for the inbox-router decision policy (pure logic, no DB)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_core.services.inbox_router.decisions import (
    CandidateScore,
    DecisionKind,
    DecisionThresholds,
    RoutingState,
    decide,
)

WARM = DecisionThresholds(
    auto_apply_enabled=True, auto_apply_min_p_match=0.5, t_margin=0.4, t_low=0.10
)


def _cand(name: str, p: float, raw: float = 0.9, ci: float = 0.02) -> CandidateScore:
    return CandidateScore(
        vault_id=uuid4(), vault_name=name, p_match=p, p_match_raw=raw, ci_half_width=ci
    )


def test_auto_route_when_confident_and_warm():
    cands = [_cand('a', 0.85), _cand('b', 0.10)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=True)
    assert d.kind is DecisionKind.AUTO_ROUTE
    assert d.routing_state is RoutingState.WARMED_UP
    assert d.top.vault_name == 'a'
    assert d.margin == pytest.approx(0.75)


def test_cold_start_never_auto_routes_even_if_confident():
    cands = [_cand('a', 0.95), _cand('b', 0.02)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=False)
    assert d.kind is DecisionKind.PROPOSE_CANDIDATES
    assert d.routing_state is RoutingState.COLD_START


def test_disabled_auto_apply_proposes_only():
    th = DecisionThresholds(
        auto_apply_enabled=False, auto_apply_min_p_match=0.5, t_margin=0.4, t_low=0.10
    )
    d = decide(uuid4(), [_cand('a', 0.99), _cand('b', 0.0)], thresholds=th, warmed_up=True)
    assert d.kind is DecisionKind.PROPOSE_CANDIDATES
    assert d.routing_state is RoutingState.DISABLED


def test_low_margin_proposes_not_auto():
    # Top-1 clears p_match floor but top-2 is close → ambiguous → propose.
    cands = [_cand('a', 0.55), _cand('b', 0.45)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=True)
    assert d.kind is DecisionKind.PROPOSE_CANDIDATES
    assert d.margin == pytest.approx(0.10)


def test_below_p_match_floor_proposes_not_auto():
    # Margin is wide but the absolute p_match is below the auto floor.
    cands = [_cand('a', 0.45), _cand('b', 0.01)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=True)
    assert d.kind is DecisionKind.PROPOSE_CANDIDATES


def test_no_fit_when_best_raw_below_t_low():
    cands = [_cand('a', 0.9, raw=0.05), _cand('b', 0.1, raw=0.02)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=True)
    assert d.kind is DecisionKind.PROPOSE_NO_FIT


def test_no_candidates_is_no_fit():
    d = decide(uuid4(), [], thresholds=WARM, warmed_up=True)
    assert d.kind is DecisionKind.PROPOSE_NO_FIT
    assert d.top is None
    assert d.margin == 0.0


def test_single_candidate_margin_is_full_p_match():
    d = decide(uuid4(), [_cand('a', 0.8)], thresholds=WARM, warmed_up=True)
    assert d.margin == pytest.approx(0.8)
    assert d.kind is DecisionKind.AUTO_ROUTE


def test_candidates_are_sorted_descending():
    cands = [_cand('low', 0.2), _cand('high', 0.7), _cand('mid', 0.4)]
    d = decide(uuid4(), cands, thresholds=WARM, warmed_up=True)
    names = [c.vault_name for c in d.candidates]
    assert names == ['high', 'mid', 'low']
