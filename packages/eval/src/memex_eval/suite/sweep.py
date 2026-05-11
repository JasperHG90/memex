"""Sweep orchestrator — run the same suite N times with knob overrides.

Goal: vary one (or more) ``MemexConfig`` knob across a list of values, run
``run_suite`` for each, log everything to MLflow as a parent run with N
nested child runs. Lets the user plot ``metric.recall_at_10.mean`` vs.
``mw_alpha`` (etc.) to find empirical optima for the design-doc knobs.

Mechanism per sweep point:
1. Allocate a free port (with retry on bind contention).
2. Spawn ``granian memex_core.server:app`` with the override env var,
   stripping the harness's own ``MEMEX_*`` env so a developer's shell
   cannot silently contaminate sweep results.
3. Wait for ``/api/v1/health`` (60s timeout).
4. Open an MLflow child run nested under the sweep's parent run.
5. Drive ``run_suite`` against the spawned server.
6. SIGTERM the server (30s grace), SIGKILL on timeout. A second Ctrl-C
   during the grace wait immediately escalates to SIGKILL so the spawned
   process group is never orphaned.

Local-only: the orchestrator hard-rejects remote ``--server`` URLs because
env-var overrides only take effect at process startup. A long-running
remote server cannot be re-knobbed mid-flight.

Validation happens before any spawn — ``--param`` paths must traverse
``MemexConfig.model_fields``; values must resolve to a numeric / string /
literal leaf; ``SecretStr`` fields are rejected (cannot sweep a secret).
The validator handles ``Annotated[...]`` wrappers and discriminated
unions so paths through inner config sub-trees work.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import logging
import typing
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from memex_eval.suite.base import RunResult, Suite
from memex_eval.suite.server_control import (
    ServerHandle,
    env_var_for_dotted_path,
    shutdown_server,
    spawn_server,
)

logger = logging.getLogger(__name__)


# Default cap on grid size; ``run_sweep(max_points=...)`` overrides.
# 50 points at ~3min/point ≈ 2.5h, which is the longest unattended sweep
# we want to allow without an explicit opt-in. A 4-knob × 5-values grid
# is 5**4 = 625 points (~31h at 3min/point) — well past this cap, by
# design: the user must say ``--max-points 625`` to consent to that
# wall-clock spend.
DEFAULT_MAX_POINTS = 50


class SweepValidationError(ValueError):
    """A --param input failed validation before any sweep point ran."""


class SweepNotSupportedRemote(ValueError):
    """Sweep was requested against a non-local server URL."""


class SweepInterrupted(BaseException):
    """Raised when ``run_sweep`` is interrupted by Ctrl-C / SIGTERM mid-loop.

    Carries the partial ``SweepResult`` so the CLI (and library callers)
    can still surface what completed before the interrupt. Inherits from
    ``BaseException`` rather than ``Exception`` to match
    ``KeyboardInterrupt``'s propagation discipline — broad
    ``except Exception`` blocks won't accidentally swallow it.
    """

    def __init__(self, partial_result: 'SweepResult') -> None:
        super().__init__(
            f'Sweep interrupted after {partial_result.children_total} points '
            f'({partial_result.children_failed} failed).'
        )
        self.partial_result = partial_result


class SweepPointResult(BaseModel):
    """One row of a sweep table — the (override values × suite metrics)
    tuple a downstream plot/report consumes."""

    point_index: int
    overrides: dict[str, str]
    run_result: RunResult | None = None
    error: str | None = None
    server_pid: int | None = None
    shutdown_method: str
    duration_seconds: float


class SweepResult(BaseModel):
    """Sweep-level result: parent-run id + per-point children + aggregate
    diagnostics. Persisted as a JSON artifact under the parent run."""

    sweep_id: str
    suite_name: str
    parent_run_id: str | None = None
    started_at: dt.datetime
    finished_at: dt.datetime
    points: list[SweepPointResult] = Field(default_factory=list)
    children_failed: int = 0
    children_total: int = 0


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
    sweep a secret), reject paths that bottom out in a non-leaf field.

    Handles ``Annotated[...]`` wrappers and discriminated ``Union[...]``
    arms so paths through nested config sub-trees (e.g. metastore /
    embedding backends) validate correctly.
    """

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
                # Acceptable leaf types: int / float / bool / str / Literal / Optional thereof.
                if not _is_sweepable_leaf(annotation):
                    raise SweepValidationError(
                        f'--param path {path!r} leaf type {annotation!r} is not '
                        f'sweepable (need int / float / bool / str / Literal).'
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


def _unwrap_annotated(annotation: Any) -> Any:
    """If ``annotation`` is ``Annotated[T, ...]``, return ``T``. Otherwise
    return the annotation unchanged. Handles arbitrary nesting depth."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _is_secret_type(annotation: Any) -> bool:
    """True iff ``annotation`` resolves to ``SecretStr`` (raw, in
    ``Optional[SecretStr]``, ``Annotated[SecretStr, ...]``, or any
    combination)."""
    from pydantic import SecretStr

    annotation = _unwrap_annotated(annotation)
    if annotation is SecretStr:
        return True
    args = typing.get_args(annotation)
    if args:
        return any(_is_secret_type(a) for a in args if a is not type(None))
    return False


def _is_sweepable_leaf(annotation: Any) -> bool:
    """True iff the leaf type is something we can pass via env var.

    Accepts: ``int`` / ``float`` / ``bool`` / ``str`` / any ``Literal[...]``
    arm, plus ``Optional[X]``, ``X | None``, and ``Annotated[X, ...]`` of
    any of the above.
    """
    base_types = {int, float, bool, str}
    annotation = _unwrap_annotated(annotation)
    if annotation in base_types:
        return True
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        # Literal['a', 'b', 'c'] — every value is a string-ish constant
        # the user can express via env var.
        return bool(args)
    if args:
        # Union / X | None — accept if any arm is sweepable.
        return any(_is_sweepable_leaf(a) for a in args if a is not type(None))
    return False


def _resolve_model_class(
    annotation: Any, *, _depth: int = 0, _max_depth: int = 8
) -> type[Any] | None:
    """Return a concrete Pydantic-model class behind ``annotation``,
    unwrapping ``Annotated[...]`` and ``Union[...]`` (including
    discriminated unions, optionally nested) where needed. Returns None
    when the annotation isn't model-shaped on any arm.

    Recurses through nested ``Annotated`` / ``Union`` / ``Optional``
    structures (e.g. ``Optional[Annotated[Union[A, B], Field(...)]]``)
    up to ``_max_depth`` levels.

    Discriminated-union ambiguity: if the annotation resolves to a Union
    with **multiple** ``BaseModel`` arms (e.g.
    ``Union[OnnxBackend, LitellmBackend]`` keyed on a discriminator
    field), this function picks the first arm and emits a
    ``logger.warning`` naming all arms so the operator knows which arm's
    field schema validation actually walked. The next segment of the
    path may resolve in the picked arm but not in the un-picked ones —
    that's a known limitation of static path validation against runtime
    discriminated unions.
    """
    if _depth > _max_depth:
        return None
    annotation = _unwrap_annotated(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    # Walk Union / Optional / nested Annotated arms recursively. Collect
    # every arm that resolves to a BaseModel so we can warn on ambiguity
    # (multiple model arms = discriminated union or plain Union[A, B]).
    resolved_arms: list[type[Any]] = []
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        resolved = _resolve_model_class(arg, _depth=_depth + 1, _max_depth=_max_depth)
        if resolved is not None:
            resolved_arms.append(resolved)
    if not resolved_arms:
        return None
    if len(resolved_arms) > 1:
        # Fire whenever multiple model arms collapse to one — the
        # ambiguity exists at every level a Union of models appears
        # (top-level field annotation OR a nested annotation reached via
        # recursion through ``Optional[Annotated[Union[...]]]``).
        # ``validate_param_paths_against_config`` walks one path-segment
        # at a time, so the warning fires at most once per ambiguous
        # segment, naming every arm so the operator can spot the
        # discriminator surface (e.g. embedding-backend types).
        logger.warning(
            'sweep: --param path traverses a discriminated/Union annotation '
            'with %d Pydantic-model arms (%s); validating against the first '
            "arm only. If your path's leaf only exists on a non-first arm, "
            'override-validation may incorrectly accept or reject it.',
            len(resolved_arms),
            ', '.join(arm.__name__ for arm in resolved_arms),
        )
    return resolved_arms[0]


def _is_local_url(server_url: str) -> bool:
    """True for localhost / 127.0.0.1 / ::1 / [::1] — sweep is local-only."""
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


def _experiment_name(
    suite_name: str,
    knobs: list[str],
    started_at: dt.datetime,
    explicit: str | None,
) -> str:
    """Compute the experiment name used by BOTH the parent and child
    runs. Deterministic on (suite, knobs, month) so children land in the
    same experiment as their parent. ``explicit`` wins when set."""
    if explicit:
        return explicit
    yyyymm = started_at.strftime('%Y%m')
    knob_token = '+'.join(sorted(knobs)).replace('.', '_')[:60]
    return f'memex-sweep-{suite_name}-{knob_token}-{yyyymm}'


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
    max_points: int = DEFAULT_MAX_POINTS,
) -> SweepResult:
    """Run ``suite`` once per sweep point. Each point spawns a fresh
    server with its env overrides, opens a nested MLflow child run, drives
    ``run_suite``, and shuts the server down. The parent run holds the
    sweep metadata (sweep id, knob list, count of failed children).

    Returns the aggregate ``SweepResult``. The MLflow parent-run id is
    populated when ``mlflow_uri`` is set (else None — sweep still runs,
    just without the longitudinal log).

    Notes:
    - ``server_url``'s host portion is checked for local-vs-remote. Port
      gets overridden per point.
    - ``suite_run_kwargs`` are forwarded to ``run_suite`` per child
      (judge_model, replicates, scenario_ids, groups, etc.).
    - ``max_points`` caps the Cartesian grid size to prevent runaway
      sweeps. Pass a larger value (or ``DEFAULT_MAX_POINTS`` × 10) only
      when you've sized the sweep deliberately.
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
    if len(grid) > max_points:
        raise SweepValidationError(
            f'Sweep grid has {len(grid)} points (cartesian product of '
            f'{ {k: len(v) for k, v in param_grid.items()} }), exceeding the '
            f'safety cap of {max_points}. Re-run with --max-points >={len(grid)} '
            f'if intentional. A typical point takes 1-5 minutes; '
            f'{len(grid)} points ≈ {len(grid) * 3 / 60:.1f}h wall time.'
        )
    sweep_id = uuid.uuid4().hex[:12]
    started_at = dt.datetime.now(dt.timezone.utc)
    sweep_label = sweep_label or f'sweep-{sweep_id}'
    suite_run_kwargs = dict(suite_run_kwargs or {})

    # Compute the experiment name ONCE so parent and children land in the
    # same experiment. The MLflow Compare view requires this.
    experiment = _experiment_name(
        suite.metadata.name,
        list(param_grid.keys()),
        started_at,
        mlflow_experiment,
    )

    # Open the parent run if MLflow is configured.
    parent_run_id: str | None = None
    parent_ctx: Any | None = None
    mlflow: Any | None = None
    if mlflow_uri:
        try:
            import mlflow as _mlflow

            mlflow = _mlflow
            mlflow.set_tracking_uri(mlflow_uri)
            mlflow.set_experiment(experiment)
            # If the user already has an active run (e.g. they're calling
            # run_sweep from inside their own MLflow harness), nest under
            # it rather than crash. mlflow.start_run() without
            # ``nested=True`` raises in that case.
            already_active = mlflow.active_run() is not None
            if already_active:
                logger.info(
                    'sweep: existing MLflow run is active; opening parent run as nested under it.'
                )
            parent_ctx = mlflow.start_run(
                run_name=sweep_label,
                tags={
                    'sweep.id': sweep_id,
                    'sweep.suite': suite.metadata.name,
                    'sweep.knobs': ','.join(sorted(param_grid.keys())),
                    'sweep.points_planned': str(len(grid)),
                },
                nested=already_active,
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
    interrupted = False
    interrupt_signal: BaseException | None = None

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
                        experiment_name=experiment,  # SAME as parent
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
            except BaseException as exc:
                # BaseException covers KeyboardInterrupt — the finally
                # below shuts down the spawn cleanly even on Ctrl-C.
                error = f'{type(exc).__name__}: {exc}'
                children_failed += 1
                if not isinstance(exc, Exception):
                    # Real KeyboardInterrupt / SystemExit — record this
                    # point as failed, run the per-point finally for
                    # spawn shutdown, then re-raise as ``SweepInterrupted``
                    # carrying the partial result so the CLI can write
                    # the --output JSON before exiting 130.
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
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            shutdown_server(handle)
                    interrupt_signal = exc
                    interrupted = True
                    break
                logger.exception('sweep[%d]: failed', i)
            finally:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        shutdown_server(handle)
            if not interrupted:
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
    result = SweepResult(
        sweep_id=sweep_id,
        suite_name=suite.metadata.name,
        parent_run_id=parent_run_id,
        started_at=started_at,
        finished_at=finished_at,
        points=points,
        children_failed=children_failed,
        children_total=len(grid),
    )
    if interrupted:
        raise SweepInterrupted(result) from interrupt_signal
    return result


__all__ = [
    'DEFAULT_MAX_POINTS',
    'SweepInterrupted',
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
