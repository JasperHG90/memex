"""Tests for aggregate_per_scenario in metrics.py."""

from __future__ import annotations

from memex_eval.suite.base import ScenarioOutcome
from memex_eval.suite.metrics import _sanitize_mlflow_key, aggregate_per_scenario


def _o(scenario_id: str, status: str, replicate_index: int = 0) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario_id=scenario_id,
        status=status,
        replicate_index=replicate_index,
        answer_mode='hermes',
    )


class TestPerScenarioMetrics:
    def test_emits_per_scenario_pass_rate(self) -> None:
        outcomes = [
            _o('s1', 'pass', 0),
            _o('s1', 'pass', 1),
            _o('s1', 'fail', 2),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=3)
        assert result['scenario.s1.pass_rate'] == 2 / 3
        assert result['scenario.s1.pass_count'] == 2.0
        assert result['scenario.s1.total'] == 3.0

    def test_excludes_skipped_outcomes(self) -> None:
        outcomes = [
            _o('s1', 'pass', 0),
            _o('s1', 'skip', 1),
            _o('s1', 'skip', 2),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=3)
        assert result['scenario.s1.pass_rate'] == 1.0
        assert result['scenario.s1.total'] == 1.0

    def test_excludes_errored_outcomes(self) -> None:
        outcomes = [
            _o('s1', 'pass', 0),
            _o('s1', 'error', 1),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=2)
        assert result['scenario.s1.pass_rate'] == 1.0
        assert result['scenario.s1.total'] == 1.0

    def test_all_errored_emits_no_keys(self) -> None:
        outcomes = [
            _o('s1', 'error', 0),
            _o('s1', 'error', 1),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=2)
        assert 'scenario.s1.pass_rate' not in result

    def test_xfail_counts_as_pass(self) -> None:
        outcomes = [_o('s1', 'xfail', 0)]
        result = aggregate_per_scenario(outcomes, run_replicates=1)
        assert result['scenario.s1.pass_rate'] == 1.0

    def test_xpass_counts_as_fail(self) -> None:
        outcomes = [_o('s1', 'xpass', 0)]
        result = aggregate_per_scenario(outcomes, run_replicates=1)
        assert result['scenario.s1.pass_rate'] == 0.0


class TestSuiteRollups:
    def test_suite_pass_rate_all(self) -> None:
        outcomes = [
            _o('s1', 'pass', 0),
            _o('s1', 'fail', 1),
            _o('s2', 'pass', 0),
            _o('s2', 'pass', 1),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=2)
        assert result['suite.pass_rate_all'] == 3 / 4

    def test_suite_pass_rate_excludes_mutating(self) -> None:
        outcomes = [
            _o('s1', 'pass', 0),
            _o('s1', 'pass', 1),
            _o('s1', 'pass', 2),
            _o('s_mutating', 'fail', 0),
        ]
        result = aggregate_per_scenario(outcomes, run_replicates=3)
        assert result['suite.pass_rate_non_mutating'] == 1.0
        assert result['suite.pass_rate_all'] == 3 / 4


class TestMlflowKeySanitizer:
    def test_clean_ids_unchanged(self) -> None:
        assert _sanitize_mlflow_key('triage_picks_relevant_from_many') == (
            'triage_picks_relevant_from_many'
        )

    def test_disallowed_chars_replaced(self) -> None:
        assert _sanitize_mlflow_key('foo:bar') == 'foo_bar'
        assert _sanitize_mlflow_key('foo[1]') == 'foo_1_'
