"""Optional MLflow recorder for benchmark runs.

Mirrors the graceful-degradation pattern from ``memex_core.tracing``:
if MLflow is not installed or not configured, the ``NullRecorder`` is
used instead — every call is a no-op with zero overhead.

Install the optional dependency with::

    uv add memex-eval[mlflow]
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any

from memex_eval.__about__ import __version__

logger = logging.getLogger('memex_eval.recorders.mlflow')


class NullRecorder:
    """No-op recorder used when MLflow is disabled or not installed."""

    def __enter__(self) -> NullRecorder:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def start_run(self, **kwargs: Any) -> None:
        pass

    def log_params(self, params: dict[str, Any]) -> None:
        pass

    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        pass

    def log_artifact(self, local_path: str | Path) -> None:
        pass

    def end_run(self) -> None:
        pass


class MLflowRecorder:
    """Records benchmark results to an MLflow tracking server.

    Requires ``mlflow>=2.18,<3`` (install via ``memex-eval[mlflow]``).
    """

    def __init__(
        self,
        tracking_uri: str,
        experiment_name: str = 'memex-eval',
        run_name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        try:
            import mlflow
        except ImportError as e:
            raise ImportError(
                'MLflow is required for recording. Install with: uv add memex-eval[mlflow]'
            ) from e

        self._mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)

        default_tags: dict[str, str] = {
            'memex.eval.version': __version__,
            'host': platform.node(),
        }
        try:
            import subprocess

            git_branch = (
                subprocess.check_output(
                    ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                .decode()
                .strip()
            )
            git_commit = (
                subprocess.check_output(
                    ['git', 'rev-parse', '--short', 'HEAD'],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                .decode()
                .strip()
            )
            default_tags['git.branch'] = git_branch
            default_tags['git.commit'] = git_commit
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
            pass

        if tags:
            default_tags.update(tags)

        self._run_name = run_name
        self._tags = default_tags
        self._run: Any = None

    def start_run(self, **kwargs: Any) -> None:
        self._run = self._mlflow.start_run(run_name=self._run_name, tags=self._tags, **kwargs)
        logger.info('MLflow run started: %s', self._run.info.run_id)

    def log_params(self, params: dict[str, Any]) -> None:
        self._mlflow.log_params({k: str(v) for k, v in params.items()})

    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        self._mlflow.log_metrics(metrics, step=step)

    def log_artifact(self, local_path: str | Path) -> None:
        self._mlflow.log_artifact(str(local_path))

    def end_run(self) -> None:
        if self._run is not None:
            self._mlflow.end_run()
            logger.info('MLflow run ended: %s', self._run.info.run_id)
            self._run = None

    def __enter__(self) -> MLflowRecorder:
        return self

    def __exit__(self, *args: object) -> None:
        self.end_run()

    @classmethod
    def from_cli_options(
        cls,
        mlflow_uri: str | None,
        mlflow_experiment: str = 'memex-eval',
        mlflow_run_name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> MLflowRecorder | NullRecorder:
        """Construct a recorder from Typer CLI options (with env var fallback)."""
        uri = mlflow_uri or os.environ.get('MLFLOW_TRACKING_URI')
        if not uri:
            return NullRecorder()

        experiment = os.environ.get('MEMEX_EVAL_MLFLOW_EXPERIMENT', mlflow_experiment)
        return cls(
            tracking_uri=uri, experiment_name=experiment, run_name=mlflow_run_name, tags=tags
        )


def get_recorder(
    mlflow_uri: str | None = None,
    mlflow_experiment: str = 'memex-eval',
    mlflow_run_name: str | None = None,
    tags: dict[str, str] | None = None,
) -> MLflowRecorder | NullRecorder:
    """Convenience factory: returns NullRecorder if no URI or MLflow not installed."""
    try:
        return MLflowRecorder.from_cli_options(
            mlflow_uri=mlflow_uri,
            mlflow_experiment=mlflow_experiment,
            mlflow_run_name=mlflow_run_name,
            tags=tags,
        )
    except ImportError:
        logger.warning(
            'MLflow not installed — results will not be recorded. Install with: uv add memex-eval[mlflow]'
        )
        return NullRecorder()
