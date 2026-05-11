"""Tests for Scenario.mutating_scenario × _compute_own_skip_reason."""

from __future__ import annotations

from memex_eval.suite.base import (
    KeywordsPresent,
    Scenario,
    Suite,
    SuiteMetadata,
)
from memex_eval.suite.runner import _compute_own_skip_reason
from memex_eval.suite.sources import SuiteSources


def _scenario(**overrides) -> Scenario:
    base = dict(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )
    base.update(overrides)
    return Scenario(**base)


def _suite(scenarios: list[Scenario]) -> Suite:
    return Suite(
        metadata=SuiteMetadata(
            name='probe',
            schema_version='1',
            suite_version='0.1.0',
            description='probe',
        ),
        sources=SuiteSources(notes=[]),
        scenarios=scenarios,
    )


def test_mutating_scenario_skips_under_reuse_vault() -> None:
    sc = _scenario(mutating_scenario=True)
    suite = _suite([sc])
    assert (
        _compute_own_skip_reason(
            sc,
            suite=suite,
            reuse_vault='label',
            config_snapshot_available=True,
            nli_available=True,
        )
        == 'mutating_under_reuse_vault'
    )


def test_mutating_scenario_runs_without_reuse_vault() -> None:
    sc = _scenario(mutating_scenario=True)
    suite = _suite([sc])
    assert (
        _compute_own_skip_reason(
            sc,
            suite=suite,
            reuse_vault=None,
            config_snapshot_available=True,
            nli_available=True,
        )
        is None
    )


def test_non_mutating_scenario_runs_under_reuse_vault() -> None:
    sc = _scenario(mutating_scenario=False)
    suite = _suite([sc])
    assert (
        _compute_own_skip_reason(
            sc,
            suite=suite,
            reuse_vault='label',
            config_snapshot_available=True,
            nli_available=True,
        )
        is None
    )


def test_xfail_mutating_scenario_still_skipped_under_reuse() -> None:
    """xfail + mutating combo under --reuse-vault: the mutating skip wins.

    Three scenarios in agent_integration carry both flags
    (review_loop_drives_due, review_loop_records_rating,
    asset_lifecycle_detach). Under --reuse-vault they must skip, not
    surface xfail/xpass — the README warns users this silences the
    plugin-gap tripwire.
    """
    sc = _scenario(
        mutating_scenario=True,
        expected_failure_modes=['hermes'],
    )
    suite = _suite([sc])
    assert (
        _compute_own_skip_reason(
            sc,
            suite=suite,
            reuse_vault='label',
            config_snapshot_available=True,
            nli_available=True,
        )
        == 'mutating_under_reuse_vault'
    )


def test_mutating_skip_takes_precedence_over_setup_action_check() -> None:
    """Mutating scenarios skip via the new path even if setup_actions would
    also block them. This keeps the skip_reason unambiguous and tests the
    early-return ordering."""
    from memex_eval.suite.base import SetupAction

    sc = _scenario(
        mutating_scenario=True,
        setup_actions=[SetupAction(kind='record_outcome', search_query='x', success=True)],
    )
    suite = _suite([sc])
    assert (
        _compute_own_skip_reason(
            sc,
            suite=suite,
            reuse_vault='label',
            config_snapshot_available=True,
            nli_available=True,
        )
        == 'mutating_under_reuse_vault'
    )


def test_decorator_accepts_replicates_override_and_mutating() -> None:
    from memex_eval.suite.decorator import Suite as DSuite

    suite = DSuite(
        metadata=SuiteMetadata(
            name='probe',
            schema_version='1',
            suite_version='0.1.0',
            description='d',
        ),
    )
    sc = suite.register(
        id='probe',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        replicates_override=1,
        mutating_scenario=True,
    )
    assert sc.replicates_override == 1
    assert sc.mutating_scenario is True
