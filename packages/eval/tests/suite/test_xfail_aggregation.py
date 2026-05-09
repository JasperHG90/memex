"""Tests for pytest-style xfail / xpass status handling.

A scenario can declare ``expected_failure_modes=['api']`` (or any backend
name). When the active answer_mode matches:
- a fail becomes ``xfail`` and counts as a pass for ``suite.pass_rate``
- a pass becomes ``xpass`` and counts as a fail (the embedded constraint
  is wrong / the bug is fixed and the marker should be removed)
"""

from __future__ import annotations

from memex_eval.suite.base import ScenarioOutcome
from memex_eval.suite.runner import _aggregate_results


def _outcome(scenario_id: str, status: str, **extra) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario_id=scenario_id,
        status=status,  # type: ignore[arg-type]
        metrics=extra.get('metrics', {'pass': 1.0 if status in {'pass', 'xpass'} else 0.0}),
        duration_ms=1.0,
    )


class TestXfailAggregation:
    def test_xfail_counted_as_pass_in_pass_rate(self) -> None:
        outcomes = [
            _outcome('a', 'pass'),
            _outcome('b', 'xfail'),
            _outcome('c', 'fail'),
        ]
        agg = _aggregate_results(outcomes)
        # numerator = pass + xfail = 2; denominator = pass + fail + xfail + xpass = 3
        assert agg['suite.pass_rate'] == 2 / 3
        assert agg['count.passed'] == 1
        assert agg['count.failed'] == 1
        assert agg['count.xfailed'] == 1
        assert agg['count.xpassed'] == 0

    def test_xpass_counted_as_fail_in_pass_rate(self) -> None:
        outcomes = [
            _outcome('a', 'pass'),
            _outcome('b', 'xpass'),
        ]
        agg = _aggregate_results(outcomes)
        # numerator = pass = 1; denominator = pass + xpass = 2
        assert agg['suite.pass_rate'] == 0.5
        assert agg['count.xpassed'] == 1

    def test_skip_excluded_from_denominator(self) -> None:
        outcomes = [
            _outcome('a', 'pass'),
            _outcome('b', 'skip'),
        ]
        agg = _aggregate_results(outcomes)
        assert agg['suite.pass_rate'] == 1.0
        assert agg['count.skipped'] == 1

    def test_error_excluded_from_pass_rate(self) -> None:
        outcomes = [
            _outcome('a', 'pass'),
            _outcome('b', 'error'),
        ]
        agg = _aggregate_results(outcomes)
        assert agg['suite.pass_rate'] == 1.0
        assert agg['count.errored'] == 1

    def test_all_xfail_full_pass_rate(self) -> None:
        """A suite where every scenario is xfail in this mode has pass_rate=1.0."""
        outcomes = [_outcome('a', 'xfail'), _outcome('b', 'xfail')]
        agg = _aggregate_results(outcomes)
        assert agg['suite.pass_rate'] == 1.0
        assert agg['count.xfailed'] == 2

    def test_empty_runnable_returns_zero_pass_rate(self) -> None:
        outcomes = [_outcome('a', 'skip'), _outcome('b', 'error')]
        agg = _aggregate_results(outcomes)
        assert agg['suite.pass_rate'] == 0.0

    def test_errored_outcomes_excluded_from_latency(self) -> None:
        """Errored runs carry meaningless durations — including them
        skews p50/p95 (regression guard for review feedback M3)."""
        outcomes = [
            ScenarioOutcome(
                scenario_id='ok',
                status='pass',
                metrics={'pass': 1.0},
                duration_ms=100.0,
            ),
            ScenarioOutcome(
                scenario_id='boom',
                status='error',
                metrics={},
                duration_ms=1.0,  # near-zero crash, would skew p50 down
                error='setup-action crashed',
            ),
        ]
        agg = _aggregate_results(outcomes)
        # p50/p95 must reflect only the passing run's 100ms.
        assert agg['latency_ms.p50'] == 100.0
        assert agg['latency_ms.p95'] == 100.0
        assert agg['latency_ms.mean'] == 100.0


class TestXfailWithSlaViolation:
    """Regression for review feedback M8: SLA-violation in xfail mode must
    fail (not xfail). The xfail expectation does not extend to SLA — a
    scenario that takes too long is broken regardless of whether its
    content was the expected failure.
    """

    def test_sla_violation_in_xfail_mode_fails_not_xfails(self) -> None:
        # Aggregation-layer canary: confirms a hand-built ``status='fail'``
        # SLA outcome aggregates correctly. The end-to-end relabel-bypass
        # is exercised by ``test_execute_scenario_sla_violation_bypasses_xfail``
        # below.
        outcome = ScenarioOutcome(
            scenario_id='slow',
            status='fail',
            metrics={'pass': 0.0},
            actual_summary={'exceeded_max_duration_ms': 5000.0},
            duration_ms=5000.0,
            error='Exceeded max_duration_ms: 5000ms > 100ms',
            answer_mode='api',
        )
        agg = _aggregate_results([outcome])
        assert agg['count.failed'] == 1
        assert agg['count.xfailed'] == 0
        assert agg['suite.pass_rate'] == 0.0


class TestExecuteScenarioSlaBypassXfail:
    """End-to-end check: a slow scenario whose ``answer_mode`` is in
    ``expected_failure_modes`` must STILL be reported as ``status='fail'``
    when it exceeds ``max_duration_ms`` — the SLA path bypasses the
    xfail relabel. Regression guard for M-NEW-1: if a future refactor
    moves the SLA check below the relabel, this test catches it where
    the aggregation-layer test would not.
    """

    def test_execute_scenario_sla_violation_bypasses_xfail(self) -> None:
        import asyncio

        from memex_eval.suite import (
            AgentAnswer,
            AnswerBackend,
            KeywordsPresent,
            Scenario,
            Suite,
            SuiteMetadata,
            SuiteSources,
            isolated_registries,
            register_backend,
        )
        from memex_eval.suite.runner import _execute_scenario

        with isolated_registries():

            @register_backend('slow_test_backend')
            class _SlowBackend(AnswerBackend):
                async def answer(
                    self,
                    scenario,
                    *,
                    api,
                    vault_id,
                    server_url,
                    judge=None,
                ) -> AgentAnswer:
                    # Block well past the scenario's 1ms SLA cap so the
                    # runner sees a max_duration_ms violation. asyncio.sleep
                    # yields control; the call itself takes ~50ms wall.
                    await asyncio.sleep(0.05)
                    # Empirically empty answer; KeywordsPresent will say
                    # "fail" if the SLA path didn't short-circuit. Either
                    # way the relabel must NOT fire.
                    return AgentAnswer(backend_name='slow_test_backend')

            scenario = Scenario(
                id='slow_scenario',
                description='deliberate SLA violation under xfail mode',
                query='whatever',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                top_k=5,
                max_duration_ms=1.0,
                # The slow backend is in the xfail list — without the
                # SLA-bypass, the resulting fail would relabel to xfail.
                expected_failure_modes=['slow_test_backend'],
                answer_mode='slow_test_backend',
            )

            suite = Suite(
                metadata=SuiteMetadata(
                    name='sla_bypass_test',
                    schema_version='1',
                    suite_version='1.0.0',
                    description='SLA-bypass regression test',
                    tags=['test'],
                    primary_metrics=['suite.pass_rate'],
                    components_under_test=['runner.sla'],
                    knobs=[],
                    requires_llm_judge=False,
                ),
                sources=SuiteSources(),
                scenarios=[scenario],
                readme_path=None,
            )

            outcome = asyncio.run(
                _execute_scenario(
                    # slow backend doesn't touch the api parameter
                    api=None,  # type: ignore[arg-type]
                    server_url='http://localhost/api/v1/',
                    vault_id=__import__('uuid').UUID(int=0),
                    scenario=scenario,
                    suite=suite,
                    judge=None,
                    note_key_to_unit_ids={},
                    replicate_index=0,
                )
            )

            # The SLA-bypass requires status='fail' regardless of
            # expected_failure_modes membership. If a future refactor
            # places the relabel above the SLA short-circuit, this would
            # report 'xfail' and the test fails.
            assert outcome.status == 'fail', (
                f'SLA violation in xfail mode must fail (not xfail); got {outcome.status!r}. '
                f'error={outcome.error!r}'
            )
            assert outcome.error is not None
            assert 'max_duration_ms' in outcome.error
            # Sanity: the relabel applies in the non-SLA path. Use a
            # second scenario with no SLA cap and the same backend; this
            # one MUST be xfail, proving the relabel still fires.

        # Outside isolated_registries — check the OTHER branch (relabel
        # works) using a second isolated registration.
        with isolated_registries():

            @register_backend('quick_failer')
            class _QuickBackend(AnswerBackend):
                async def answer(
                    self,
                    scenario,
                    *,
                    api,
                    vault_id,
                    server_url,
                    judge=None,
                ) -> AgentAnswer:
                    return AgentAnswer(backend_name='quick_failer')  # empty answer → fail

            sc2 = Scenario(
                id='quick_xfail',
                description='quick fail under xfail mode',
                query='whatever',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                top_k=5,
                expected_failure_modes=['quick_failer'],
                answer_mode='quick_failer',
            )
            suite2 = Suite(
                metadata=SuiteMetadata(
                    name='sla_bypass_test_2',
                    schema_version='1',
                    suite_version='1.0.0',
                    description='control: relabel works without SLA',
                    tags=['test'],
                    primary_metrics=['suite.pass_rate'],
                    components_under_test=['runner.xfail'],
                    knobs=[],
                    requires_llm_judge=False,
                ),
                sources=SuiteSources(),
                scenarios=[sc2],
                readme_path=None,
            )

            o2 = asyncio.run(
                _execute_scenario(
                    api=None,  # type: ignore[arg-type]
                    server_url='http://localhost/api/v1/',
                    vault_id=__import__('uuid').UUID(int=0),
                    scenario=sc2,
                    suite=suite2,
                    judge=None,
                    note_key_to_unit_ids={},
                    replicate_index=0,
                )
            )
            assert o2.status == 'xfail', f'Without SLA, the relabel SHOULD fire; got {o2.status!r}.'
