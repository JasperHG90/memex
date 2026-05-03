"""Unit tests for MW exploration floor (F33) and edge exploration (F22)."""

from unittest.mock import patch
from uuid import uuid4

from memex_core.memory.confidence import MAX_VARIANCE
from memex_core.memory.confidence import mean_and_variance as _real_mean_and_variance
from memex_core.memory.retrieval.exploration import (
    DEFAULT_HIGH_VARIANCE_FRACTION,
    inject_edge_exploration,
    inject_exploration_units,
    select_edge_exploration_candidates,
    select_exploration_candidates,
)
from memex_core.memory.sql_models import ContentStatus, MemoryUnit


def _make_unit(
    success: int = 0,
    failure: int = 0,
    text: str = 'test',
    status: ContentStatus = ContentStatus.ACTIVE,
    is_deprioritized: bool = False,
    confidence: float = 1.0,
    confidence_evidence_count: int = 0,
) -> MemoryUnit:
    return MemoryUnit(
        id=uuid4(),
        text=text,
        fact_type='world',
        event_date=__import__('datetime').datetime.now(__import__('datetime').timezone.utc),
        vault_id=uuid4(),
        note_id=uuid4(),
        embedding=[],
        success_co_count=success,
        failure_co_count=failure,
        status=status,
        is_deprioritized=is_deprioritized,
        confidence=confidence,
        confidence_evidence_count=confidence_evidence_count,
    )


class TestSelectExplorationCandidates:
    """ε-greedy candidate selection."""

    def test_no_injection_when_epsilon_zero(self):
        results = [_make_unit()]
        candidates = [_make_unit(success=0, failure=0)]
        selected = select_exploration_candidates(results, candidates, epsilon=0.0)
        assert selected == []

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injection_when_random_below_epsilon(self, _mock):
        results = [_make_unit(success=10, failure=2)]
        # A cold-start unit that's NOT in results
        cold = _make_unit(success=0, failure=0)
        candidates = [results[0], cold]
        selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert len(selected) == 1
        assert selected[0].id == cold.id

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.99)
    def test_no_injection_when_random_above_epsilon(self, _mock):
        results = [_make_unit(success=10, failure=2)]
        cold = _make_unit(success=0, failure=0)
        candidates = [results[0], cold]
        selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_high_mw_units_excluded(self):
        results = [_make_unit()]
        # Unit with high outcome count (>5) should not be eligible
        high_mw = _make_unit(success=10, failure=5)
        candidates = [results[0], high_mw]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert len(selected) == 0

    def test_already_selected_units_excluded(self):
        unit = _make_unit(success=0, failure=0)
        results = [unit]
        candidates = [unit]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert len(selected) == 0

    def test_max_injections_limits_count(self):
        results = [_make_unit()]
        eligible = [_make_unit(success=0, failure=0) for _ in range(5)]
        candidates = [results[0]] + eligible
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_exploration_candidates(
                results, candidates, epsilon=0.05, max_injections=1
            )
        assert len(selected) <= 1

    def test_deprioritized_units_excluded(self):
        """Deprioritized units are not eligible for exploration injection (PR #91 MED-1)."""
        results = [_make_unit(success=10, failure=2)]
        deprioritized_cold = _make_unit(success=0, failure=0, is_deprioritized=True)
        candidates = [results[0], deprioritized_cold]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_stale_units_excluded(self):
        """Stale (superseded) units are not eligible for exploration injection (PR #91 MED-1)."""
        results = [_make_unit(success=10, failure=2)]
        stale_cold = _make_unit(success=0, failure=0, status=ContentStatus.STALE)
        candidates = [results[0], stale_cold]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_custom_low_mw_threshold(self):
        results = [_make_unit()]
        # Unit with 3 total outcomes — below default threshold of 5
        lowish = _make_unit(success=2, failure=1)
        candidates = [results[0], lowish]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            # threshold=2 → this unit has 3 outcomes → NOT eligible
            selected = select_exploration_candidates(
                results, candidates, epsilon=0.05, low_mw_threshold=2
            )
        assert len(selected) == 0


class TestInjectExplorationUnits:
    """Integration of exploration candidates into results."""

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.99)
    def test_no_injection_returns_original_list(self, _mock):
        results = [_make_unit()]
        candidates = [results[0]]
        output = inject_exploration_units(results, candidates)
        assert output is results

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injected_units_appended_to_end(self, _mock):
        results = [_make_unit(success=10, failure=2, text='regular')]
        cold = _make_unit(success=0, failure=0, text='exploration')
        candidates = [results[0], cold]
        output = inject_exploration_units(results, candidates)
        assert len(output) == 2
        assert output[-1].text == 'exploration'

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injected_units_marked_exploration(self, _mock):
        results = [_make_unit(success=10, failure=2)]
        cold = _make_unit(success=0, failure=0)
        candidates = [results[0], cold]
        output = inject_exploration_units(results, candidates)
        injected = output[-1]
        assert injected.unit_metadata.get('exploration') is True

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_regular_units_not_marked_exploration(self, _mock):
        results = [_make_unit(success=10, failure=2)]
        cold = _make_unit(success=0, failure=0)
        candidates = [results[0], cold]
        output = inject_exploration_units(results, candidates)
        # The first unit (regular) should NOT have exploration flag
        assert output[0].unit_metadata.get('exploration') is not True

    def test_empty_results_no_crash(self):
        output = inject_exploration_units([], [])
        assert output == []


# ---------------------------------------------------------------------------
# F22 — edge_exploration tests (Hermes round-1 HIGH).
#
# Mirrors the F33 test surface above for `select_edge_exploration_candidates`
# and `inject_edge_exploration`. Eligibility is keyed on closed-form Beta(1,1)
# posterior variance (not outcome count).
#
#   Cold-start (confidence_evidence_count=0)         → variance = 1/12 (MAX)
#   Well-evidenced (count >> 0, c > 0 or c < 1)      → variance ≪ 1/12
#
# DEFAULT_HIGH_VARIANCE_FRACTION = 0.5 → threshold = 0.5 * 1/12 ≈ 0.0417.
# ---------------------------------------------------------------------------


class TestSelectEdgeExplorationCandidates:
    """ε-greedy candidate selection keyed on confidence variance."""

    def test_no_injection_when_epsilon_zero(self):
        results = [_make_unit()]
        candidates = [_make_unit()]  # cold-start, max variance
        selected = select_edge_exploration_candidates(results, candidates, epsilon=0.0)
        assert selected == []

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injection_when_random_below_epsilon(self, _mock):
        # Already-selected: well-evidenced (low variance, NOT eligible).
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        # Candidate: cold-start (variance = MAX, ABOVE threshold) → eligible.
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0)
        candidates = [results[0], cold]
        selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert len(selected) == 1
        assert selected[0].id == cold.id

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.99)
    def test_no_injection_when_random_above_epsilon(self, _mock):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0)
        candidates = [results[0], cold]
        selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_low_variance_units_excluded(self):
        """Well-evidenced units (low variance) are NOT eligible for edge exploration."""
        results = [_make_unit()]
        # 1000 negative events → variance ≪ threshold.
        well_evidenced = _make_unit(confidence=0.5, confidence_evidence_count=1000)
        candidates = [results[0], well_evidenced]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_already_selected_units_excluded(self):
        unit = _make_unit(confidence=1.0, confidence_evidence_count=0)
        results = [unit]
        candidates = [unit]  # cold-start qualifies, but it's already in results.
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_max_injections_limits_count(self):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        eligible = [_make_unit(confidence=1.0, confidence_evidence_count=0) for _ in range(5)]
        candidates = [results[0]] + eligible
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_edge_exploration_candidates(
                results, candidates, epsilon=0.05, max_injections=1
            )
        assert len(selected) <= 1

    def test_deprioritized_units_excluded(self):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        deprioritized = _make_unit(
            confidence=1.0, confidence_evidence_count=0, is_deprioritized=True
        )
        candidates = [results[0], deprioritized]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_stale_units_excluded(self):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        stale = _make_unit(confidence=1.0, confidence_evidence_count=0, status=ContentStatus.STALE)
        candidates = [results[0], stale]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            selected = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
        assert selected == []

    def test_custom_high_variance_fraction(self):
        """Tightening the fraction excludes more candidates."""
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        # Small population — variance is in (threshold @ 0.5, MAX]
        # at count = 1, c = 0.5: variance = (1.5*1.5) / (3^2 * 4) = 0.0625
        moderate = _make_unit(confidence=0.5, confidence_evidence_count=1)
        candidates = [results[0], moderate]
        with patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01):
            # default threshold = 0.5 * 1/12 ≈ 0.0417 → 0.0625 IS eligible.
            default = select_edge_exploration_candidates(results, candidates, epsilon=0.05)
            # threshold = 0.95 * 1/12 ≈ 0.0792 → 0.0625 is NOT eligible.
            tight = select_edge_exploration_candidates(
                results, candidates, epsilon=0.05, high_variance_fraction=0.95
            )
        assert len(default) == 1
        assert tight == []

    def test_default_high_variance_fraction_is_one_half(self):
        """Pin the v1 default — surfaces only units with variance >= 0.5 * MAX."""
        assert DEFAULT_HIGH_VARIANCE_FRACTION == 0.5
        threshold = DEFAULT_HIGH_VARIANCE_FRACTION * MAX_VARIANCE
        # Cold-start variance = MAX_VARIANCE > threshold → qualifies.
        cold_unit = _make_unit(confidence=1.0, confidence_evidence_count=0)
        from memex_core.memory.retrieval.exploration import _unit_variance

        assert _unit_variance(cold_unit) > threshold

    def test_confidence_zero_not_coerced_to_one(self):
        """Hermes round-3 HIGH: ``confidence=0.0`` is legitimate (contradicted to zero).

        Prior code did ``getattr(unit, 'confidence', 1.0) or 1.0`` which
        coerced 0.0 back to 1.0 because 0.0 is falsy. A unit thoroughly
        contradicted to ``confidence=0.0, count=5`` would compute variance
        as if ``confidence=1.0, count=5``, inverting eligibility.

        Note: the Beta(alpha, beta) variance formula is symmetric under
        ``alpha <-> beta``, so the variance numerically coincides between
        ``(0.0, n)`` and ``(1.0, n)``. The bug therefore does NOT change
        the high-variance threshold predicate — but it WOULD change any
        downstream consumer of the returned ``mean`` (which is the input
        confidence, per the F22 design). We assert the helper now passes
        ``confidence`` through to ``mean_and_variance`` unchanged via a
        spy so the falsy-zero coercion is provably gone.
        """
        from unittest.mock import patch

        from memex_core.memory.retrieval.exploration import _unit_variance

        contradicted = _make_unit(confidence=0.0, confidence_evidence_count=5)
        with patch(
            'memex_core.memory.retrieval.exploration.mean_and_variance',
            wraps=_real_mean_and_variance,
        ) as spy:
            _unit_variance(contradicted)
        # The helper MUST forward confidence=0.0 verbatim — never 1.0.
        passed_confidence, passed_count = spy.call_args.args
        assert passed_confidence == 0.0
        assert passed_count == 5

    def test_confidence_none_falls_back_to_one(self):
        """Defensive: ``confidence=None`` (stale model) falls back to 1.0."""
        from memex_core.memory.confidence import mean_and_variance
        from memex_core.memory.retrieval.exploration import _unit_variance

        unit = _make_unit(confidence=1.0, confidence_evidence_count=5)
        # Force-strip the value to None to mirror a stripped/stale row.
        unit.confidence = None
        actual = _unit_variance(unit)
        _, expected = mean_and_variance(1.0, 5)
        assert actual == expected


class TestInjectEdgeExploration:
    """Integration of edge candidates into results."""

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.99)
    def test_no_injection_returns_original_list(self, _mock):
        results = [_make_unit()]
        candidates = [results[0]]
        output = inject_edge_exploration(results, candidates)
        # When nothing eligible, the function returns the input list as-is —
        # critical for downstream callers that may rely on identity.
        assert output is results

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injected_units_appended_to_end(self, _mock):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200, text='regular')]
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0, text='edge')
        candidates = [results[0], cold]
        output = inject_edge_exploration(results, candidates)
        assert len(output) == 2
        assert output[-1].text == 'edge'

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_injected_units_marked_edge_exploration(self, _mock):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0)
        candidates = [results[0], cold]
        output = inject_edge_exploration(results, candidates)
        injected = output[-1]
        assert injected.unit_metadata.get('edge_exploration') is True

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_regular_units_not_marked_edge_exploration(self, _mock):
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0)
        candidates = [results[0], cold]
        output = inject_edge_exploration(results, candidates)
        # The result-side unit must NOT pick up the edge_exploration flag.
        assert output[0].unit_metadata.get('edge_exploration') is not True

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_does_not_overwrite_existing_metadata(self, _mock):
        """Existing metadata keys survive the edge_exploration annotation."""
        results = [_make_unit(confidence=0.5, confidence_evidence_count=200)]
        cold = _make_unit(confidence=1.0, confidence_evidence_count=0)
        cold.unit_metadata = {'existing_key': 'preserve_me'}
        candidates = [results[0], cold]
        output = inject_edge_exploration(results, candidates)
        injected = output[-1]
        assert injected.unit_metadata.get('existing_key') == 'preserve_me'
        assert injected.unit_metadata.get('edge_exploration') is True

    def test_empty_results_no_crash(self):
        output = inject_edge_exploration([], [])
        assert output == []

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_does_not_dedupe_against_already_selected(self, _mock):
        """Units already in `results` (by id) are NOT re-selected as edge candidates."""
        already = _make_unit(confidence=1.0, confidence_evidence_count=0)  # cold-start
        results = [already]
        candidates = [already]  # same id — must be excluded.
        output = inject_edge_exploration(results, candidates)
        # Original list returned (identity) when nothing eligible.
        assert output is results


class TestMetadataCoercion:
    """`_coerce_metadata_to_dict` accepts any Mapping (Hermes round-2 MED).

    The silent-key-loss hazard from the prior `isinstance(..., dict)` check
    is gone: non-dict Mappings now have their keys preserved.
    """

    def test_dict_passthrough(self):
        from memex_core.memory.retrieval.exploration import _coerce_metadata_to_dict

        original = {'key': 'value'}
        assert _coerce_metadata_to_dict(original) is original

    def test_mapping_converted_preserves_keys(self):
        from collections.abc import Mapping

        from memex_core.memory.retrieval.exploration import _coerce_metadata_to_dict

        class _ROMapping(Mapping):
            def __init__(self, data):
                self._data = data

            def __getitem__(self, k):
                return self._data[k]

            def __iter__(self):
                return iter(self._data)

            def __len__(self):
                return len(self._data)

        out = _coerce_metadata_to_dict(_ROMapping({'preserved': 'yes'}))
        assert out == {'preserved': 'yes'}
        assert isinstance(out, dict)

    def test_none_returns_empty_dict(self):
        from memex_core.memory.retrieval.exploration import _coerce_metadata_to_dict

        assert _coerce_metadata_to_dict(None) == {}

    def test_non_mapping_returns_empty_dict(self):
        from memex_core.memory.retrieval.exploration import _coerce_metadata_to_dict

        # Non-Mapping garbage falls through to empty — same fall-through
        # the original isinstance(..., dict) check provided.
        assert _coerce_metadata_to_dict('not a dict') == {}
        assert _coerce_metadata_to_dict(42) == {}


class TestCrossPathInjectionGuard:
    """Hermes round-21 HIGH: a unit already carrying ``exploration`` or
    ``edge_exploration`` metadata MUST NOT be re-injected by either
    selector, even if the call-site disjoint-pool invariant is broken.
    """

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_low_mw_selector_skips_already_exploration_annotated(self, _mock):
        """A unit already annotated ``exploration=True`` must not be
        re-selected by ``select_exploration_candidates`` — defends against
        a caller that breaks the disjoint-pool invariant."""
        results = [_make_unit(success=10, failure=2)]
        cold_clean = _make_unit(success=0, failure=0)
        cold_already = _make_unit(success=0, failure=0)
        cold_already.unit_metadata = {'exploration': True}
        candidates = [results[0], cold_clean, cold_already]
        selected = select_exploration_candidates(results, candidates, epsilon=1.0)
        selected_ids = {u.id for u in selected}
        assert cold_already.id not in selected_ids
        # The clean cold-start unit IS still eligible.
        assert cold_clean.id in selected_ids

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_low_mw_selector_skips_already_edge_exploration_annotated(self, _mock):
        results = [_make_unit(success=10, failure=2)]
        cold_clean = _make_unit(success=0, failure=0)
        cold_already = _make_unit(success=0, failure=0)
        cold_already.unit_metadata = {'edge_exploration': True}
        candidates = [results[0], cold_clean, cold_already]
        selected = select_exploration_candidates(results, candidates, epsilon=1.0)
        selected_ids = {u.id for u in selected}
        assert cold_already.id not in selected_ids
        assert cold_clean.id in selected_ids

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_edge_selector_skips_already_exploration_annotated(self, _mock):
        """A unit already annotated ``exploration=True`` must not be
        re-selected by ``select_edge_exploration_candidates``."""
        results = [_make_unit()]
        # High-variance candidate (cold-start gives MAX_VARIANCE).
        hv_clean = _make_unit(confidence=0.5, confidence_evidence_count=0)
        hv_already = _make_unit(confidence=0.5, confidence_evidence_count=0)
        hv_already.unit_metadata = {'exploration': True}
        candidates = [results[0], hv_clean, hv_already]
        selected = select_edge_exploration_candidates(
            results,
            candidates,
            epsilon=1.0,
            high_variance_fraction=DEFAULT_HIGH_VARIANCE_FRACTION,
            max_injections=10,
        )
        selected_ids = {u.id for u in selected}
        assert hv_already.id not in selected_ids
        assert hv_clean.id in selected_ids

    @patch('memex_core.memory.retrieval.exploration.random.random', return_value=0.01)
    def test_edge_selector_skips_already_edge_exploration_annotated(self, _mock):
        results = [_make_unit()]
        hv_clean = _make_unit(confidence=0.5, confidence_evidence_count=0)
        hv_already = _make_unit(confidence=0.5, confidence_evidence_count=0)
        hv_already.unit_metadata = {'edge_exploration': True}
        candidates = [results[0], hv_clean, hv_already]
        selected = select_edge_exploration_candidates(
            results,
            candidates,
            epsilon=1.0,
            high_variance_fraction=DEFAULT_HIGH_VARIANCE_FRACTION,
            max_injections=10,
        )
        selected_ids = {u.id for u in selected}
        assert hv_already.id not in selected_ids
        assert hv_clean.id in selected_ids
