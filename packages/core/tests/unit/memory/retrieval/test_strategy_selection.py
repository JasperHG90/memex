"""Unit tests for selective strategy search and weighted RRF."""

from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest, VALID_STRATEGIES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def engine(mock_embedder):
    return RetrievalEngine(embedder=mock_embedder, reranker=None)


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestRetrievalRequestValidation:
    """Tests for the strategies field validation on RetrievalRequest."""

    def test_strategies_none_is_default(self):
        req = RetrievalRequest(query='hello')
        assert req.strategies is None

    def test_strategies_valid_single(self):
        req = RetrievalRequest(query='hello', strategies=['semantic'])
        assert req.strategies == ['semantic']

    def test_strategies_valid_multiple(self):
        req = RetrievalRequest(query='hello', strategies=['graph', 'keyword'])
        assert set(req.strategies) == {'graph', 'keyword'}

    def test_strategies_all_valid(self):
        req = RetrievalRequest(query='hello', strategies=sorted(VALID_STRATEGIES))
        assert set(req.strategies) == VALID_STRATEGIES

    def test_strategies_empty_raises(self):
        with pytest.raises(ValueError, match='must not be empty'):
            RetrievalRequest(query='hello', strategies=[])

    def test_strategies_invalid_name_raises(self):
        with pytest.raises(ValueError, match='Invalid strategy names'):
            RetrievalRequest(query='hello', strategies=['semantic', 'bogus'])

    def test_strategies_completely_invalid_raises(self):
        with pytest.raises(ValueError, match='Invalid strategy names'):
            RetrievalRequest(query='hello', strategies=['nonexistent'])


# ---------------------------------------------------------------------------
# _resolve_active_strategies tests
# ---------------------------------------------------------------------------


class TestResolveActiveStrategies:
    """Tests for the strategy resolution helper."""

    def test_none_returns_all(self, engine):
        active, include_mm = engine._resolve_active_strategies(None)
        assert set(active.keys()) == {'semantic', 'keyword', 'graph', 'temporal'}
        assert include_mm is True

    def test_single_semantic(self, engine):
        active, include_mm = engine._resolve_active_strategies(['semantic'])
        assert set(active.keys()) == {'semantic'}
        assert include_mm is False

    def test_mental_model_only(self, engine):
        active, include_mm = engine._resolve_active_strategies(['mental_model'])
        assert active == {}
        assert include_mm is True

    def test_graph_and_keyword(self, engine):
        active, include_mm = engine._resolve_active_strategies(['graph', 'keyword'])
        assert set(active.keys()) == {'graph', 'keyword'}
        assert include_mm is False

    def test_keyword_and_mental_model(self, engine):
        active, include_mm = engine._resolve_active_strategies(['keyword', 'mental_model'])
        assert set(active.keys()) == {'keyword'}
        assert include_mm is True


# ---------------------------------------------------------------------------
# Retrieval dispatch tests
# ---------------------------------------------------------------------------


class TestStrategyDispatch:
    """Tests that retrieve() dispatches correctly based on strategies."""

    @pytest.mark.asyncio
    async def test_strategies_none_runs_all(self, engine, mock_session, mock_embedder):
        """strategies=None should invoke _perform_rrf_retrieval with strategies=None."""
        request = RetrievalRequest(query='test', limit=5)

        with patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf:
            mock_rrf.return_value = []
            results, _ = await engine.retrieve(mock_session, request)

            mock_rrf.assert_called_once()
            _, kwargs = mock_rrf.call_args
            assert kwargs.get('strategies') is None
            assert kwargs.get('strategy_weights') is None

    @pytest.mark.asyncio
    async def test_single_strategy_calls_single_path(self, engine, mock_session, mock_embedder):
        """A single strategy should use the single-strategy fast path."""
        request = RetrievalRequest(query='test', limit=5, strategies=['semantic'])

        with patch.object(
            engine, '_perform_single_strategy_retrieval', new_callable=AsyncMock
        ) as mock_single:
            mock_single.return_value = []
            with patch.object(
                engine, '_perform_rrf_retrieval', wraps=engine._perform_rrf_retrieval
            ):
                # We need to let _perform_rrf_retrieval run to hit the single-strategy branch
                # But it calls _perform_single_strategy_retrieval internally
                results, _ = await engine.retrieve(mock_session, request)
                mock_single.assert_called_once()

    @pytest.mark.asyncio
    async def test_mental_model_only_calls_single_path(self, engine, mock_session, mock_embedder):
        """mental_model as the only strategy should use single-strategy path."""
        request = RetrievalRequest(query='test', limit=5, strategies=['mental_model'])

        with patch.object(
            engine, '_perform_single_strategy_retrieval', new_callable=AsyncMock
        ) as mock_single:
            mock_single.return_value = []
            results, _ = await engine.retrieve(mock_session, request)
            mock_single.assert_called_once()
            args, kwargs = mock_single.call_args
            # strategy_name should be 'mental_model', result_type should be 'model'
            assert args[5] == 'mental_model'
            assert args[8] == 'model'

    @pytest.mark.asyncio
    async def test_two_strategies_use_rrf(self, engine, mock_session, mock_embedder):
        """Two strategies should use multi-strategy RRF path (not single)."""
        request = RetrievalRequest(query='test', limit=5, strategies=['graph', 'keyword'])

        with patch.object(
            engine, '_perform_single_strategy_retrieval', new_callable=AsyncMock
        ) as mock_single:
            with patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf:
                mock_rrf.return_value = []
                results, _ = await engine.retrieve(mock_session, request)
                # RRF should be called, single should NOT
                mock_rrf.assert_called_once()
                mock_single.assert_not_called()

    @pytest.mark.asyncio
    async def test_strategy_weights_forwarded(self, engine, mock_session, mock_embedder):
        """strategy_weights should be forwarded to _perform_rrf_retrieval."""
        weights = {'semantic': 2.0, 'keyword': 0.5}
        request = RetrievalRequest(query='test', limit=5, strategy_weights=weights)

        with patch.object(engine, '_perform_rrf_retrieval', new_callable=AsyncMock) as mock_rrf:
            mock_rrf.return_value = []
            results, _ = await engine.retrieve(mock_session, request)

            _, kwargs = mock_rrf.call_args
            assert kwargs.get('strategy_weights') == weights
