import math
import random

import pytest
from pydantic import ValidationError

from memex_common.config import RetrievalConfig
from memex_core.memory.retrieval.engine import _compose_boosts_logspace


def _compose(
    ce: float,
    *,
    recency: float = 1.0,
    temporal: float = 1.0,
    mw: float = 1.0,
    confidence: float = 1.0,
    decay: float = 1.0,
    log_clip: float = math.inf,
) -> float:
    return _compose_boosts_logspace(
        ce,
        recency=recency,
        temporal=temporal,
        mw=mw,
        confidence=confidence,
        decay=decay,
        log_clip=log_clip,
    )


class TestIdentityAtInfiniteClip:
    def test_neutral_boosts_return_ce_score(self):
        assert _compose(0.85) == pytest.approx(0.85, rel=1e-9)

    def test_random_boosts_match_multiplicative_product_at_inf(self):
        rng = random.Random(0xC0FFEE)
        for _ in range(64):
            ce = rng.uniform(0.05, 0.95)
            boosts = {
                'recency': rng.uniform(0.5, 1.5),
                'temporal': rng.uniform(0.5, 1.5),
                'mw': rng.uniform(0.5, 1.5),
                'confidence': rng.uniform(0.5, 1.5),
                'decay': rng.uniform(0.5, 1.5),
            }
            expected = ce
            for b in boosts.values():
                expected *= b
            actual = _compose(ce, log_clip=math.inf, **boosts)
            assert actual == pytest.approx(expected, rel=1e-9)

    def test_extreme_low_boosts_match_at_inf_modulo_floor(self):
        ce = 0.7
        expected = ce * (0.5**5)
        actual = _compose(ce, recency=0.5, temporal=0.5, mw=0.5, confidence=0.5, decay=0.5)
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_extreme_high_boosts_match_at_inf(self):
        ce = 0.4
        expected = ce * (2.0**5)
        actual = _compose(ce, recency=2.0, temporal=2.0, mw=2.0, confidence=2.0, decay=2.0)
        assert actual == pytest.approx(expected, rel=1e-9)


class TestFiniteClipBoundsAggregate:
    def test_product_exceeding_exp_L_is_clipped_to_exp_L(self):
        ce = 0.5
        L = 0.7
        actual = _compose(
            ce, recency=2.0, temporal=2.0, mw=2.0, confidence=2.0, decay=2.0, log_clip=L
        )
        assert actual == pytest.approx(ce * math.exp(L), rel=1e-12)

    def test_product_below_exp_negL_is_clipped_to_exp_negL(self):
        ce = 0.5
        L = 0.7
        actual = _compose(
            ce, recency=0.5, temporal=0.5, mw=0.5, confidence=0.5, decay=0.5, log_clip=L
        )
        assert actual == pytest.approx(ce * math.exp(-L), rel=1e-12)

    def test_product_inside_band_passes_through_unchanged(self):
        ce = 0.5
        L = 1.5
        log_product = 5 * math.log(0.95)
        assert abs(log_product) < L
        actual = _compose(
            ce, recency=0.95, temporal=0.95, mw=0.95, confidence=0.95, decay=0.95, log_clip=L
        )
        expected = ce * (0.95**5)
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_clip_at_zero_collapses_metadata_to_identity(self):
        ce = 0.6
        for boosts in [
            {'recency': 2.0, 'temporal': 0.5, 'mw': 1.5, 'confidence': 0.8, 'decay': 1.2},
            {'recency': 0.1, 'temporal': 0.1, 'mw': 0.1, 'confidence': 0.1, 'decay': 0.1},
            {'recency': 10.0, 'temporal': 10.0, 'mw': 10.0, 'confidence': 10.0, 'decay': 10.0},
        ]:
            actual = _compose(ce, log_clip=0.0, **boosts)
            assert actual == pytest.approx(ce, rel=1e-12), f'failed for {boosts}'


class TestZeroAndNegativeBoosts:
    def test_zero_boost_does_not_nan(self):
        actual = _compose(
            0.5, recency=0.0, temporal=1.0, mw=1.0, confidence=1.0, decay=1.0, log_clip=math.inf
        )
        assert math.isfinite(actual)
        assert actual >= 0.0

    def test_zero_boost_with_finite_clip_lands_at_lower_bound(self):
        ce = 0.5
        L = 0.7
        actual = _compose(
            ce, recency=0.0, temporal=1.0, mw=1.0, confidence=1.0, decay=1.0, log_clip=L
        )
        assert actual == pytest.approx(ce * math.exp(-L), rel=1e-12)

    def test_negative_boost_treated_as_floor(self):
        actual_neg = _compose(
            0.5,
            recency=-0.5,
            temporal=1.0,
            mw=1.0,
            confidence=1.0,
            decay=1.0,
            log_clip=math.inf,
        )
        actual_zero = _compose(
            0.5,
            recency=0.0,
            temporal=1.0,
            mw=1.0,
            confidence=1.0,
            decay=1.0,
            log_clip=math.inf,
        )
        assert actual_neg == pytest.approx(actual_zero, rel=1e-12)


class TestNaNGuard:
    def test_nan_boost_returns_ce_score_untouched(self):
        result = _compose(0.7, recency=math.nan, temporal=1.0, mw=1.0, confidence=1.0, decay=1.0)
        assert result == pytest.approx(0.7, rel=1e-12)

    def test_nan_ce_score_propagates_as_nan(self):
        result = _compose(math.nan, recency=1.0, temporal=1.0, mw=1.0, confidence=1.0, decay=1.0)
        assert math.isnan(result)

    def test_nan_log_clip_returns_ce_score(self):
        result = _compose(
            0.5, recency=1.0, temporal=1.0, mw=1.0, confidence=1.0, decay=1.0, log_clip=math.nan
        )
        assert result == pytest.approx(0.5, rel=1e-12)


class TestConfigValidator:
    def test_default_is_inf(self):
        config = RetrievalConfig()
        assert config.composite_boost_log_clip == math.inf

    def test_explicit_inf_accepted(self):
        config = RetrievalConfig(composite_boost_log_clip=math.inf)
        assert config.composite_boost_log_clip == math.inf

    def test_zero_accepted(self):
        config = RetrievalConfig(composite_boost_log_clip=0.0)
        assert config.composite_boost_log_clip == 0.0

    def test_finite_positive_accepted(self):
        config = RetrievalConfig(composite_boost_log_clip=0.7)
        assert config.composite_boost_log_clip == 0.7

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            RetrievalConfig(composite_boost_log_clip=-0.5)

    def test_negative_inf_rejected(self):
        with pytest.raises(ValidationError):
            RetrievalConfig(composite_boost_log_clip=-math.inf)


class TestRankPreservationAtInfiniteClip:
    def test_ranking_preserved_for_diverse_units(self):
        rng = random.Random(0x1234)
        units = []
        for _ in range(20):
            units.append(
                {
                    'ce': rng.uniform(0.05, 0.95),
                    'recency': rng.uniform(0.5, 1.5),
                    'temporal': rng.uniform(0.5, 1.5),
                    'mw': rng.uniform(0.5, 1.5),
                    'confidence': rng.uniform(0.5, 1.5),
                    'decay': rng.uniform(0.5, 1.5),
                }
            )

        def product_score(u: dict) -> float:
            return u['ce'] * u['recency'] * u['temporal'] * u['mw'] * u['confidence'] * u['decay']

        def logspace_score(u: dict) -> float:
            return _compose(
                u['ce'],
                recency=u['recency'],
                temporal=u['temporal'],
                mw=u['mw'],
                confidence=u['confidence'],
                decay=u['decay'],
                log_clip=math.inf,
            )

        product_order = sorted(
            range(len(units)), key=lambda i: product_score(units[i]), reverse=True
        )
        logspace_order = sorted(
            range(len(units)), key=lambda i: logspace_score(units[i]), reverse=True
        )
        assert product_order == logspace_order
