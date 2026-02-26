import os
import pytest
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch
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
    config.server.active_vault = 'global'
    config.server.memory.extraction.model.model = 'test-model'
    config.server.memory.opinion_formation.confidence.similarity_threshold = 0.8
    config.server.attached_vaults = []
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
