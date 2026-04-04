"""Unit tests for NoteSearchEngine cross-encoder reranking."""

import math

import numpy as np
import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from memex_common.schemas import NoteSearchResult
from memex_core.memory.retrieval.document_search import NoteSearchEngine


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.encode.return_value = [np.array([0.1] * 384)]
    return embedder


@pytest.fixture
def mock_reranker():
    reranker = MagicMock()
    # Default: return raw logits (pre-sigmoid)
    reranker.score.return_value = [2.0, -1.0, 0.5]
    return reranker


def _make_result(note_id=None, score=0.05):
    return NoteSearchResult(
        note_id=note_id or uuid4(),
        metadata={'title': 'test'},
        score=score,
    )


class TestNoteSearchReranking:
    def test_init_with_reranker(self, mock_embedder, mock_reranker):
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        assert engine.reranker is mock_reranker

    def test_init_without_reranker(self, mock_embedder):
        engine = NoteSearchEngine(embedder=mock_embedder)
        assert engine.reranker is None

    @pytest.mark.asyncio
    async def test_rerank_applies_sigmoid_scores(self, mock_embedder, mock_reranker):
        """Reranking should produce sigmoid-normalized scores in [0, 1]."""
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)

        ids = [uuid4() for _ in range(3)]
        results = [_make_result(note_id=ids[i], score=0.05) for i in range(3)]
        chunk_texts = {ids[i]: f'chunk text {i}' for i in range(3)}

        reranked = await engine._rerank_results('test query', results, chunk_texts)

        # Verify scores are sigmoid-normalized
        expected_scores = [1.0 / (1.0 + math.exp(-s)) for s in [2.0, -1.0, 0.5]]
        for r in reranked:
            assert 0.0 < r.score < 1.0

        # Verify sorted by score descending
        scores = [r.score for r in reranked]
        assert scores == sorted(scores, reverse=True)

        # Top result should correspond to logit 2.0 (highest)
        assert reranked[0].score == pytest.approx(expected_scores[0], rel=1e-6)

    @pytest.mark.asyncio
    async def test_rerank_with_no_reranker_returns_unchanged(self, mock_embedder):
        """When reranker is None, results are returned as-is."""
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=None)

        results = [_make_result(score=0.06), _make_result(score=0.05)]
        original_scores = [r.score for r in results]

        reranked = await engine._rerank_results('query', results, {})

        assert len(reranked) == 2
        assert [r.score for r in reranked] == original_scores

    @pytest.mark.asyncio
    async def test_rerank_with_empty_results(self, mock_embedder, mock_reranker):
        """Empty results list should return empty."""
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', [], {})
        assert reranked == []

    @pytest.mark.asyncio
    async def test_rerank_calls_reranker_with_chunk_texts(self, mock_embedder, mock_reranker):
        """Reranker should be called with raw chunk text, not formatted memory-unit text."""
        mock_reranker.score.return_value = [1.0, 0.5]
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)

        ids = [uuid4(), uuid4()]
        results = [_make_result(note_id=ids[0]), _make_result(note_id=ids[1])]
        chunk_texts = {ids[0]: 'first doc chunk', ids[1]: 'second doc chunk'}

        await engine._rerank_results('my query', results, chunk_texts)

        mock_reranker.score.assert_called_once_with(
            'my query', ['first doc chunk', 'second doc chunk']
        )

    @pytest.mark.asyncio
    async def test_rerank_graceful_fallback_on_error(self, mock_embedder, mock_reranker):
        """On reranker error, fall back to original RRF order."""
        mock_reranker.score.side_effect = RuntimeError('model failed')
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)

        results = [_make_result(score=0.07), _make_result(score=0.05)]
        reranked = await engine._rerank_results('query', results, {})

        # Should return originals unchanged
        assert len(reranked) == 2
        assert reranked[0].score == 0.07
        assert reranked[1].score == 0.05
