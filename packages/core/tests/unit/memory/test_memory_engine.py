import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.engine import MemoryEngine
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
