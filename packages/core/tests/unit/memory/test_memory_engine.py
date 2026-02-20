import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.config import MemexConfig
from memex_core.memory.sql_models import TokenUsage
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
    mock_extraction_engine.extract_and_persist.return_value = (['id1'], TokenUsage(), set())

    result = await memory_engine.retain(mock_session, contents, reflect_after=False)

    assert result['unit_ids'] == ['id1']
    mock_extraction_engine.extract_and_persist.assert_called_once()


@pytest.mark.asyncio
async def test_retain_with_reflection(memory_engine, mock_session, mock_extraction_engine):
    contents = [RetainContent(content='test')]
    entity_id = uuid4()
    mock_extraction_engine.extract_and_persist.return_value = (['id1'], TokenUsage(), {entity_id})

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
    mock_retrieval_engine.retrieve.return_value = ['memory1']
    request = MagicMock()

    result = await memory_engine.recall(mock_session, request)

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
