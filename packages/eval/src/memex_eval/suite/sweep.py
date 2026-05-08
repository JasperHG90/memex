"""Sweep harness — N runs of one suite at different knob values.

Each sweep point spawns a fresh server subprocess with the override env,
runs the suite against it, and logs to MLflow as a nested child run.
Local-only — see ``SweepNotSupportedRemote``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import uuid
from typing import Any

from memex_eval.suite.loader import load_suite
from memex_eval.suite.runner import run_suite
from memex_eval.suite.server_control import SpawnedServer, is_localhost

logger = logging.getLogger('memex_eval.suite.sweep')


class SweepNotSupportedRemote(Exception):
    """Raised when a sweep is requested against a non-localhost server."""


def _parse_param_spec(spec: str) -> tuple[str, list[str]]:
    """``KEY=v1,v2,v3`` → (KEY, [v1, v2, v3])."""
    if '=' not in spec:
        raise ValueError(f'--param must be KEY=v1,v2,...; got {spec!r}')
    key, raw_values = spec.split('=', 1)
    values = [v.strip() for v in raw_values.split(',') if v.strip()]
    if not values:
        raise ValueError(f'--param {spec!r} has no values')
    return key.strip(), values


def _env_var_for(dotted_path: str) -> str:
    return 'MEMEX_' + dotted_path.upper().replace('.', '__')


def sweep_suite(
    suite_name: str,
    params: list[str],
    *,
    server: str | None = None,
    mlflow_uri: str | None = None,
    mlflow_experiment: str | None = None,
    use_llm_judge: bool = True,
    judge_model: str | None = None,
    replicates: int = 1,
    seed: int | None = None,
    answer_mode: str | None = None,
    startup_timeout_s: float = 60.0,
    graceful_shutdown_s: float = 30.0,
) -> dict[str, Any]:
    """Run a knob sweep over ``params`` (cartesian product if multiple).

    Returns a summary dict with ``parent_run_id``, ``children``, etc.
    """
    if server and not is_localhost(server):
        raise SweepNotSupportedRemote(
            'Sweep requires a localhost server (env-var overrides only take '
            'effect at process startup, not on a running remote server). '
            f'Got --server={server!r}.'
        )

    parsed = [_parse_param_spec(p) for p in params]
    param_names = [k for k, _ in parsed]
    param_values = [vs for _, vs in parsed]
    points = list(itertools.product(*param_values))
    if len(points) > 20:
        raise ValueError(
            f'Sweep cap of 20 points exceeded ({len(points)}); split into multiple sweeps'
        )

    suite = load_suite(suite_name)
    if answer_mode:
        suite.metadata.default_answer_mode = answer_mode

    sweep_id = uuid.uuid4().hex
    experiment = (
        mlflow_experiment
        or f'memex-sweep-{suite.name}-{"-".join(k.split(".")[-1] for k in param_names)}'
    )

    # Open MLflow parent run
    from memex_eval.recorders.mlflow_recorder import NullRecorder, get_recorder

    parent_recorder = get_recorder(mlflow_uri=mlflow_uri, mlflow_experiment=experiment)
    parent_run_id: str | None = None
    if not isinstance(parent_recorder, NullRecorder):
        try:
            parent_recorder.start_run(run_name=f'sweep-{sweep_id}')
            parent_recorder.log_params(
                {
                    'sweep.id': sweep_id,
                    'sweep.suite': suite.name,
                    'sweep.params': ','.join(param_names),
                    'sweep.points_total': str(len(points)),
                }
            )
            parent_run_id = getattr(parent_recorder, 'run_id', None)
        except Exception as e:  # noqa: BLE001
            logger.warning('Could not open MLflow parent run: %s', e)

    children_failed = 0
    children_results: list[dict[str, Any]] = []

    try:
        for point_index, point in enumerate(points):
            env_overlay = {_env_var_for(k): v for k, v in zip(param_names, point, strict=True)}
            override_pairs = dict(zip(param_names, point, strict=True))
            logger.info('Sweep point %d/%d: %s', point_index + 1, len(points), override_pairs)

            child_run_name = f'point-{point_index:02d}'
            child_extra_params = {
                'sweep.id': sweep_id,
                'sweep.point_index': str(point_index),
            }
            if parent_run_id:
                child_extra_params['mlflow.parentRunId'] = parent_run_id

            try:
                with SpawnedServer(
                    env_overlay=env_overlay,
                    startup_timeout_s=startup_timeout_s,
                    graceful_shutdown_s=graceful_shutdown_s,
                ) as srv:
                    child_recorder = get_recorder(
                        mlflow_uri=mlflow_uri,
                        mlflow_experiment=experiment,
                        mlflow_run_name=child_run_name,
                    )
                    result = asyncio.run(
                        run_suite(
                            suite,
                            server_url=srv.server_url,
                            config_overrides=override_pairs,
                            judge_model=judge_model,
                            use_llm_judge=use_llm_judge,
                            replicates=replicates,
                            seed=seed,
                            recorder=child_recorder,
                            extra_params=child_extra_params,
                            extra_tags={'sweep.id': sweep_id},
                        )
                    )
                shutdown_method = srv.stop() if False else 'graceful'  # already done by ctx
                children_results.append(
                    {
                        'point_index': point_index,
                        'overrides': override_pairs,
                        'pass_rate': result.overall_pass_rate,
                        'shutdown_method': shutdown_method,
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.warning('Sweep point %d failed: %s', point_index, e)
                children_failed += 1
                children_results.append(
                    {
                        'point_index': point_index,
                        'overrides': override_pairs,
                        'error': str(e),
                    }
                )
                with contextlib.suppress(Exception):
                    if hasattr(parent_recorder, 'set_tag'):
                        parent_recorder.set_tag('sweep.partial_failure', 'true')
    except KeyboardInterrupt:
        logger.warning('Sweep interrupted by user — finalizing parent run as KILLED')
        if not isinstance(parent_recorder, NullRecorder):
            with contextlib.suppress(Exception):
                if hasattr(parent_recorder, '_mlflow'):
                    parent_recorder._mlflow.end_run(status='KILLED')
        raise
    finally:
        if not isinstance(parent_recorder, NullRecorder):
            with contextlib.suppress(Exception):
                parent_recorder.log_params(
                    {
                        'sweep.children_total': str(len(points)),
                        'sweep.children_failed': str(children_failed),
                    }
                )
                parent_recorder.end_run()

    return {
        'sweep.id': sweep_id,
        'parent_run_id': parent_run_id,
        'children': children_results,
        'children_failed': children_failed,
    }


__all__ = ['sweep_suite', 'SweepNotSupportedRemote']
