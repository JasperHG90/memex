from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import MemoryUnit


@pytest.fixture
def mock_session():
    mock = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock.exec.return_value = mock_result
    return mock


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    mock_vec = MagicMock()
    mock_vec.tolist.return_value = [0.1] * 384
    embedder.encode.return_value = [mock_vec]
    return embedder


@pytest.mark.asyncio
async def test_reranking_failure_fallback(mock_embedder, mock_session):
    """Test that engine falls back to RRF order if reranking fails."""
    # Setup Reranker that raises Exception
    mock_reranker = MagicMock()
    mock_reranker.score.side_effect = RuntimeError('Reranking Service Down')

    engine = RetrievalEngine(embedder=mock_embedder, reranker=mock_reranker)

    # Mock _perform_rrf_retrieval to return 2 dummy items
    # We mock the internal method to avoid complex DB setup
    # But wait, retrieve calls _perform_rrf_retrieval then _hydrate_results
    # It's easier to mock _hydrate_results to return concrete MemoryUnits

    u1 = MemoryUnit(id=uuid4(), text='Doc 1', embedding=[])
    u2 = MemoryUnit(id=uuid4(), text='Doc 2', embedding=[])

    # We need to patch the methods on the instance or class
    with patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf:
        with patch.object(engine, '_hydrate_results', new_callable=AsyncMock) as mock_hydrate:
            # Mock returns
            mock_rrf.return_value = [
                'dummy1',
                'dummy2',
            ]  # The content doesn't matter as we mock hydrate
            mock_hydrate.return_value = [u1, u2]  # RRF Order: u1, u2

            # Request with rerank=True
            request = RetrievalRequest(query='test', limit=5, rerank=True)

            results, _ = await engine.retrieve(mock_session, request)

            # Assert reranker was called
            mock_reranker.score.assert_called_once()

            # Assert results are still [u1, u2] (original order preserved)
            assert results == [u1, u2]


def test_deduplicate_malformed_evidence_ids():
    """Test robustness against invalid UUID strings in metadata."""
    engine = RetrievalEngine(embedder=MagicMock(), reranker=None)

    u1_id = uuid4()
    u1 = MemoryUnit(id=u1_id, text='Fact', embedding=[])

    u2 = MemoryUnit(
        id=uuid4(),
        text='Obs',
        unit_metadata={
            'evidence_ids': ['not-a-uuid', str(u1_id), 12345]  # 1 valid, 2 invalid
        },
        embedding=[],
    )

    results = [u1, u2]
    deduplicated = engine._deduplicate_and_cite(results)

    # Both units remain; citation metadata is attached to u2
    assert len(deduplicated) == 2

    u2_result = next(u for u in deduplicated if u.id == u2.id)
    citations = u2_result.unit_metadata.get('citations')
    assert citations is not None
    assert len(citations) == 1
    assert citations[0]['id'] == str(u1_id)


@pytest.mark.asyncio
async def test_resonance_context_collected(mock_embedder, mock_session):
    """Test that retrieve() collects resonance context for background scheduling."""
    mock_queue = AsyncMock()
    entity_id = uuid4()

    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)
    engine.queue_service = mock_queue

    u1 = MemoryUnit(id=uuid4(), text='Unit', embedding=[])

    with (
        patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf,
        patch.object(engine, '_hydrate_results', new_callable=AsyncMock, return_value=[u1]),
    ):
        mock_rrf.return_value = [MagicMock(id=u1.id, type='unit')]

        # Mock entity lookup to return an entity ID
        mock_result = MagicMock()
        mock_result.all.return_value = [entity_id]
        mock_session.exec.return_value = mock_result

        request = RetrievalRequest(query='test')
        results, resonance_ctx = await engine.retrieve(mock_session, request)

        assert len(results) == 1
        assert resonance_ctx is not None
        assert entity_id in resonance_ctx['entity_ids']
        # queue_service.handle_retrieval_event should NOT have been called inline
        mock_queue.handle_retrieval_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_filters_propagation(mock_embedder, mock_session):
    """Test that filters in RetrievalRequest are passed to strategies."""
    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)

    filters: dict[str, Any] = {'start_date': '2024-01-01'}
    request = RetrievalRequest(query='test', filters=filters)

    with patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf:
        mock_rrf.return_value = []

        await engine.retrieve(mock_session, request)  # return value not needed

        # Verify call args
        args, kwargs = mock_rrf.call_args
        # Signature: _perform_rrf_retrieval(session, query, embedding, limit, filters)
        passed_filters = args[4]

        expected_filters: dict[str, Any] = filters.copy()
        expected_filters['include_stale'] = False
        assert passed_filters == expected_filters


@pytest.mark.asyncio
async def test_retrieve_empty_returns_none_resonance(mock_embedder, mock_session):
    """When RRF returns no results, resonance_ctx should be None."""
    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)

    with patch.object(
        engine,
        '_perform_rrf_retrieval',
        new_callable=AsyncMock,
        return_value=[],
    ):
        request = RetrievalRequest(query='test')
        results, resonance_ctx = await engine.retrieve(mock_session, request)

        assert results == []
        assert resonance_ctx is None


@pytest.mark.asyncio
async def test_resonance_collection_error_still_returns_results(mock_embedder, mock_session):
    """If entity lookup for resonance fails, results are still returned."""
    mock_queue = AsyncMock()
    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)
    engine.queue_service = mock_queue

    u1 = MemoryUnit(id=uuid4(), text='Unit', embedding=[])

    with (
        patch.object(
            engine,
            '_perform_rrf_retrieval',
            new_callable=AsyncMock,
        ) as mock_rrf,
        patch.object(
            engine,
            '_hydrate_results',
            new_callable=AsyncMock,
            return_value=[u1],
        ),
    ):
        mock_rrf.return_value = [MagicMock(id=u1.id, type='unit')]

        # Make session.exec raise on the entity lookup call (step 10)
        # The first call(s) to session.exec are in _perform_rrf_retrieval
        # which is mocked, so the only real call is the entity lookup.
        mock_session.exec.side_effect = RuntimeError('DB error')

        request = RetrievalRequest(query='test')
        results, resonance_ctx = await engine.retrieve(mock_session, request)

        assert len(results) == 1
        assert results[0].id == u1.id
        assert resonance_ctx is None
