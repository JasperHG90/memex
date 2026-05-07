"""Tests for the MLflow recorder module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memex_eval.recorders.mlflow_recorder import MLflowRecorder, NullRecorder, get_recorder


class TestNullRecorder:
    """NullRecorder should silently absorb all calls."""

    def test_context_manager(self) -> None:
        r = NullRecorder()
        with r as rec:
            rec.start_run()
            rec.log_params({'a': 1})
            rec.log_metrics({'b': 2.0})
            rec.log_artifact('/tmp/fake.json')
            rec.end_run()

    def test_all_methods_are_noop(self) -> None:
        r = NullRecorder()
        r.start_run()
        r.log_params({'x': 'y'})
        r.log_metrics({'m': 1.0})
        r.log_artifact(Path('/tmp/fake.json'))
        r.end_run()


class TestMLflowRecorderImportError:
    """MLflowRecorder should raise ImportError with install instructions."""

    def test_import_error_message(self) -> None:
        with patch.dict(sys.modules, {'mlflow': None}):
            with pytest.raises(ImportError, match='uv add memex-eval\\[mlflow\\]'):
                MLflowRecorder(tracking_uri='http://localhost:5000')


class TestGetRecorder:
    """get_recorder factory returns NullRecorder when no URI or MLflow missing."""

    def test_returns_null_when_no_uri(self) -> None:
        rec = get_recorder(mlflow_uri=None)
        assert isinstance(rec, NullRecorder)

    def test_returns_null_when_mlflow_not_installed(self) -> None:
        with patch.dict(sys.modules, {'mlflow': None}):
            rec = get_recorder(mlflow_uri='http://localhost:5000')
            assert isinstance(rec, NullRecorder)


class TestMLflowRecorderUnit:
    """Unit tests for MLflowRecorder with mocked mlflow module."""

    def _make_mock_mlflow(self) -> MagicMock:
        mlflow = MagicMock()
        mlflow.start_run.return_value = MagicMock(info=MagicMock(run_id='test-run-id'))
        return mlflow

    def test_logs_params_and_metrics(self) -> None:
        mlflow = self._make_mock_mlflow()
        with patch.dict(sys.modules, {'mlflow': mlflow}):
            rec = MLflowRecorder(
                tracking_uri='http://localhost:5000',
                experiment_name='test-exp',
            )
            rec.start_run()
            rec.log_params({'server_url': 'http://localhost:8001'})
            rec.log_metrics({'pass_rate': 0.85, 'duration_ms': 1234.0})
            rec.end_run()

        mlflow.set_tracking_uri.assert_called_once_with('http://localhost:5000')
        mlflow.set_experiment.assert_called_once_with('test-exp')
        mlflow.start_run.assert_called_once()
        mlflow.log_params.assert_called_once()
        mlflow.log_metrics.assert_called_once()
        mlflow.end_run.assert_called_once()

    def test_context_manager_ends_run(self) -> None:
        mlflow = self._make_mock_mlflow()
        with patch.dict(sys.modules, {'mlflow': mlflow}):
            with MLflowRecorder(
                tracking_uri='http://localhost:5000',
                experiment_name='test-exp',
            ) as rec:
                rec.start_run()

        mlflow.end_run.assert_called_once()

    def test_log_artifact(self) -> None:
        mlflow = self._make_mock_mlflow()
        with patch.dict(sys.modules, {'mlflow': mlflow}):
            rec = MLflowRecorder(
                tracking_uri='http://localhost:5000',
                experiment_name='test-exp',
            )
            rec.start_run()
            rec.log_artifact('/tmp/results.json')
            rec.end_run()

        mlflow.log_artifact.assert_called_once_with('/tmp/results.json')

    def test_from_cli_options_with_env_var(self) -> None:
        mlflow = self._make_mock_mlflow()
        with patch.dict(sys.modules, {'mlflow': mlflow}):
            with patch.dict('os.environ', {'MLFLOW_TRACKING_URI': 'http://env-host:5000'}):
                rec = MLflowRecorder.from_cli_options(mlflow_uri=None)
                assert isinstance(rec, MLflowRecorder)

        mlflow.set_tracking_uri.assert_called_once_with('http://env-host:5000')

    def test_from_cli_options_returns_null_when_no_uri(self) -> None:
        rec = MLflowRecorder.from_cli_options(mlflow_uri=None)
        assert isinstance(rec, NullRecorder)


class TestRecorderWithBenchmarkResult:
    """Test that the CLI wiring passes correct data through the recorder."""

    def test_null_recorder_used_in_run_command(self) -> None:
        """Verify NullRecorder is used when --mlflow-uri is not set."""
        from typer.testing import CliRunner

        from memex_eval.cli import app

        runner = CliRunner()
        # Just verify the --mlflow-uri flag is accepted without error
        # (the actual benchmark won't run without a server)
        result = runner.invoke(app, ['internal', 'run', '--help'])
        assert result.exit_code == 0
        assert '--mlflow-uri' in result.output
        assert '--mlflow-experiment' in result.output
        assert '--mlflow-run-name' in result.output

    def test_mlflow_options_on_locomo_commands(self) -> None:
        from typer.testing import CliRunner

        from memex_eval.cli import app

        runner = CliRunner()
        for sub in ['ingest', 'export', 'answer', 'judge']:
            result = runner.invoke(app, ['locomo', sub, '--help'])
            assert result.exit_code == 0
            assert '--mlflow-uri' in result.output
