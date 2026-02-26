"""Unit tests for the DocumentSearchEngine class."""

import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from memex_common.schemas import NoteSearchRequest
from memex_core.memory.retrieval.document_search import NoteSearchEngine


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.encode.return_value = [np.array([0.1] * 384)]
    return embedder


@pytest.fixture
def engine(mock_embedder: MagicMock) -> NoteSearchEngine:
    return NoteSearchEngine(embedder=mock_embedder)


class TestNoteSearchEngine:
    """Tests for DocumentSearchEngine initialization and strategy selection."""

    def test_init_default(self, mock_embedder: MagicMock) -> None:
        engine = NoteSearchEngine(embedder=mock_embedder)
        assert engine.embedder is mock_embedder
        assert engine.graph_strategy is not None
        assert engine.expander is None

    def test_init_with_ner(self, mock_embedder: MagicMock) -> None:
        ner = MagicMock()
        engine = NoteSearchEngine(embedder=mock_embedder, ner_model=ner)
        assert engine.graph_strategy.ner_model is ner

    def test_init_with_lm(self, mock_embedder: MagicMock) -> None:
        lm = MagicMock()
        engine = NoteSearchEngine(embedder=mock_embedder, lm=lm)
        assert engine.expander is not None


class TestStrategySelection:
    """Tests for strategy selection based on request parameters."""

    @pytest.mark.asyncio
    async def test_empty_strategies_returns_empty(self, engine: NoteSearchEngine) -> None:
        """When no strategies are active, return empty results."""
        request = NoteSearchRequest(query='test', strategies=[])
        session = AsyncMock()
        result = await engine.search(session, request)
        assert result == []

    @pytest.mark.asyncio
    async def test_default_strategies(self, engine: NoteSearchEngine) -> None:
        """Default strategies include semantic, keyword, graph, and temporal."""
        request = NoteSearchRequest(query='test')
        assert set(request.strategies) == {'semantic', 'keyword', 'graph', 'temporal'}


class TestMultiQueryFusion:
    """Tests for the multi-query RRF fusion logic."""

    def test_single_batch_passthrough(self) -> None:
        """Single batch returns items as-is."""
        chunk_a = MagicMock(id=uuid4())
        chunk_b = MagicMock(id=uuid4())
        # batch = [((chunk_a, 0.9), (chunk_b, 0.5))]

        # _fuse_multi_query expects list[tuple[list, float]]
        batches: list[tuple[list, float]] = [([(chunk_a, 0.9), (chunk_b, 0.5)], 2.0)]
        result = NoteSearchEngine._fuse_multi_query(batches, limit=10)
        assert len(result) == 2
        assert result[0][0] is chunk_a

    def test_multi_batch_deduplication(self) -> None:
        """Same chunk appearing in multiple batches gets a higher fused score."""
        shared_id = uuid4()
        chunk_shared = MagicMock(id=shared_id)
        chunk_unique = MagicMock(id=uuid4())

        batches: list[tuple[list, float]] = [
            ([(chunk_shared, 0.9)], 2.0),
            ([(chunk_shared, 0.8), (chunk_unique, 0.5)], 1.0),
        ]
        result = NoteSearchEngine._fuse_multi_query(batches, limit=10)

        ids = [r[0].id for r in result]
        # Shared chunk should appear exactly once
        assert ids.count(shared_id) == 1
        # Shared chunk should rank first (boosted by two batches)
        assert result[0][0].id == shared_id
