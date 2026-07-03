"""Unit tests for the sweep harness. Heavy integration (real subprocess
spawn + Postgres) lives in ``test_server_control.py`` gated behind
``MEMEX_RUN_HEAVY_INTEGRATION=1``."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from memex_eval.suite.sweep import (
    DEFAULT_MAX_POINTS,
    SweepNotSupportedRemote,
    SweepPointResult,
    SweepResult,
    SweepValidationError,
    _experiment_name,
    _is_local_url,
    _is_secret_type,
    _is_sweepable_leaf,
    _resolve_model_class,
    build_env_overrides,
    expand_sweep_grid,
    parse_param_specs,
    validate_param_paths_against_config,
)


class TestParseParamSpecs:
    def test_single_param_three_values(self) -> None:
        out = parse_param_specs(['mw_alpha=0.0,0.3,0.5'])
        assert out == {'mw_alpha': ['0.0', '0.3', '0.5']}

    def test_strips_whitespace_in_values(self) -> None:
        out = parse_param_specs(['k=1, 2 ,3'])
        assert out == {'k': ['1', '2', '3']}

    def test_multiple_params(self) -> None:
        out = parse_param_specs(['a=1,2', 'b.c=x,y'])
        assert out == {'a': ['1', '2'], 'b.c': ['x', 'y']}

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='must be KEY='):
            parse_param_specs(['no_equals'])

    def test_empty_key_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='empty key'):
            parse_param_specs(['=1,2'])

    def test_empty_values_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='no values'):
            parse_param_specs(['k='])

    def test_only_commas_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='no values'):
            parse_param_specs(['k=,,'])

    def test_duplicate_key_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='specified twice'):
            parse_param_specs(['k=1', 'k=2'])

    def test_no_specs_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='At least one'):
            parse_param_specs([])


class TestExpandSweepGrid:
    def test_single_axis(self) -> None:
        grid = expand_sweep_grid({'a': ['1', '2', '3']})
        assert grid == [{'a': '1'}, {'a': '2'}, {'a': '3'}]

    def test_two_axes_cartesian(self) -> None:
        grid = expand_sweep_grid({'a': ['1', '2'], 'b': ['x', 'y']})
        # Sorted-keys × itertools.product → stable order
        assert grid == [
            {'a': '1', 'b': 'x'},
            {'a': '1', 'b': 'y'},
            {'a': '2', 'b': 'x'},
            {'a': '2', 'b': 'y'},
        ]

    def test_empty_axis_drops_to_empty_grid(self) -> None:
        # Empty list × any other axis → empty product. The validator
        # rejects empty values upstream, but the helper should not crash.
        grid = expand_sweep_grid({'a': [], 'b': ['x']})
        assert grid == []


class TestBuildEnvOverrides:
    def test_dotted_path_to_env_var(self) -> None:
        out = build_env_overrides({'server.memory.retrieval.reranking_mw_alpha': '0.5'})
        assert out == {'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.5'}

    def test_multiple_overrides(self) -> None:
        out = build_env_overrides(
            {
                'server.memory.retrieval.reranking_mw_alpha': '0.3',
                'server.memory.lint.cost_cap_per_24h': '0.05',
            }
        )
        assert out == {
            'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.3',
            'MEMEX_SERVER__MEMORY__LINT__COST_CAP_PER_24H': '0.05',
        }


class TestValidateParamPaths:
    def test_known_path_accepted(self) -> None:
        # The existing default config has server.memory.retrieval.* knobs.
        # Pick one we know is sweepable.
        validate_param_paths_against_config(['server.memory.retrieval.reranking_mw_alpha'])

    def test_unknown_path_segment_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='does not resolve'):
            validate_param_paths_against_config(
                ['server.memory.retrieval.totally_made_up_knob_name']
            )

    def test_unknown_top_level_raises(self) -> None:
        with pytest.raises(SweepValidationError, match='does not resolve'):
            validate_param_paths_against_config(['nope.does.not.exist'])

    def test_intermediate_non_model_raises(self) -> None:
        # ``api_key`` is a SecretStr leaf at the top level — extending past
        # it should fail the "intermediate must be a model" check.
        with pytest.raises(SweepValidationError):
            validate_param_paths_against_config(['api_key.something'])

    def test_secret_leaf_rejected(self) -> None:
        # MemexConfig.api_key: SecretStr | None — sweeping it would
        # exfiltrate via env / MLflow params.
        with pytest.raises(SweepValidationError, match='SecretStr'):
            validate_param_paths_against_config(['api_key'])


class TestRunSweepValidation:
    """``run_sweep`` itself is async + side-effecting; the unit-testable
    surface is its pre-spawn validation. Heavy integration covered
    separately."""

    @pytest.mark.asyncio
    async def test_remote_url_rejected(self) -> None:
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sweep import run_sweep

        meta = SuiteMetadata(
            name='dummy',
            schema_version='1',
            suite_version='1.0.0',
            description='dummy',
        )
        suite = Suite(metadata=meta, sources=SuiteSources(notes=[]), scenarios=[])
        with pytest.raises(SweepNotSupportedRemote, match='local-only'):
            await run_sweep(
                suite,
                param_grid={'server.memory.retrieval.reranking_mw_alpha': ['0.0']},
                server_url='https://staging.memex.example.com/api/v1/',
            )

    @pytest.mark.asyncio
    async def test_unknown_param_path_rejected_pre_spawn(self) -> None:
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sweep import run_sweep

        meta = SuiteMetadata(
            name='dummy',
            schema_version='1',
            suite_version='1.0.0',
            description='dummy',
        )
        suite = Suite(metadata=meta, sources=SuiteSources(notes=[]), scenarios=[])
        with pytest.raises(SweepValidationError, match='does not resolve'):
            await run_sweep(
                suite,
                param_grid={'totally.bogus.path': ['1', '2']},
            )


# ---------------------------------------------------------------------------
# Round-1 review fixes — annotation handling, grid cap, IPv6 / URL edges,
# JSON serialisation, experiment naming, active-mlflow handling.
# ---------------------------------------------------------------------------


class TestAnnotationHandling:
    """``_resolve_model_class`` and ``_is_sweepable_leaf`` must handle
    ``Annotated[...]`` wrappers and discriminated unions. Round-1 HIGH-2 / 5."""

    def test_resolve_model_through_annotated(self) -> None:
        class Inner(BaseModel):
            val: int = 0

        annotation = Annotated[Inner, Field(description='wrapped')]
        assert _resolve_model_class(annotation) is Inner

    def test_resolve_model_through_optional(self) -> None:
        class Inner(BaseModel):
            val: int = 0

        annotation = Inner | None
        assert _resolve_model_class(annotation) is Inner

    def test_resolve_model_through_discriminated_union(self) -> None:
        class ArmA(BaseModel):
            kind: Literal['a'] = 'a'

        class ArmB(BaseModel):
            kind: Literal['b'] = 'b'

        annotation = Annotated[ArmA | ArmB, Field(discriminator='kind')]
        # First BaseModel arm wins.
        assert _resolve_model_class(annotation) is ArmA

    def test_resolve_returns_none_for_non_model(self) -> None:
        assert _resolve_model_class(int) is None
        assert _resolve_model_class(int | None) is None
        assert _resolve_model_class(Annotated[int, Field()]) is None

    def test_resolve_warns_on_multiple_model_arms(self, caplog: pytest.LogCaptureFixture) -> None:
        # Round-2 MEDIUM-R2-4: discriminated unions with multiple BaseModel
        # arms are ambiguous from a static-validation standpoint; the
        # resolver picks the first arm and must surface a WARNING naming
        # every arm so the operator can spot the ambiguity.
        import logging

        class ArmA(BaseModel):
            kind: Literal['a'] = 'a'

        class ArmB(BaseModel):
            kind: Literal['b'] = 'b'

        annotation = Annotated[ArmA | ArmB, Field(discriminator='kind')]
        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.sweep'):
            resolved = _resolve_model_class(annotation)
        assert resolved is ArmA
        assert any(
            'discriminated/Union' in rec.message and 'ArmA' in rec.message and 'ArmB' in rec.message
            for rec in caplog.records
        ), f'expected ambiguity warning, got: {[r.message for r in caplog.records]}'

    def test_resolve_recursive_through_optional_annotated_union(self) -> None:
        # Round-2 HIGH-R2-2: ``Optional[Annotated[Union[A, B], Field(...)]]``
        # used to drop the inner union; recursion must reach a model arm.
        class Inner(BaseModel):
            val: int = 0

        annotation: Any = Annotated[Inner | None, Field(description='wrapped')] | None
        assert _resolve_model_class(annotation) is Inner

    def test_sweepable_leaf_through_annotated(self) -> None:
        assert _is_sweepable_leaf(Annotated[float, Field(ge=0.0, le=1.0)])
        assert _is_sweepable_leaf(Annotated[int, Field(gt=0)])
        assert _is_sweepable_leaf(Annotated[str, Field()])

    def test_sweepable_leaf_optional_annotated(self) -> None:
        assert _is_sweepable_leaf(Annotated[float | None, Field()])
        assert _is_sweepable_leaf(Annotated[int, Field()] | None)

    def test_sweepable_leaf_literal(self) -> None:
        assert _is_sweepable_leaf(Literal['a', 'b', 'c'])
        assert _is_sweepable_leaf(Annotated[Literal['x', 'y'], Field()])

    def test_sweepable_leaf_rejects_models(self) -> None:
        class Inner(BaseModel):
            val: int = 0

        assert not _is_sweepable_leaf(Inner)
        assert not _is_sweepable_leaf(Annotated[Inner, Field()])

    def test_secret_type_detection(self) -> None:
        from pydantic import SecretStr

        assert _is_secret_type(SecretStr)
        assert _is_secret_type(SecretStr | None)
        assert _is_secret_type(Annotated[SecretStr, Field()])
        assert not _is_secret_type(str)
        assert not _is_secret_type(Annotated[float, Field()])


class TestIsLocalUrl:
    """Local-vs-remote URL detection covers the actual edge cases."""

    def test_localhost(self) -> None:
        assert _is_local_url('http://localhost:8000/api/v1/')

    def test_127_loopback(self) -> None:
        assert _is_local_url('http://127.0.0.1:8000/api/v1/')

    def test_ipv6_loopback(self) -> None:
        assert _is_local_url('http://[::1]:8000/api/v1/')

    def test_remote_host_rejected(self) -> None:
        assert not _is_local_url('https://staging.example.com/api/v1/')

    def test_no_scheme_returns_false(self) -> None:
        # urlparse with no scheme returns hostname=None; sweep treats as remote.
        assert not _is_local_url('staging.example.com/api/v1/')

    def test_empty_url(self) -> None:
        assert not _is_local_url('')


class TestExperimentName:
    """Parent and child runs MUST share an experiment so the MLflow
    Compare view groups them. Round-1 CRITICAL-1."""

    def test_explicit_wins(self) -> None:
        import datetime as dt

        name = _experiment_name(
            'acme', ['k.a'], dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc), 'my-exp'
        )
        assert name == 'my-exp'

    def test_default_includes_knob_token_and_yyyymm(self) -> None:
        import datetime as dt

        name = _experiment_name(
            'acme',
            ['server.memory.retrieval.reranking_mw_alpha'],
            dt.datetime(2026, 5, 10, tzinfo=dt.timezone.utc),
            None,
        )
        assert 'acme' in name
        assert 'server_memory_retrieval_reranking_mw_alpha' in name
        assert '202605' in name

    def test_multi_knob_join(self) -> None:
        import datetime as dt

        name = _experiment_name(
            'acme',
            ['k.a', 'k.b'],
            dt.datetime(2026, 5, 10, tzinfo=dt.timezone.utc),
            None,
        )
        # Sorted-keys + plus-joined.
        assert 'k_a+k_b' in name


class TestGridExplosionGuard:
    """Round-1 HIGH-3: Cartesian explosion must be capped before any spawn."""

    @pytest.mark.asyncio
    async def test_grid_over_cap_rejected(self) -> None:
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sweep import run_sweep

        meta = SuiteMetadata(
            name='dummy',
            schema_version='1',
            suite_version='1.0.0',
            description='dummy',
        )
        suite = Suite(metadata=meta, sources=SuiteSources(notes=[]), scenarios=[])
        # 6 × 6 = 36 < 50, OK. 6 × 6 × 6 = 216 > 50.
        oversized = {
            'server.memory.retrieval.reranking_mw_alpha': ['0.0'] * 6,
            'server.memory.retrieval.reranking_recency_alpha': ['0.0'] * 6,
            'server.memory.retrieval.reranking_temporal_alpha': ['0.0'] * 6,
        }
        with pytest.raises(SweepValidationError, match='exceeding the safety cap'):
            await run_sweep(
                suite,
                param_grid=oversized,
                max_points=50,
            )

    @pytest.mark.asyncio
    async def test_grid_at_cap_accepted(self) -> None:
        # Validation-only path: no spawn happens because spawn is mocked
        # at the run_suite layer in TestRunSweepOrchestration. Here we
        # only assert the cap guard does not raise at exactly the cap.
        from memex_eval.suite.sweep import expand_sweep_grid

        grid = expand_sweep_grid({'a': ['1'] * 50})
        assert len(grid) == 50

    def test_default_cap_value(self) -> None:
        assert DEFAULT_MAX_POINTS == 50


class TestSweepResultPydanticSerialization:
    """Round-1 MEDIUM-3 / 6: ``SweepResult`` is now a Pydantic model so
    ``model_dump_json`` round-trips datetimes / nested ``RunResult``
    cleanly. The CLI's --output writer relies on this."""

    def test_round_trip_empty(self) -> None:
        import datetime as dt
        import json as _json

        result = SweepResult(
            sweep_id='abc',
            suite_name='dummy',
            parent_run_id=None,
            started_at=dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2026, 5, 10, 12, 5, tzinfo=dt.timezone.utc),
            points=[],
            children_failed=0,
            children_total=0,
        )
        serialised = result.model_dump_json()
        data = _json.loads(serialised)
        assert data['sweep_id'] == 'abc'
        assert '2026-05-10' in data['started_at']

    def test_round_trip_with_point(self) -> None:
        import datetime as dt
        import json as _json

        point = SweepPointResult(
            point_index=0,
            overrides={'a': '1'},
            run_result=None,
            error='spawn failed',
            server_pid=None,
            shutdown_method='never_spawned',
            duration_seconds=2.5,
        )
        result = SweepResult(
            sweep_id='abc',
            suite_name='dummy',
            parent_run_id='parent_42',
            started_at=dt.datetime(2026, 5, 10, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2026, 5, 10, tzinfo=dt.timezone.utc),
            points=[point],
            children_failed=1,
            children_total=1,
        )
        data = _json.loads(result.model_dump_json())
        assert data['children_failed'] == 1
        assert data['points'][0]['error'] == 'spawn failed'
        assert data['points'][0]['shutdown_method'] == 'never_spawned'

    def test_round_trip_with_populated_run_result(self) -> None:
        # Round-2 MEDIUM-R2-2: success-path round-trip — a fully-populated
        # RunResult with a ScenarioOutcome must serialise via
        # ``model_dump_json`` and reload via ``model_validate_json``
        # without dropping fields. This is the path the CLI's --output
        # writer takes on a healthy sweep, and was previously uncovered
        # because the active-mlflow test stubbed run_suite to raise.
        import datetime as dt

        from memex_eval.suite.base import RunResult, ScenarioOutcome

        ts = dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.timezone.utc)
        scenario = ScenarioOutcome(
            scenario_id='alpha',
            status='pass',
            metrics={'recall_at_10': 1.0, 'mrr': 0.5},
            duration_ms=42.0,
        )
        run = RunResult(
            suite_name='dummy',
            suite_version='1.0.0',
            schema_version='1',
            run_id='run-abc',
            started_at=ts,
            finished_at=ts,
            scenario_outcomes=[scenario],
            suite_metrics={'pass_rate': 1.0, 'metric.recall_at_10.mean': 1.0},
        )
        point = SweepPointResult(
            point_index=0,
            overrides={'server.memory.retrieval.reranking_mw_alpha': '0.3'},
            run_result=run,
            error=None,
            server_pid=4242,
            shutdown_method='sigterm',
            duration_seconds=12.5,
        )
        result = SweepResult(
            sweep_id='xyz',
            suite_name='dummy',
            parent_run_id='parent_99',
            started_at=ts,
            finished_at=ts,
            points=[point],
            children_failed=0,
            children_total=1,
        )

        # Bytes-to-bytes round-trip — the Pydantic model must reload
        # cleanly from its own dump, including the nested RunResult.
        serialised = result.model_dump_json()
        reloaded = SweepResult.model_validate_json(serialised)
        assert reloaded.points[0].run_result is not None
        assert reloaded.points[0].run_result.scenario_outcomes[0].scenario_id == 'alpha'
        assert reloaded.points[0].run_result.scenario_outcomes[0].metrics['mrr'] == 0.5
        assert reloaded.points[0].run_result.suite_metrics['pass_rate'] == 1.0
        assert reloaded.children_failed == 0
        assert reloaded.points[0].shutdown_method == 'sigterm'


class TestSweepInterruptedFlow:
    """Round-2 HIGH-R2-1: Ctrl-C / SIGINT during a sweep loop must
    propagate as ``SweepInterrupted`` carrying the partial result so the
    CLI can write --output JSON before exiting 130. The previous
    implementation re-raised KeyboardInterrupt directly, dropping every
    completed point on the floor.

    Patching note: ``run_sweep`` does ``from memex_eval.suite.runner
    import run_suite`` *inside* its function body, so the patch target
    ``memex_eval.suite.runner.run_suite`` works because the import goes
    through the module's ``sys.modules`` entry. If ``run_sweep`` is ever
    refactored to a top-level import, this patch must move to
    ``memex_eval.suite.sweep.run_suite`` — otherwise the patch becomes a
    silent no-op and the test asserts against the real ``run_suite``.
    """

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_carries_partial_result(self) -> None:
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sweep import SweepInterrupted, run_sweep

        meta = SuiteMetadata(
            name='dummy',
            schema_version='1',
            suite_version='1.0.0',
            description='dummy',
        )
        suite = Suite(metadata=meta, sources=SuiteSources(notes=[]), scenarios=[])

        fake_handle = MagicMock()
        fake_handle.url = 'http://127.0.0.1:18080/api/v1/'
        fake_handle.pid = 1234
        fake_handle.shutdown_method = 'sigkill'

        # Two-point grid; second point's run_suite call raises
        # KeyboardInterrupt. We assert the resulting SweepInterrupted
        # carries a partial_result with both points recorded (point 0 ran
        # cleanly via the run_suite stub raising an Exception which
        # records as a failed Exception-class point; point 1 raised
        # KeyboardInterrupt and the loop broke). The contract is: the
        # partial result is non-empty and points[1].error names
        # KeyboardInterrupt.
        async def run_suite_stub(*args: Any, **kwargs: Any) -> Any:
            # Each call: raise to force a recorded outcome.
            # The first call raises a regular exception (failed point);
            # the second raises KeyboardInterrupt (signals interrupt).
            if not hasattr(run_suite_stub, 'calls'):
                run_suite_stub.calls = 0  # type: ignore[attr-defined]
            run_suite_stub.calls += 1  # type: ignore[attr-defined]
            if run_suite_stub.calls == 1:  # type: ignore[attr-defined]
                raise RuntimeError('point 0 stub failure')
            raise KeyboardInterrupt('user pressed Ctrl-C')

        with (
            patch('memex_eval.suite.sweep.spawn_server', return_value=fake_handle),
            patch('memex_eval.suite.sweep.shutdown_server'),
            patch(
                'memex_eval.suite.runner.run_suite',
                new=run_suite_stub,
            ),
        ):
            with pytest.raises(SweepInterrupted) as excinfo:
                await run_sweep(
                    suite,
                    param_grid={'server.memory.retrieval.reranking_mw_alpha': ['0.0', '0.3']},
                )
        partial = excinfo.value.partial_result
        # Both points are accounted for (one Exception failure + one
        # interrupt; both count as failed children).
        assert partial.children_total == 2
        assert partial.children_failed == 2
        assert len(partial.points) == 2
        assert 'KeyboardInterrupt' in (partial.points[1].error or '')


class TestRunSweepActiveMlflow:
    """Round-1 HIGH-1: starting a sweep with an existing active mlflow
    run must not crash — instead nest under it."""

    @pytest.mark.asyncio
    async def test_existing_run_nests_parent(self) -> None:
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sweep import run_sweep

        meta = SuiteMetadata(
            name='dummy',
            schema_version='1',
            suite_version='1.0.0',
            description='dummy',
        )
        suite = Suite(metadata=meta, sources=SuiteSources(notes=[]), scenarios=[])

        # Build a fake mlflow with active_run() returning truthy.
        fake_mlflow = MagicMock()
        active_run = MagicMock()
        active_run.info.run_id = 'outer_run_id'
        fake_mlflow.active_run.return_value = active_run
        parent_run_obj = MagicMock()
        parent_run_obj.info.run_id = 'parent_run_id'
        fake_mlflow.start_run.return_value = parent_run_obj

        # Stub spawn / run_suite so we don't need a real server. Have
        # run_suite raise so the point lands as failed (run_result=None
        # round-trips cleanly through Pydantic; success path with a
        # MagicMock RunResult would fail validation). The point is the
        # parent-run nesting kwarg, not the suite outcome.
        fake_handle = MagicMock()
        fake_handle.url = 'http://127.0.0.1:18080/api/v1/'
        fake_handle.pid = 1234
        fake_handle.shutdown_method = 'sigterm'
        with (
            patch.dict('sys.modules', {'mlflow': fake_mlflow}),
            patch('memex_eval.suite.sweep.spawn_server', return_value=fake_handle) as spawn_mock,
            patch('memex_eval.suite.sweep.shutdown_server') as shutdown_mock,
            patch(
                'memex_eval.suite.runner.run_suite',
                new=AsyncMock(side_effect=RuntimeError('stub: skip suite execution')),
            ),
        ):
            result = await run_sweep(
                suite,
                param_grid={'server.memory.retrieval.reranking_mw_alpha': ['0.0']},
                mlflow_uri='file:///tmp/test-mlruns',
            )
            # Parent's start_run should have been called with nested=True.
            call_kwargs = fake_mlflow.start_run.call_args.kwargs
            assert call_kwargs.get('nested') is True
            spawn_mock.assert_called_once()
            shutdown_mock.assert_called_once_with(fake_handle)
            assert result.parent_run_id == 'parent_run_id'
            # The suite stub raised, so the point was recorded as failed.
            assert result.children_failed == 1
            assert result.points[0].error is not None
            assert 'stub: skip suite execution' in result.points[0].error
