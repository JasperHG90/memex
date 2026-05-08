"""Tests for the recorder kwargs/run_id contract on MLflowRecorder."""

from __future__ import annotations

from typing import Any

import pytest

# Skip cleanly when the optional [mlflow] extra isn't installed.
pytest.importorskip('mlflow')


def _make_recorder(monkeypatch: pytest.MonkeyPatch):
    """Build an MLflowRecorder with the underlying mlflow module monkeypatched."""
    from memex_eval.recorders.mlflow_recorder import MLflowRecorder

    captured: dict[str, Any] = {'start_calls': []}

    class FakeRunInfo:
        run_id = 'fake-run-id'

    class FakeRun:
        info = FakeRunInfo()

    class FakeMlflow:
        @staticmethod
        def set_tracking_uri(uri: str) -> None:
            captured['tracking_uri'] = uri

        @staticmethod
        def set_experiment(name: str) -> None:
            captured['experiment'] = name

        @staticmethod
        def start_run(**kwargs: Any) -> FakeRun:
            captured['start_calls'].append(kwargs)
            return FakeRun()

        @staticmethod
        def end_run(**_kwargs: Any) -> None:
            pass

        @staticmethod
        def log_artifact(*_a: Any, **_k: Any) -> None:
            pass

    rec = MLflowRecorder(
        tracking_uri='file:///tmp/mlflow-test', experiment_name='t', run_name='preset'
    )
    rec._mlflow = FakeMlflow()  # type: ignore[assignment]
    return rec, captured


def test_start_run_accepts_run_name_kwarg_without_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, captured = _make_recorder(monkeypatch)
    # The sweep harness passes run_name=... — must NOT collide with the preset.
    rec.start_run(run_name='sweep-abc')
    assert captured['start_calls'][0]['run_name'] == 'sweep-abc'


def test_start_run_falls_back_to_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, captured = _make_recorder(monkeypatch)
    rec.start_run()
    assert captured['start_calls'][0]['run_name'] == 'preset'


def test_run_id_property_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, _captured = _make_recorder(monkeypatch)
    rec.start_run()
    assert rec.run_id == 'fake-run-id'
    rec.end_run()
    assert rec.run_id is None


def test_start_run_passes_through_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, captured = _make_recorder(monkeypatch)
    rec.start_run(nested=True, run_name='child-1')
    assert captured['start_calls'][0]['nested'] is True
    assert captured['start_calls'][0]['run_name'] == 'child-1'


def test_log_dict_artifact_preserves_name(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, _captured = _make_recorder(monkeypatch)
    paths_logged: list[str] = []

    class FakeMlflow:
        @staticmethod
        def log_artifact(p: str, *args: Any, **kwargs: Any) -> None:
            paths_logged.append(p)

    rec._mlflow = FakeMlflow()  # type: ignore[assignment]
    rec.log_dict_artifact('judge_probe.json', {'model': 'gemini-2.5-pro'})
    assert paths_logged, 'log_artifact should have been called'
    assert paths_logged[0].endswith('judge_probe.json')
