"""Unit tests for the sweep harness. Heavy integration (real subprocess
spawn + Postgres) lives in ``test_server_control_integration.py`` gated
behind ``MEMEX_RUN_HEAVY_INTEGRATION=1``."""

from __future__ import annotations

import pytest

from memex_eval.suite.sweep import (
    SweepNotSupportedRemote,
    SweepValidationError,
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
