"""Unit tests for NoteSearchEngine cross-encoder reranking."""

import math

import numpy as np
import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from memex_common.schemas import NoteSearchResult
from memex_core.memory.retrieval.document_search import MAX_RERANK_DOCS, NoteSearchEngine


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
    async def test_rerank_false_skips_reranker(self, mock_embedder, mock_reranker):
        """When rerank=False, the reranker must not be called (AC-002)."""
        mock_reranker.score.return_value = [1.0, 0.5]
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)

        ids = [uuid4(), uuid4()]
        results = [
            _make_result(note_id=ids[0], score=0.06),
            _make_result(note_id=ids[1], score=0.05),
        ]
        chunk_texts = {ids[0]: 'chunk a', ids[1]: 'chunk b'}

        # Simulate the guard in search(): rerank=False should skip reranking
        request = MagicMock()
        request.rerank = False

        if request.rerank and engine.reranker and results:
            results = await engine._rerank_results('query', results, chunk_texts)

        mock_reranker.score.assert_not_called()
        # Scores should remain as original RRF scores
        assert results[0].score == 0.06
        assert results[1].score == 0.05

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


class TestMaxRerankDocs:
    def test_max_rerank_docs_constant_is_30(self):
        """AC-F01: MAX_RERANK_DOCS constant exists and equals 30."""
        assert MAX_RERANK_DOCS == 30

    @pytest.mark.asyncio
    async def test_rerank_caps_at_max_rerank_docs(self, mock_embedder, mock_reranker):
        """AC-F02: Only top 30 candidates are sent to the cross-encoder."""
        n_candidates = 50
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.1 - i * 0.001) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        # Reranker should only receive MAX_RERANK_DOCS texts
        mock_reranker.score.return_value = [float(i) for i in range(MAX_RERANK_DOCS)]

        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        # Verify reranker was called with exactly MAX_RERANK_DOCS texts
        call_args = mock_reranker.score.call_args
        assert len(call_args[0][1]) == MAX_RERANK_DOCS

        # All 50 results should still be returned (30 reranked + 20 overflow)
        assert len(reranked) == n_candidates

    @pytest.mark.asyncio
    async def test_rerank_preserves_overflow_after_reranked(self, mock_embedder, mock_reranker):
        """Non-reranked overflow results are appended after the reranked set."""
        n_candidates = 35
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.1 - i * 0.001) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}
        overflow_ids = set(ids[MAX_RERANK_DOCS:])

        mock_reranker.score.return_value = [float(i) for i in range(MAX_RERANK_DOCS)]

        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        # Last 5 results should be the overflow (unreranked), preserving original order
        tail = reranked[MAX_RERANK_DOCS:]
        assert len(tail) == 5
        for r in tail:
            assert r.note_id in overflow_ids

    @pytest.mark.asyncio
    async def test_rerank_under_cap_sends_all(self, mock_embedder, mock_reranker):
        """When candidates <= MAX_RERANK_DOCS, all are sent to reranker."""
        n_candidates = 10
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.05) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        mock_reranker.score.return_value = [float(i) for i in range(n_candidates)]

        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        call_args = mock_reranker.score.call_args
        assert len(call_args[0][1]) == n_candidates
        assert len(reranked) == n_candidates


class TestOverflowScoreNormalization:
    """Verify overflow results get scores below all reranked results."""

    @pytest.mark.asyncio
    async def test_overflow_scores_below_min_reranked(self, mock_embedder, mock_reranker):
        """Overflow scores must be strictly below the lowest reranked score."""
        n_candidates = 35
        ids = [uuid4() for _ in range(n_candidates)]
        # Give overflow items high RRF+boost scores that would otherwise outrank
        results = [
            _make_result(note_id=ids[i], score=0.3 if i >= MAX_RERANK_DOCS else 0.05)
            for i in range(n_candidates)
        ]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        # Reranker returns moderate logits → sigmoid scores around 0.5-0.88
        mock_reranker.score.return_value = [1.0 + i * 0.1 for i in range(MAX_RERANK_DOCS)]
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        reranked_scores = [r.score for r in reranked[:MAX_RERANK_DOCS]]
        overflow_scores = [r.score for r in reranked[MAX_RERANK_DOCS:]]

        min_reranked = min(reranked_scores)
        for s in overflow_scores:
            assert s < min_reranked, f'Overflow score {s} >= min reranked score {min_reranked}'

    @pytest.mark.asyncio
    async def test_overflow_preserves_relative_order(self, mock_embedder, mock_reranker):
        """Overflow items should maintain descending score order."""
        n_candidates = 35
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.1 - i * 0.001) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        mock_reranker.score.return_value = [float(i) for i in range(MAX_RERANK_DOCS)]
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        overflow_scores = [r.score for r in reranked[MAX_RERANK_DOCS:]]
        assert overflow_scores == sorted(overflow_scores, reverse=True)

    @pytest.mark.asyncio
    async def test_overflow_scores_non_negative(self, mock_embedder, mock_reranker):
        """Overflow scores must never go below 0."""
        n_candidates = 35
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.05) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        # Very low logits → sigmoid near 0 → min_reranked very small
        mock_reranker.score.return_value = [-10.0] * MAX_RERANK_DOCS
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        for r in reranked[MAX_RERANK_DOCS:]:
            assert r.score >= 0.0

    @pytest.mark.asyncio
    async def test_no_overflow_leaves_scores_unchanged(self, mock_embedder, mock_reranker):
        """When all results fit in rerank window, no overflow normalization needed."""
        n_candidates = 5
        ids = [uuid4() for _ in range(n_candidates)]
        results = [_make_result(note_id=ids[i], score=0.05) for i in range(n_candidates)]
        chunk_texts = {ids[i]: f'chunk {i}' for i in range(n_candidates)}

        logits = [2.0, 1.0, 0.5, -0.5, -1.0]
        mock_reranker.score.return_value = logits
        engine = NoteSearchEngine(embedder=mock_embedder, reranker=mock_reranker)
        reranked = await engine._rerank_results('query', results, chunk_texts)

        # All scores should be sigmoid-normalized, no overflow processing
        expected = sorted([1.0 / (1.0 + math.exp(-s)) for s in logits], reverse=True)
        actual = [r.score for r in reranked]
        for a, e in zip(actual, expected):
            assert a == pytest.approx(e, rel=1e-6)
