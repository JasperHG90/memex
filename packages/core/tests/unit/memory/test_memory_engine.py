import logging

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.engine import MemoryEngine, get_memory_engine, _build_contradiction_engine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.config import MemexConfig
from memex_core.memory.reflect.models import ReflectionRequest


@pytest.fixture
def mock_session():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_extraction_engine():
    engine = MagicMock(spec=ExtractionEngine)
    engine.extract_and_persist = AsyncMock()
    engine.queue_service = None
    engine.embedding_model = MagicMock()
    return engine


@pytest.fixture
def mock_retrieval_engine():
    engine = MagicMock(spec=RetrievalEngine)
    engine.retrieve = AsyncMock()
    return engine


@pytest.fixture
def config():
    return MagicMock(spec=MemexConfig)


@pytest.fixture
def memory_engine(config, mock_extraction_engine, mock_retrieval_engine):
    return MemoryEngine(config, mock_extraction_engine, mock_retrieval_engine)


@pytest.mark.asyncio
async def test_retain_without_reflection(memory_engine, mock_session, mock_extraction_engine):
    contents = [RetainContent(content='test')]
    mock_extraction_engine.extract_and_persist.return_value = (['id1'], set())

    result = await memory_engine.retain(mock_session, contents, reflect_after=False)

    assert result['unit_ids'] == ['id1']
    mock_extraction_engine.extract_and_persist.assert_called_once()


@pytest.mark.asyncio
async def test_retain_with_reflection(memory_engine, mock_session, mock_extraction_engine):
    contents = [RetainContent(content='test')]
    entity_id = uuid4()
    mock_extraction_engine.extract_and_persist.return_value = (['id1'], {entity_id})

    with patch('memex_core.memory.engine.ReflectionEngine') as MockReflectionEngine:
        mock_reflector = MockReflectionEngine.return_value
        mock_reflector.reflect_batch = AsyncMock(
            return_value=[MagicMock()]
        )  # Return list of models

        result = await memory_engine.retain(mock_session, contents, reflect_after=True)

        assert result['touched_entities'] == {entity_id}
        mock_reflector.reflect_batch.assert_called_once()

        # Verify the call args were a list of requests
        call_args = mock_reflector.reflect_batch.call_args[0][0]
        assert isinstance(call_args, list)
        assert len(call_args) == 1
        assert isinstance(call_args[0], ReflectionRequest)
        assert call_args[0].entity_id == entity_id


@pytest.mark.asyncio
async def test_recall(memory_engine, mock_session, mock_retrieval_engine):
    mock_retrieval_engine.retrieve.return_value = (['memory1'], None)
    request = MagicMock()

    result, _ = await memory_engine.recall(mock_session, request)

    assert result == ['memory1']
    mock_retrieval_engine.retrieve.assert_called_once_with(mock_session, request)


@pytest.mark.asyncio
async def test_reflect_explicit(memory_engine, mock_session):
    request = ReflectionRequest(entity_id=uuid4())

    with patch('memex_core.memory.engine.ReflectionEngine') as MockReflectionEngine:
        mock_reflector = MockReflectionEngine.return_value
        mock_reflector.reflect_on_entity = AsyncMock(return_value='model')

        result = await memory_engine.reflect(mock_session, request)

        assert result == 'model'
        mock_reflector.reflect_on_entity.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_resonance_task_swallows_exceptions(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """_do_resonance_update should catch and log exceptions, not raise."""
    mock_bg_session = AsyncMock(spec=AsyncSession)
    mock_session_factory = MagicMock(spec=async_sessionmaker)
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_bg_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_queue = AsyncMock()
    mock_queue.handle_retrieval_event = AsyncMock(side_effect=RuntimeError('DB error'))
    mock_extraction_engine.queue_service = mock_queue

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        session_factory=mock_session_factory,
    )

    entity_ids = {uuid4()}
    vault_id = uuid4()
    mock_retrieval_engine.retrieve.return_value = (
        [MagicMock()],
        {'entity_ids': entity_ids, 'vault_id': vault_id},
    )
    request = MagicMock()
    session = AsyncMock(spec=AsyncSession)

    _, resonance_task = await engine.recall(session, request)
    assert resonance_task is not None

    with patch('memex_core.memory.engine.logger') as mock_logger:
        # Should NOT raise
        await resonance_task()
        mock_logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_resonance_task_commits_on_success(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """_do_resonance_update should commit the background session on success."""
    mock_bg_session = AsyncMock(spec=AsyncSession)
    mock_session_factory = MagicMock(spec=async_sessionmaker)
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_bg_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_queue = AsyncMock()
    mock_queue.handle_retrieval_event = AsyncMock()
    mock_extraction_engine.queue_service = mock_queue

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        session_factory=mock_session_factory,
    )

    entity_ids = {uuid4()}
    vault_id = uuid4()
    mock_retrieval_engine.retrieve.return_value = (
        [MagicMock()],
        {'entity_ids': entity_ids, 'vault_id': vault_id},
    )
    request = MagicMock()
    session = AsyncMock(spec=AsyncSession)

    _, resonance_task = await engine.recall(session, request)
    assert resonance_task is not None

    await resonance_task()

    mock_queue.handle_retrieval_event.assert_awaited_once()
    mock_bg_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recall_no_session_factory_skips_resonance(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """When session_factory is None, resonance_task should be None."""
    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        session_factory=None,
    )

    mock_extraction_engine.queue_service = AsyncMock()

    entity_ids = {uuid4()}
    vault_id = uuid4()
    mock_retrieval_engine.retrieve.return_value = (
        [MagicMock()],
        {'entity_ids': entity_ids, 'vault_id': vault_id},
    )
    request = MagicMock()
    session = AsyncMock(spec=AsyncSession)

    results, resonance_task = await engine.recall(session, request)
    assert len(results) == 1
    assert resonance_task is None


@pytest.mark.asyncio
async def test_recall_no_queue_service_skips_resonance(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """When queue_service is None, resonance_task should be None."""
    mock_session_factory = MagicMock(spec=async_sessionmaker)
    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        session_factory=mock_session_factory,
    )

    mock_extraction_engine.queue_service = None

    entity_ids = {uuid4()}
    vault_id = uuid4()
    mock_retrieval_engine.retrieve.return_value = (
        [MagicMock()],
        {'entity_ids': entity_ids, 'vault_id': vault_id},
    )
    request = MagicMock()
    session = AsyncMock(spec=AsyncSession)

    results, resonance_task = await engine.recall(session, request)
    assert len(results) == 1
    assert resonance_task is None


# ---------------------------------------------------------------------------
# Contradiction pipeline: retain() early return, gate logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retain_returns_contradiction_task_on_early_return(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """retain() must include contradiction_task in early return when entities are empty."""
    mock_contradiction = MagicMock()
    mock_contradiction.detect_contradictions = MagicMock(return_value='coro-sentinel')

    mock_session_factory = MagicMock(spec=async_sessionmaker)

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        contradiction_engine=mock_contradiction,
        session_factory=mock_session_factory,
    )

    unit_ids = [uuid4()]
    mock_extraction_engine.extract_and_persist.return_value = (unit_ids, set())

    session = AsyncMock(spec=AsyncSession)
    contents = [RetainContent(content='test')]

    result = await engine.retain(session, contents, note_id='note-1', reflect_after=False)

    # Early return (empty entities) must still include contradiction_task
    assert 'contradiction_task' in result
    assert result['contradiction_task'] == 'coro-sentinel'
    assert result['touched_entities'] == set()


@pytest.mark.asyncio
async def test_retain_contradiction_runs_with_entities(
    config, mock_extraction_engine, mock_retrieval_engine
):
    """retain() runs contradiction even when entities are present (normal path)."""
    mock_contradiction = MagicMock()
    mock_contradiction.detect_contradictions = MagicMock(return_value='coro-sentinel')

    mock_session_factory = MagicMock(spec=async_sessionmaker)

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        contradiction_engine=mock_contradiction,
        session_factory=mock_session_factory,
    )

    unit_ids = [uuid4()]
    entity_id = uuid4()
    mock_extraction_engine.extract_and_persist.return_value = (unit_ids, {entity_id})

    session = AsyncMock(spec=AsyncSession)
    contents = [RetainContent(content='test')]

    with patch('memex_core.memory.engine.ReflectionEngine') as MockReflection:
        MockReflection.return_value.reflect_batch = AsyncMock(return_value=[MagicMock()])

        result = await engine.retain(session, contents, note_id='note-1', reflect_after=True)

    assert result['contradiction_task'] == 'coro-sentinel'
    assert result['touched_entities'] == {entity_id}


@pytest.mark.asyncio
async def test_retain_gate_logging_no_engine(
    config, mock_extraction_engine, mock_retrieval_engine, caplog
):
    """retain() logs WARNING when contradiction engine is None."""
    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        contradiction_engine=None,
        session_factory=MagicMock(spec=async_sessionmaker),
    )

    mock_extraction_engine.extract_and_persist.return_value = ([uuid4()], set())

    session = AsyncMock(spec=AsyncSession)
    contents = [RetainContent(content='test')]

    with caplog.at_level(logging.WARNING, logger='memex.core.memory.engine'):
        await engine.retain(session, contents, reflect_after=False)

    assert any('engine is None' in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_retain_gate_logging_no_session_factory(
    config, mock_extraction_engine, mock_retrieval_engine, caplog
):
    """retain() logs WARNING when session_factory is None."""
    mock_contradiction = MagicMock()

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        contradiction_engine=mock_contradiction,
        session_factory=None,
    )

    mock_extraction_engine.extract_and_persist.return_value = ([uuid4()], set())

    session = AsyncMock(spec=AsyncSession)
    contents = [RetainContent(content='test')]

    with caplog.at_level(logging.WARNING, logger='memex.core.memory.engine'):
        await engine.retain(session, contents, reflect_after=False)

    assert any('no session_factory' in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_retain_gate_logging_no_units(
    config, mock_extraction_engine, mock_retrieval_engine, caplog
):
    """retain() logs INFO when no units were extracted."""
    mock_contradiction = MagicMock()

    engine = MemoryEngine(
        config,
        mock_extraction_engine,
        mock_retrieval_engine,
        contradiction_engine=mock_contradiction,
        session_factory=MagicMock(spec=async_sessionmaker),
    )

    mock_extraction_engine.extract_and_persist.return_value = ([], set())

    session = AsyncMock(spec=AsyncSession)
    contents = [RetainContent(content='test')]

    with caplog.at_level(logging.INFO, logger='memex.core.memory.engine'):
        await engine.retain(session, contents, reflect_after=False)

    assert any('no units extracted' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# get_memory_engine() session_factory forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_memory_engine_forwards_session_factory(
    mock_extraction_engine, mock_retrieval_engine
):
    """get_memory_engine() must forward session_factory to MemoryEngine."""
    mock_factory = MagicMock(spec=async_sessionmaker)

    with (
        patch(
            'memex_core.memory.models.get_embedding_model',
            new_callable=AsyncMock,
        ),
        patch(
            'memex_core.memory.models.get_reranking_model',
            new_callable=AsyncMock,
        ),
        patch(
            'memex_core.memory.models.get_ner_model',
            new_callable=AsyncMock,
        ),
        patch('memex_core.memory.engine._build_contradiction_engine', return_value=None),
    ):
        config = MagicMock()

        engine = await get_memory_engine(
            config,
            extraction_engine=mock_extraction_engine,
            retrieval_engine=mock_retrieval_engine,
            session_factory=mock_factory,
        )

    assert engine._session_factory is mock_factory


@pytest.mark.asyncio
async def test_get_memory_engine_defaults_session_factory_none(
    mock_extraction_engine, mock_retrieval_engine
):
    """get_memory_engine() defaults session_factory to None."""
    with (
        patch(
            'memex_core.memory.models.get_embedding_model',
            new_callable=AsyncMock,
        ),
        patch(
            'memex_core.memory.models.get_reranking_model',
            new_callable=AsyncMock,
        ),
        patch(
            'memex_core.memory.models.get_ner_model',
            new_callable=AsyncMock,
        ),
        patch('memex_core.memory.engine._build_contradiction_engine', return_value=None),
    ):
        config = MagicMock()

        engine = await get_memory_engine(
            config,
            extraction_engine=mock_extraction_engine,
            retrieval_engine=mock_retrieval_engine,
        )

    assert engine._session_factory is None


# ---------------------------------------------------------------------------
# _build_contradiction_engine() success log
# ---------------------------------------------------------------------------


def test_build_contradiction_engine_logs_success(caplog):
    """_build_contradiction_engine() logs INFO with model name, threshold, alpha on success."""
    config = MagicMock()
    config.server.memory.contradiction.enabled = True
    config.server.memory.contradiction.model.model = 'test-model/v1'
    config.server.memory.contradiction.model.base_url = None
    config.server.memory.contradiction.model.api_key = None
    config.server.memory.contradiction.similarity_threshold = 0.42
    config.server.memory.contradiction.alpha = 0.15

    with (
        patch('memex_core.memory.engine.dspy.LM'),
        patch('memex_core.memory.engine.ContradictionEngine'),
        caplog.at_level(logging.INFO, logger='memex.core.memory.engine'),
    ):
        result = _build_contradiction_engine(config)

    assert result is not None
    log_msgs = [r.message for r in caplog.records]
    assert any('Contradiction engine created' in m for m in log_msgs)


# ---------------------------------------------------------------------------
# Static verification: dead code removal
# ---------------------------------------------------------------------------


def test_server_ingestion_no_schedule_contradiction():
    """Verify _schedule_contradiction and _run_contradiction are removed from server routes."""
    import inspect
    from memex_core.server import ingestion as server_ingestion

    source = inspect.getsource(server_ingestion)
    assert '_schedule_contradiction' not in source
    assert '_run_contradiction' not in source


def test_batch_no_contradiction_task():
    """Verify contradiction_task handling is removed from batch.py."""
    import inspect
    from memex_core.processing import batch

    source = inspect.getsource(batch)
    assert 'contradiction_task' not in source


# ---------------------------------------------------------------------------
# MemexAPI startup diagnostic log (AC-008)
# ---------------------------------------------------------------------------


def test_memex_api_logs_warning_when_contradiction_disabled(
    mock_metastore,
    mock_filestore,
    mock_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
    caplog,
):
    """MemexAPI.__init__ logs WARNING when contradiction engine is None (disabled)."""
    from memex_core.api import MemexAPI

    with (
        patch(
            'memex_core.api._build_contradiction_engine',
            return_value=None,
        ),
        caplog.at_level(logging.WARNING, logger='memex.core.api'),
    ):
        MemexAPI(
            embedding_model=mock_embedding_model,
            reranking_model=mock_reranking_model,
            ner_model=mock_ner_model,
            metastore=mock_metastore,
            filestore=mock_filestore,
            config=mock_config,
        )

    assert any('contradiction detection is DISABLED' in r.message for r in caplog.records), (
        'Expected WARNING about contradiction detection being disabled'
    )
