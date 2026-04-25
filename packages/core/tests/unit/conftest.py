import os
import pytest
import tempfile
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

import dspy
from dspy.utils.dummies import DummyLM

from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore


@pytest.fixture(autouse=True)
def setup_unit_test_env():
    """Setup a minimal valid environment for MemexConfig during unit tests."""
    # Create a temporary empty config file to ensure isolation
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
        tmp.write('')
        tmp_path = tmp.name

    with patch.dict(
        os.environ,
        {
            'MEMEX_LOAD_LOCAL_CONFIG': 'false',
            'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
            'MEMEX_CONFIG_PATH': tmp_path,  # Point to empty temp file
            'MEMEX_SERVER__META_STORE__TYPE': 'postgres',
            'MEMEX_SERVER__META_STORE__INSTANCE__HOST': 'localhost',
            'MEMEX_SERVER__META_STORE__INSTANCE__DATABASE': 'dummy',
            'MEMEX_SERVER__META_STORE__INSTANCE__USER': 'dummy',
            'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD': 'dummy',
            'MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL': 'gemini/flash',
        },
    ):
        yield

    # Cleanup
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


@pytest.fixture(autouse=True)
def _reset_inflight_gauges():
    """Per-test reset of `memex_extraction_inflight` + `memex_sync_offload_inflight`
    stage children — both PRE-yield (so this test starts from a known-zero
    state) and POST-yield (so a leak in this test doesn't bleed into the
    next).

    F7 (Phase 3 adversarial fix): the previous version of this fixture lived
    in `test_instrument.py` only and ran post-yield. That left two gaps:

    1. **Per-file scope** — tests in `test_pr2_observability.py` and
       elsewhere that drove the real production wrappers had no reset, so
       a previous gauge increment from `test_instrument.py` could leak
       into them and break `assert observed == [before + 1]` invariants.
    2. **Post-yield only** — a test that ran AFTER another test that
       happened to leave a non-zero gauge would observe the leak in its
       `before` snapshot and pass for the wrong reason.

    Resolution: promote to conftest (covers every unit test in the tree)
    and add a pre-yield reset (every test starts from gauge==0). The
    post-yield reset stays as a defence-in-depth guard against a buggy
    test that exits with the gauge non-zero.
    """
    from memex_core.metrics import EXTRACTION_INFLIGHT, SYNC_OFFLOAD_INFLIGHT

    def _drain(gauge, stages):
        for stage in stages:
            for metric in gauge.collect():
                for sample in metric.samples:
                    if sample.labels.get('stage') == stage and sample.value > 0:
                        # Decrement by the observed value to floor.
                        for _ in range(int(sample.value)):
                            gauge.labels(stage=stage).dec()

    extraction_stages = ('scan', 'refine', 'summarize', 'block_summarize')
    sync_offload_stages = ('rerank', 'embed', 'ner')

    # Pre-yield reset: enter every test from a known-zero gauge state.
    _drain(EXTRACTION_INFLIGHT, extraction_stages)
    _drain(SYNC_OFFLOAD_INFLIGHT, sync_offload_stages)
    try:
        yield
    finally:
        # Post-yield reset: defence in depth. A test that exits with the
        # gauge non-zero (e.g. an unhandled exception inside `_instrument`
        # *before* the `try/finally` decrement could fire) would otherwise
        # leak state into the next test's pre-yield observation.
        _drain(EXTRACTION_INFLIGHT, extraction_stages)
        _drain(SYNC_OFFLOAD_INFLIGHT, sync_offload_stages)


@pytest.fixture(autouse=True)
def _configure_offload_semaphores_default():
    """Initialise sync-offload semaphores so gated to_thread sites are callable.

    In production this happens at server startup (server/__init__.py) before
    warmup. Unit tests don't run startup, so an autouse fixture pre-configures
    with default ServerConfig caps. Tests that need specific caps (e.g.
    test_reranker_respects_concurrency_cap with cap=2) call
    configure_offload_semaphores(cfg) explicitly to override.

    F11 (Phase 3 adversarial fix): the previous version of this fixture only
    set state on entry and left whatever the previous test reconfigured in
    place between teardown and the next setup. Tests that mutate
    `_offload._CFG` (e.g. cap=2 stress tests) could leak state to a
    subsequent test that read `_offload._CFG` before the autouse next ran —
    rare in practice (pytest runs tests serially within a process), but
    real if pytest-xdist or any test-runtime code observed the globals
    between tests.

    Resolution: null the four globals BEFORE re-configuring at setup, and
    null them AFTER yielding so a leaked reference from inside the test
    cannot influence the next test's configuration. Concretely the reset
    sequence is:
        setup:   _CFG=None, semaphores=None  ->  configure_offload_semaphores(default)
        yield
        teardown: _CFG=None, semaphores=None  (next test's autouse reconfigures)
    """
    from memex_common.config import ServerConfig
    from memex_core.memory.retrieval import _offload

    # Pre-yield reset: clear any leaked state from a prior test, then
    # establish fresh defaults so tests start from a known configuration.
    _offload._CFG = None
    _offload._RERANKER_SEMAPHORE = None
    _offload._EMBEDDING_SEMAPHORE = None
    _offload._NER_SEMAPHORE = None
    _offload.configure_offload_semaphores(ServerConfig())
    try:
        yield
    finally:
        # Post-yield reset: defensive — should the next test's autouse run
        # before the previous test releases a captured semaphore reference,
        # the captured reference is still the OLD object and the new test
        # gets a fresh one. Without this, a test that patched _CFG fields
        # after configure (e.g. mutating reranker_call_timeout in place)
        # would leak the patched cfg into the next test.
        _offload._CFG = None
        _offload._RERANKER_SEMAPHORE = None
        _offload._EMBEDDING_SEMAPHORE = None
        _offload._NER_SEMAPHORE = None


@pytest.fixture
def mock_session():
    """Shared AsyncMock for metastore sessions."""
    session = AsyncMock()

    # Default behavior for session.exec().all() and .first()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_result.first.return_value = None
    session.exec.return_value = mock_result

    # session.add and session.delete are synchronous in SQLModel/AsyncSession
    session.add = MagicMock()
    session.delete = MagicMock()

    return session


@pytest.fixture
def mock_metastore(mock_session):
    """Shared MagicMock for AsyncBaseMetaStoreEngine."""
    ms = MagicMock(spec=AsyncBaseMetaStoreEngine)

    # mock session context manager for read ops
    # We need a MagicMock that returns an object with async __aenter__/__aexit__
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    ms.session.return_value = ctx

    # mock session_maker for transactions
    session_factory = MagicMock()
    txn_session = AsyncMock()
    # session.add and session.delete are synchronous
    txn_session.add = MagicMock()
    txn_session.delete = MagicMock()

    session_factory.return_value = txn_session
    ms.session_maker.return_value = session_factory

    return ms


@pytest.fixture
def mock_filestore():
    """Shared MagicMock for BaseAsyncFileStore."""
    fs = MagicMock(spec=BaseAsyncFileStore)
    fs.save = AsyncMock()
    fs.begin_staging = MagicMock()
    fs.commit_staging = AsyncMock()
    fs.rollback_staging = AsyncMock()
    return fs


@pytest.fixture
def mock_config():
    """Mock MemexConfig with standard test values."""
    config = MagicMock()
    config.server.default_active_vault = 'global'
    config.server.default_reader_vault = 'global'
    config.server.memory.extraction.model.model = 'test-model'
    config.server.logging.level = 'WARNING'
    config.server.logging.json_output = False
    return config


@pytest.fixture
def mock_embedding_model():
    """Mock FastEmbedder."""
    model = MagicMock()
    # encode is synchronous, run in executor by core
    model.encode.return_value = [[0.1] * 384]
    return model


@pytest.fixture
def mock_reranking_model():
    """Mock FastReranker."""
    model = MagicMock()
    model.predict.return_value = []
    return model


@pytest.fixture
def mock_ner_model():
    """Mock FastNERModel."""
    model = MagicMock()
    model.extract_entities.return_value = []
    return model


@pytest.fixture
def api(
    mock_metastore,
    mock_filestore,
    mock_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
):
    """Provides a MemexAPI instance with all internal engines patched."""
    from memex_core.api import MemexAPI

    return MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )


@pytest.fixture
def patch_api_engines():
    """Context-managed patch for all MemexAPI engine components."""
    with (
        patch('memex_core.api.dspy'),
        patch('memex_core.api.EntityResolver') as m1,
        patch('memex_core.api.ExtractionEngine') as m2,
        patch('memex_core.api.RetrievalEngine') as m3,
        patch('memex_core.api.NoteSearchEngine') as m4,
        patch('memex_core.api.MemoryEngine') as m5,
        patch('memex_core.api.ReflectionQueueService') as m6,
        patch('memex_core.api.FileContentProcessor') as m7,
    ):
        # Ensure the instances (return_value of the class mock) are AsyncMocks
        m1.return_value = AsyncMock()
        m2.return_value = AsyncMock()
        m3.return_value = AsyncMock()
        m4.return_value = AsyncMock()
        m5.return_value = AsyncMock()
        m6.return_value = AsyncMock()
        m7.return_value = AsyncMock()
        yield


# ---------------------------------------------------------------------------
# LLM mocking infrastructure (P2-07)
# ---------------------------------------------------------------------------


class MockDspyLM:
    """Deterministic mock for ``run_dspy_operation`` in unit tests.

    Usage::

        def test_something(mock_dspy_lm):
            mock_dspy_lm.set_responses([result_obj])
            # ... call code that invokes run_dspy_operation ...
            assert mock_dspy_lm.call_count == 1
    """

    def __init__(self) -> None:
        self.dummy_lm: dspy.LM = DummyLM(
            [{'response': 'mocked'}],
        )
        self._responses: list[Any] = []
        self.call_count: int = 0

    # -- public helpers --------------------------------------------------

    def set_responses(self, responses: list[Any]) -> None:
        """Replace the response queue."""
        self._responses = list(responses)
        self.call_count = 0

    def add_response(self, result: Any) -> None:
        """Append a single response."""
        self._responses.append(result)

    # -- async side-effect used by the patch ----------------------------

    async def _mock_run_dspy(self, *args: Any, **kwargs: Any) -> Any:
        if not self._responses:
            raise RuntimeError(
                'MockDspyLM: no responses queued — call set_responses() or add_response() first'
            )
        result = self._responses.pop(0)
        self.call_count += 1
        return result


@pytest.fixture
def mock_dspy_lm():
    """Fixture providing a ``MockDspyLM`` with ``run_dspy_operation`` patched.

    Patches both the definition site and the most common import site so that
    code using ``from memex_core.llm import run_dspy_operation`` is also
    intercepted.
    """
    mock = MockDspyLM()
    with (
        patch(
            'memex_core.llm.run_dspy_operation',
            side_effect=mock._mock_run_dspy,
        ),
        patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=mock._mock_run_dspy,
        ),
    ):
        yield mock
