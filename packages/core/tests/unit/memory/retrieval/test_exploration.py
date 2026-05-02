"""Unit tests for MW exploration floor (F33)."""

from unittest.mock import patch
from uuid import uuid4

from memex_core.memory.retrieval.exploration import (
    inject_exploration_units,
    select_exploration_candidates,
)
from memex_core.memory.sql_models import ContentStatus, MemoryUnit


def _make_unit(
    success: int = 0,
    failure: int = 0,
    text: str = 'test',
    status: ContentStatus = ContentStatus.ACTIVE,
    is_deprioritized: bool = False,
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
