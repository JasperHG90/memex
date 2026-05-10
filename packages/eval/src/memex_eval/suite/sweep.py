"""Sweep orchestrator — run the same suite N times with knob overrides.

Goal: vary one (or more) ``MemexConfig`` knob across a list of values, run
``run_suite`` for each, log everything to MLflow as a parent run with N
nested child runs. Lets the user plot ``metric.recall_at_10.mean`` vs.
``mw_alpha`` (etc.) to find empirical optima for the design-doc knobs.

Mechanism per sweep point:
1. Allocate a free port.
2. Spawn ``granian memex_core.server:app`` with the override env var.
3. Wait for ``/api/v1/health`` (60s timeout).
4. Open an MLflow child run nested under the sweep's parent run.
5. Drive ``run_suite`` against the spawned server.
6. SIGTERM the server (30s grace), SIGKILL on timeout.

Local-only: the orchestrator hard-rejects remote ``--server`` URLs because
env-var overrides only take effect at process startup. A long-running
remote server cannot be re-knobbed mid-flight.

Validation happens before any spawn — ``--param`` paths must traverse
``MemexConfig.model_fields``; values must be numeric (the knobs Memex
exposes are all int/float/bool); ``SecretStr`` fields are rejected
(cannot sweep a secret).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import itertools
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from memex_eval.suite.base import Suite
from memex_eval.suite.server_control import (
    ServerHandle,
    env_var_for_dotted_path,
    shutdown_server,
    spawn_server,
)

if TYPE_CHECKING:
    from memex_eval.suite.base import RunResult

logger = logging.getLogger(__name__)


class SweepValidationError(ValueError):
    """A --param input failed validation before any sweep point ran."""


class SweepNotSupportedRemote(ValueError):
    """Sweep was requested against a non-local server URL."""


@dataclasses.dataclass
class SweepPointResult:
    """One row of a sweep table — the (override values × suite metrics)
    tuple a downstream plot/report consumes."""

    point_index: int
    overrides: dict[str, str]
    run_result: 'RunResult | None'
    error: str | None
    server_pid: int | None
    shutdown_method: str
    duration_seconds: float


@dataclasses.dataclass
class SweepResult:
    """Sweep-level result: parent-run id + per-point children + aggregate
    diagnostics. Persisted as a JSON artifact under the parent run."""

    sweep_id: str
    suite_name: str
    parent_run_id: str | None
    started_at: dt.datetime
    finished_at: dt.datetime
    points: list[SweepPointResult]
    children_failed: int
    children_total: int


def parse_param_specs(specs: list[str]) -> dict[str, list[str]]:
    """Parse repeated ``--param key=v1,v2,v3`` into ``{key: [v1, v2, v3]}``.

    Whitespace around comma-separated values is stripped. Empty values
    after stripping raise.
    """
    out: dict[str, list[str]] = {}
    for spec in specs:
        if '=' not in spec:
            raise SweepValidationError(
                f'--param value {spec!r} must be KEY=V1,V2,V3 (got no ``=``).'
            )
        key, raw_values = spec.split('=', 1)
        key = key.strip()
        if not key:
            raise SweepValidationError(f'--param {spec!r} has empty key.')
        values = [v.strip() for v in raw_values.split(',')]
        values = [v for v in values if v]
        if not values:
            raise SweepValidationError(f'--param {spec!r} has no values; expected KEY=V1,V2,V3.')
        if key in out:
            raise SweepValidationError(
                f'--param {key!r} specified twice; merge values into one --param.'
            )
        out[key] = values
    if not out:
        raise SweepValidationError('At least one --param KEY=V1,V2,V3 is required.')
    return out


def validate_param_paths_against_config(paths: list[str]) -> None:
    """Walk each dotted path through ``MemexConfig.model_fields``. Reject
    typos (path doesn't resolve), reject ``SecretStr`` leaves (cannot
    sweep a secret), reject paths that bottom out in a non-leaf field."""

    from memex_common.config import MemexConfig

    for path in paths:
        parts = path.split('.')
        # Walk the nested-model tree.
        current_model: type[Any] = MemexConfig
        for i, part in enumerate(parts):
            fields = getattr(current_model, 'model_fields', None)
            if fields is None or part not in fields:
                raise SweepValidationError(
                    f'--param path {path!r} does not resolve: '
                    f'segment {part!r} not in {current_model.__name__}.model_fields.'
                )
            field_info = fields[part]
            annotation = field_info.annotation
            is_last = i == len(parts) - 1
            if is_last:
                # SecretStr leaf? Reject.
                if _is_secret_type(annotation):
                    raise SweepValidationError(
                        f'--param path {path!r} resolves to a SecretStr field; '
                        f'cannot sweep a secret.'
                    )
                # Acceptable leaf types: int / float / bool / str / Optional thereof.
                if not _is_sweepable_leaf(annotation):
                    raise SweepValidationError(
                        f'--param path {path!r} leaf type {annotation!r} is not '
                        f'sweepable (need int / float / bool / str).'
                    )
            else:
                # Intermediate — must be a Pydantic model.
                resolved = _resolve_model_class(annotation)
                if resolved is None:
                    raise SweepValidationError(
                        f'--param path {path!r}: segment {part!r} '
                        f'(type {annotation!r}) is not a nested model.'
                    )
                current_model = resolved


def _is_secret_type(annotation: Any) -> bool:
    """True iff ``annotation`` is ``SecretStr`` or ``SecretStr | None``."""
    from pydantic import SecretStr

    if annotation is SecretStr:
        return True
    args = getattr(annotation, '__args__', None)
    if args is not None:
        return any(arg is SecretStr for arg in args)
    return False


def _is_sweepable_leaf(annotation: Any) -> bool:
    """True iff the leaf type is something we can pass via env var."""
    base_types = {int, float, bool, str}
    if annotation in base_types:
        return True
    args = getattr(annotation, '__args__', None)
    if args is not None:
        # Optional[int], int | None, etc. — accept if any arm is sweepable.
        non_none = [a for a in args if a is not type(None)]
        return any(a in base_types for a in non_none)
    return False


def _resolve_model_class(annotation: Any) -> type[Any] | None:
    """Return the concrete Pydantic-model class behind ``annotation``,
    unwrapping ``Optional`` / discriminated unions where needed.
    Returns None when the annotation isn't model-shaped."""
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    args = getattr(annotation, '__args__', None)
    if args is not None:
        for arg in args:
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _is_local_url(server_url: str) -> bool:
    """True for localhost / 127.0.0.1 / ::1 — sweep is local-only."""
    try:
        host = urlparse(server_url).hostname or ''
    except ValueError:
        return False
    return host in {'localhost', '127.0.0.1', '::1'}


def expand_sweep_grid(param_grid: dict[str, list[str]]) -> list[dict[str, str]]:
    """Cartesian product of param values. ``{a: [1, 2], b: [x, y]}`` →
    ``[{a:1,b:x}, {a:1,b:y}, {a:2,b:x}, {a:2,b:y}]``. Order is stable
    (sorted-keys × itertools.product) so a sweep is reproducible."""
    keys = sorted(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*value_lists)]


def build_env_overrides(point: dict[str, str]) -> dict[str, str]:
    """Translate one sweep point's overrides to MEMEX_* env vars."""
    return {env_var_for_dotted_path(path): value for path, value in point.items()}


async def run_sweep(
    suite: Suite,
    *,
    param_grid: dict[str, list[str]],
    server_url: str = 'http://127.0.0.1:18080/api/v1/',
    mlflow_uri: str | None = None,
    mlflow_experiment: str | None = None,
    sweep_label: str | None = None,
    suite_run_kwargs: dict[str, Any] | None = None,
    log_dir: Path | None = None,
) -> SweepResult:
    """Run ``suite`` once per sweep point. Each point spawns a fresh
    server with its env overrides, opens a nested MLflow child run, drives
    ``run_suite``, and shuts the server down. The parent run holds the
    sweep metadata (sweep id, knob list, count of failed children).

    Returns the aggregate ``SweepResult``. The MLflow parent-run id is
    populated when ``mlflow_uri`` is set (else None — sweep still runs,
    just without the longitudinal log).

    Notes:
    - The ``server_url`` parameter is the shape the suite will see (port
      gets overridden per point); only the host portion is used for the
      local-vs-remote check.
    - ``suite_run_kwargs`` are forwarded to ``run_suite`` per child
      (judge_model, replicates, scenario_ids, groups, etc.).
    """
    from memex_eval.suite.runner import run_suite

    if not _is_local_url(server_url):
        raise SweepNotSupportedRemote(
            f'Sweep is local-only; cannot sweep against {server_url!r}. '
            f'Env-var overrides only apply at server startup, so re-knobbing '
            f'a remote server mid-flight is impossible. '
            f'Use ``memex-eval suite run --server <remote>`` instead (single point).'
        )

    validate_param_paths_against_config(list(param_grid.keys()))
    grid = expand_sweep_grid(param_grid)
    sweep_id = uuid.uuid4().hex[:12]
    started_at = dt.datetime.now(dt.timezone.utc)
    sweep_label = sweep_label or f'sweep-{sweep_id}'
    suite_run_kwargs = dict(suite_run_kwargs or {})

    # Open the parent run if MLflow is configured.
    parent_run_id: str | None = None
    parent_ctx: Any | None = None
    mlflow: Any | None = None
    if mlflow_uri:
        try:
            import mlflow as _mlflow

            mlflow = _mlflow
            mlflow.set_tracking_uri(mlflow_uri)
            yyyymm = started_at.strftime('%Y%m')
            knob_token = '+'.join(sorted(param_grid.keys())).replace('.', '_')[:60]
            experiment = (
                mlflow_experiment or f'memex-sweep-{suite.metadata.name}-{knob_token}-{yyyymm}'
            )
            mlflow.set_experiment(experiment)
            parent_ctx = mlflow.start_run(
                run_name=sweep_label,
                tags={
                    'sweep.id': sweep_id,
                    'sweep.suite': suite.metadata.name,
                    'sweep.knobs': ','.join(sorted(param_grid.keys())),
                    'sweep.points_planned': str(len(grid)),
                },
            )
            parent_run_id = parent_ctx.info.run_id
            mlflow.log_params({f'sweep.values.{k}': ','.join(v) for k, v in param_grid.items()})
        except ImportError:
            logger.warning(
                'mlflow not installed; sweep will run but no parent/child '
                'runs are recorded. Install with: uv add memex-eval[mlflow]'
            )

    points: list[SweepPointResult] = []
    children_failed = 0

    try:
        for i, point in enumerate(grid):
            logger.info(
                'sweep[%d/%d]: %s',
                i + 1,
                len(grid),
                ', '.join(f'{k}={v}' for k, v in point.items()),
            )
            point_started = dt.datetime.now(dt.timezone.utc)
            env_overrides = build_env_overrides(point)
            handle: ServerHandle | None = None
            run_result: RunResult | None = None
            error: str | None = None
            try:
                handle = spawn_server(env_overrides=env_overrides, log_dir=log_dir)
                # Per-child MLflow recorder nested under the parent.
                from memex_eval.recorders.mlflow_recorder import (
                    MLflowRecorder,
                    NullRecorder,
                )

                recorder: MLflowRecorder | NullRecorder
                if mlflow_uri and mlflow is not None:
                    # The runner reads ``mlflow.parentRunId`` from
                    # extra_params and converts to a tag + nested=True.
                    recorder = MLflowRecorder(
                        tracking_uri=mlflow_uri,
                        experiment_name=(
                            mlflow_experiment
                            or f'memex-sweep-{suite.metadata.name}-{started_at.strftime("%Y%m")}'
                        ),
                        run_name=f'{sweep_label}-point-{i:02d}',
                    )
                else:
                    recorder = NullRecorder()

                child_extra_params: dict[str, str] = {f'override.{k}': v for k, v in point.items()}
                if parent_run_id is not None:
                    child_extra_params['mlflow.parentRunId'] = parent_run_id
                child_extra_tags = {
                    'sweep.id': sweep_id,
                    'sweep.point_index': str(i),
                }
                run_result = await run_suite(
                    suite,
                    server_url=handle.url,
                    config_overrides=point,
                    recorder=recorder,
                    extra_params=child_extra_params,
                    extra_tags=child_extra_tags,
                    **suite_run_kwargs,
                )
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                logger.exception('sweep[%d]: failed', i)
                children_failed += 1
            finally:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        shutdown_server(handle)
            point_finished = dt.datetime.now(dt.timezone.utc)
            points.append(
                SweepPointResult(
                    point_index=i,
                    overrides=dict(point),
                    run_result=run_result,
                    error=error,
                    server_pid=handle.pid if handle is not None else None,
                    shutdown_method=(
                        handle.shutdown_method if handle is not None else 'never_spawned'
                    ),
                    duration_seconds=(point_finished - point_started).total_seconds(),
                )
            )
    finally:
        if parent_ctx is not None and mlflow is not None:
            with contextlib.suppress(Exception):
                if children_failed:
                    mlflow.set_tag('sweep.partial_failure', 'true')
                mlflow.set_tag('sweep.children_failed', str(children_failed))
                mlflow.set_tag('sweep.children_total', str(len(grid)))
            with contextlib.suppress(Exception):
                mlflow.end_run()

    finished_at = dt.datetime.now(dt.timezone.utc)
    return SweepResult(
        sweep_id=sweep_id,
        suite_name=suite.metadata.name,
        parent_run_id=parent_run_id,
        started_at=started_at,
        finished_at=finished_at,
        points=points,
        children_failed=children_failed,
        children_total=len(grid),
    )


__all__ = [
    'SweepNotSupportedRemote',
    'SweepPointResult',
    'SweepResult',
    'SweepValidationError',
    'build_env_overrides',
    'expand_sweep_grid',
    'parse_param_specs',
    'run_sweep',
    'validate_param_paths_against_config',
]
