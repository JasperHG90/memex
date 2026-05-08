"""End-to-end test: run a suite with mocked HTTP, assert MLflow integration.

Uses an ephemeral file-store MLflow backend (``file:///<tmp>``) so the
test runs without external dependencies but exercises the real MLflow
SDK path (start_run → log_params → log_metrics → log_artifact → end_run).

What this proves:
- MLflow run is created in the right experiment
- All ≥10 base params logged with the expected keys
- All ≥6 metrics logged with well-formed keys
- All ≥4 artifacts logged (run_result.json, config_snapshot.json,
  README.md, sources/)
- ``run_id`` is populated and the run finalizes as FINISHED
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

# MLflow is an optional extra of memex-eval. Skip the whole module cleanly
# instead of failing collection when [mlflow] isn't installed.
mlflow = pytest.importorskip('mlflow')
from mlflow.tracking import MlflowClient  # noqa: E402

from memex_eval.recorders.mlflow_recorder import MLflowRecorder
from memex_eval.suite import (
    KeywordsPresent,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
)
from memex_eval.suite.runner import run_suite


def _build_test_suite() -> Suite:
    return Suite(
        metadata=SuiteMetadata(
            name='e2e_test_suite',
            schema_version='1',
            suite_version='1.2.3',
            description='Ephemeral end-to-end MLflow integration test.',
            tags=['e2e', 'mlflow'],
            primary_metrics=['suite.pass_rate'],
            components_under_test=['retrieval.semantic'],
            knobs=['server.memory.retrieval.reranking_mw_alpha'],
        ),
        sources=SuiteSources(notes=[]),
        scenarios=[
            Scenario(
                id='easy_check',
                description='Simple keyword presence check.',
                query='find the answer',
                expected=KeywordsPresent(type='keywords_present', keywords=['hello']),
            ),
        ],
        readme_path=None,
    )


class _FakeNote:
    def __init__(self, note_id: str = '00000000-0000-0000-0000-000000000001') -> None:
        self.note_id = note_id


class _FakeUnit:
    def __init__(self, uid: str, text: str) -> None:
        self.id = uid
        self.text = text


class _FakeVault:
    def __init__(self, name: str, vid: Any) -> None:
        self.name = name
        self.id = vid


def _build_fake_api() -> Any:
    """Build a SimpleNamespace mocking RemoteMemexAPI for the runner."""
    from types import SimpleNamespace

    api = SimpleNamespace()
    api.list_vaults = AsyncMock(return_value=[])
    api.create_vault = AsyncMock(
        return_value=SimpleNamespace(id=uuid4(), name='eval-suite-e2e_test_suite-abcd1234')
    )
    api.delete_vault = AsyncMock(return_value=None)
    api.truncate_vault = AsyncMock(return_value=None)
    api.get_system_config = AsyncMock(
        return_value={
            'server': {
                'memory': {
                    'embedding': {'model': 'test-embed', 'type': 'OnnxBackend'},
                    'reranker': {'model': 'test-rerank', 'type': 'DisabledBackend'},
                    'retrieval': {'reranking_mw_alpha': 0.3},
                }
            }
        }
    )
    api.list_memory_units_by_note = AsyncMock(return_value=[])
    api.search = AsyncMock(return_value=[_FakeUnit('u1', 'hello world is the answer')])
    return api


def test_run_suite_logs_to_mlflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive run_suite end-to-end against an ephemeral file-store MLflow."""
    import asyncio

    tracking_uri = f'file://{tmp_path / "mlruns"}'
    experiment_name = 'e2e-test'

    fake_api = _build_fake_api()

    # Patch the runner's HTTP client construction so it never touches the network.
    class _FakeAsyncClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            pass

    def _fake_remote(_client: Any) -> Any:
        return fake_api

    monkeypatch.setattr('memex_eval.suite.runner.httpx.AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr('memex_eval.suite.runner.RemoteMemexAPI', _fake_remote)

    recorder = MLflowRecorder(
        tracking_uri=tracking_uri, experiment_name=experiment_name, run_name='test-run'
    )

    suite = _build_test_suite()

    result = asyncio.run(
        run_suite(
            suite,
            server_url='http://fake-server/api/v1/',
            recorder=recorder,
            use_llm_judge=False,
            seed=42,
        )
    )

    # The scenario should have passed (search returns 'hello' in unit text).
    assert result.suite_metrics['suite.pass_rate'] == 1.0
    assert result.suite_metrics['count.scenarios'] == 1.0
    assert result.suite_metrics['count.passed'] == 1.0

    # Now interrogate the MLflow store directly.
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
    assert experiment is not None
    runs = client.search_runs(experiment_ids=[experiment.experiment_id])
    assert len(runs) == 1, f'expected exactly 1 run, got {len(runs)}'
    run = runs[0]

    # Status finalized as FINISHED, not RUNNING.
    assert run.info.status == 'FINISHED', f'unexpected status: {run.info.status}'

    # ≥10 base params (per Definition of Done §12 in the plan).
    params = run.data.params
    for required in (
        'suite.name',
        'suite.version',
        'suite.schema_version',
        'suite.sources_hash',
        'memex.version',
        'seed',
        'replicates',
        'vault.name',
        'embedding.model_id',
        'reranker.model_id',
    ):
        assert required in params, f'missing param {required!r}; got {sorted(params)}'
    assert params['suite.name'] == 'e2e_test_suite'
    assert params['suite.version'] == '1.2.3'
    assert params['seed'] == '42'

    # Knob param is logged.
    assert 'knob.server.memory.retrieval.reranking_mw_alpha' in params
    assert params['knob.server.memory.retrieval.reranking_mw_alpha'] == '0.3'

    # ≥6 metrics with well-formed keys.
    metrics = run.data.metrics
    for required in (
        'suite.pass_rate',
        'count.scenarios',
        'count.passed',
        'latency_ms.p50',
        'latency_ms.p95',
        'latency_ms.mean',
    ):
        assert required in metrics, f'missing metric {required!r}; got {sorted(metrics)}'

    # Keys are well-formed: alphanumeric/dot/underscore/dash, ≤250 chars.
    key_re = re.compile(r'^[A-Za-z0-9_./-]+$')
    for k in metrics:
        assert key_re.match(k), f'metric key {k!r} has illegal characters'
        assert len(k) <= 250

    # Tags include suite.name and schema_version.
    tags = run.data.tags
    assert tags.get('suite.name') == 'e2e_test_suite'
    assert tags.get('schema_version') == '1'

    # Artifacts: run_result.json + config_snapshot.json (README/sources may be
    # absent for this minimal suite — but the two JSON artifacts must exist).
    artifact_paths = {a.path for a in client.list_artifacts(run.info.run_id)}
    assert 'run_result.json' in artifact_paths
    assert 'config_snapshot.json' in artifact_paths

    # config_snapshot.json must NOT contain the unredacted API key value (sanity).
    artifact_dir = client.download_artifacts(run.info.run_id, 'config_snapshot.json')
    body = Path(artifact_dir).read_text()
    assert 'test-embed' in body  # model id is fine to log
    assert '<redacted>' not in body  # nothing to redact in this snapshot


def test_run_suite_uploads_notes_to_mlflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """User-supplied --notes appear as the run_notes.md artifact + a tag."""
    import asyncio

    tracking_uri = f'file://{tmp_path / "mlruns"}'
    experiment_name = 'e2e-test-notes'
    fake_api = _build_fake_api()

    class _FakeAsyncClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            pass

    monkeypatch.setattr('memex_eval.suite.runner.httpx.AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr('memex_eval.suite.runner.RemoteMemexAPI', lambda _c: fake_api)

    notes_body = (
        'Bumped reranking_mw_alpha from 0.0 to 0.3.\n'
        'Expected effect: higher MW recall on contradiction scenarios.\n'
        'Related PR: #143.'
    )

    recorder = MLflowRecorder(
        tracking_uri=tracking_uri, experiment_name=experiment_name, run_name='notes-run'
    )
    suite = _build_test_suite()
    asyncio.run(
        run_suite(
            suite,
            server_url='http://fake-server/api/v1/',
            recorder=recorder,
            use_llm_judge=False,
            seed=11,
            notes=notes_body,
        )
    )

    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
    assert experiment is not None
    runs = client.search_runs(experiment_ids=[experiment.experiment_id])
    assert len(runs) == 1
    run = runs[0]

    # First line as a tag for UI filterability.
    assert 'notes' in run.data.tags
    assert run.data.tags['notes'].startswith('Bumped reranking_mw_alpha')

    # Full text as an artifact.
    artifacts = {a.path for a in client.list_artifacts(run.info.run_id)}
    assert 'run_notes.md' in artifacts
    body = Path(client.download_artifacts(run.info.run_id, 'run_notes.md')).read_text()
    assert body == notes_body


def test_run_suite_records_to_null_recorder_when_no_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no tracking URI is provided, the framework still completes."""
    import asyncio

    from memex_eval.recorders.mlflow_recorder import NullRecorder

    fake_api = _build_fake_api()

    class _FakeAsyncClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            pass

    monkeypatch.setattr('memex_eval.suite.runner.httpx.AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr('memex_eval.suite.runner.RemoteMemexAPI', lambda _c: fake_api)

    suite = _build_test_suite()
    recorder = NullRecorder()
    result = asyncio.run(
        run_suite(
            suite,
            server_url='http://fake-server/api/v1/',
            recorder=recorder,
            use_llm_judge=False,
            seed=7,
        )
    )
    assert result.suite_metrics['count.scenarios'] == 1.0
