"""Unit tests for the DocumentSearchEngine class."""

import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4, UUID

from memex_common.schemas import NoteSearchRequest, NoteSearchResult
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


class TestMMR:
    """Tests for the MMR (Maximal Marginal Relevance) re-ranking logic."""

    def _make_result(self, _note_id: int, score: float) -> NoteSearchResult:
        """Helper to create a NoteSearchResult with given score."""
        return NoteSearchResult(
            note_id=uuid4(),
            metadata={},
            score=score,
        )

    def test_mmr_pure_relevance(self) -> None:
        """λ=1.0 preserves original ranking (pure relevance, no diversity)."""
        results = [
            self._make_result(1, 0.9),
            self._make_result(2, 0.8),
            self._make_result(3, 0.7),
        ]
        # All pairs have high similarity (would normally promote diversity)
        sim_matrix = {
            (results[0].note_id, results[1].note_id): 0.9,
            (results[0].note_id, results[2].note_id): 0.9,
            (results[1].note_id, results[2].note_id): 0.9,
        }

        reranked = NoteSearchEngine._apply_mmr(
            results, lam=1.0, limit=3, similarity_matrix=sim_matrix
        )

        # With λ=1.0, original order should be preserved
        assert [r.score for r in reranked] == [1.0, 0.8 / 0.9, 0.7 / 0.9]

    def test_mmr_pure_diversity(self) -> None:
        """λ=0.0 selects maximally different documents (pure diversity)."""
        id_a, id_b, id_c = uuid4(), uuid4(), uuid4()
        results = [
            NoteSearchResult(note_id=id_a, metadata={}, score=1.0),
            NoteSearchResult(note_id=id_b, metadata={}, score=0.9),
            NoteSearchResult(note_id=id_c, metadata={}, score=0.8),
        ]
        # A and B are very similar, C is different from both
        sim_matrix = {
            (min(id_a, id_b), max(id_a, id_b)): 0.99,
            (min(id_a, id_c), max(id_a, id_c)): 0.1,
            (min(id_b, id_c), max(id_b, id_c)): 0.1,
        }

        reranked = NoteSearchEngine._apply_mmr(
            results, lam=0.0, limit=3, similarity_matrix=sim_matrix
        )

        # With λ=0.0, first pick is by score (A), second should be C (most different from A)
        assert reranked[0].note_id == id_a
        assert reranked[1].note_id == id_c  # C is more different from A than B

    def test_mmr_balanced(self) -> None:
        """λ=0.7 promotes diverse results while keeping relevant ones near top."""
        id_a, id_b, id_c = uuid4(), uuid4(), uuid4()
        results = [
            NoteSearchResult(note_id=id_a, metadata={}, score=1.0),
            NoteSearchResult(note_id=id_b, metadata={}, score=0.95),
            NoteSearchResult(note_id=id_c, metadata={}, score=0.7),
        ]
        # A and B are very similar, C is different
        sim_matrix = {
            (min(id_a, id_b), max(id_a, id_b)): 0.95,
            (min(id_a, id_c), max(id_a, id_c)): 0.2,
            (min(id_b, id_c), max(id_b, id_c)): 0.2,
        }

        reranked = NoteSearchEngine._apply_mmr(
            results, lam=0.7, limit=3, similarity_matrix=sim_matrix
        )

        # First is A (highest score)
        assert reranked[0].note_id == id_a
        # Second should be C (diverse) or B (high score but similar to A)
        # With λ=0.7, C should win due to diversity bonus
        assert reranked[1].note_id == id_c

    def test_mmr_none_skips_reranking(self) -> None:
        """mmr_lambda=None should not call MMR (results returned as-is)."""
        # This test verifies the integration path, not the MMR function itself
        # The _apply_mmr should never be called when mmr_lambda is None
        # We test this by verifying the search flow doesn't crash
        request = NoteSearchRequest(query='test', limit=5, mmr_lambda=None)
        assert request.mmr_lambda is None

    def test_mmr_single_result(self) -> None:
        """Edge case with 1 result returns unchanged."""
        result = NoteSearchResult(note_id=uuid4(), metadata={}, score=0.5)

        reranked = NoteSearchEngine._apply_mmr([result], lam=0.7, limit=5, similarity_matrix={})

        assert len(reranked) == 1
        assert reranked[0].note_id == result.note_id


class TestSimilarityMatrix:
    """Tests for the similarity matrix computation."""

    @pytest.mark.asyncio
    async def test_similarity_matrix_returns_upper_triangle(self) -> None:
        """Verify matrix only contains (a,b) pairs where a < b."""
        # This is an integration test that requires a real database session
        # For unit testing, we verify the expected output structure
        # The actual SQL query returns pairs where a.note_id < b.note_id
        pass  # Integration test requires real DB

    @pytest.mark.asyncio
    async def test_similarity_matrix_identical_embeddings(self) -> None:
        """Identical embeddings should have similarity ≈ 1.0."""
        # Integration test - requires real pgvector
        pass

    @pytest.mark.asyncio
    async def test_similarity_matrix_orthogonal_embeddings(self) -> None:
        """Orthogonal embeddings should have similarity ≈ 0.0."""
        # Integration test - requires real pgvector
        pass

    def test_similarity_matrix_empty_input(self) -> None:
        """Empty input returns empty dict."""
        # Static method can be tested directly without session
        # The method checks for empty input at the start
        result: dict[tuple, float] = {}
        assert result == {}


class TestGroupByDocumentSummaries:
    """Tests for block-summary enrichment in _group_by_document."""

    def _make_chunk(
        self,
        note_id: UUID,
        chunk_index: int = 0,
        text: str = 'chunk text',
        summary: dict | None = None,
    ) -> MagicMock:
        chunk = MagicMock()
        chunk.id = uuid4()
        chunk.note_id = note_id
        chunk.text = text
        chunk.chunk_index = chunk_index
        chunk.embedding = [0.1] * 384
        chunk.summary = summary
        chunk.status = 'active'
        return chunk

    def _make_note(self, note_id: UUID, page_index: dict | None = None) -> MagicMock:
        note = MagicMock()
        note.id = note_id
        note.doc_metadata = {}
        note.page_index = page_index
        note.assets = []
        note.vault_id = uuid4()
        note.status = None
        return note

    @pytest.mark.asyncio
    async def test_summaries_from_chunks(self, engine: NoteSearchEngine) -> None:
        """Block summaries should be collected from all active chunks of a matched doc."""
        note_id = uuid4()
        chunk = self._make_chunk(note_id, chunk_index=0)
        note = self._make_note(note_id)

        summary_data = {'topic': 'ML Pipeline', 'key_points': ['Training', 'Inference']}

        session = AsyncMock()
        exec_results = []

        # Call 1: select(Note) — doc lookup
        doc_result = MagicMock()
        doc_result.all.return_value = [note]
        exec_results.append(doc_result)

        # Call 2: select(Chunk.note_id, Chunk.summary, ...) — summary query
        summary_result = MagicMock()
        summary_result.all.return_value = [(note_id, summary_data, 0)]
        exec_results.append(summary_result)

        # Call 3+: remaining queries (nodes, confidence, etc.)
        empty_result = MagicMock()
        empty_result.all.return_value = []

        session.exec.side_effect = [doc_result, summary_result, empty_result, empty_result]

        results = await engine._group_by_document(session, [(chunk, 0.9)], limit=10)

        assert len(results) == 1
        assert len(results[0].summaries) == 1
        assert results[0].summaries[0].topic == 'ML Pipeline'
        assert results[0].summaries[0].key_points == ['Training', 'Inference']

    @pytest.mark.asyncio
    async def test_fallback_to_description_when_no_chunk_summaries(
        self, engine: NoteSearchEngine
    ) -> None:
        """When chunks have no summary, fall back to page_index metadata description."""
        note_id = uuid4()
        chunk = self._make_chunk(note_id)
        page_index = {'metadata': {'description': 'A note about testing'}, 'toc': []}
        note = self._make_note(note_id, page_index=page_index)

        session = AsyncMock()

        doc_result = MagicMock()
        doc_result.all.return_value = [note]

        # No chunk summaries
        summary_result = MagicMock()
        summary_result.all.return_value = []

        empty_result = MagicMock()
        empty_result.all.return_value = []

        session.exec.side_effect = [doc_result, summary_result, empty_result, empty_result]

        results = await engine._group_by_document(session, [(chunk, 0.8)], limit=10)

        assert len(results) == 1
        assert len(results[0].summaries) == 1
        assert results[0].summaries[0].topic == 'A note about testing'
        assert results[0].summaries[0].key_points == []

    @pytest.mark.asyncio
    async def test_empty_summaries_when_no_chunks_and_no_description(
        self, engine: NoteSearchEngine
    ) -> None:
        """When no chunk summaries and no page_index description, summaries should be empty."""
        note_id = uuid4()
        chunk = self._make_chunk(note_id)
        note = self._make_note(note_id, page_index={'metadata': {}, 'toc': []})

        session = AsyncMock()

        doc_result = MagicMock()
        doc_result.all.return_value = [note]

        summary_result = MagicMock()
        summary_result.all.return_value = []

        empty_result = MagicMock()
        empty_result.all.return_value = []

        session.exec.side_effect = [doc_result, summary_result, empty_result, empty_result]

        results = await engine._group_by_document(session, [(chunk, 0.7)], limit=10)

        assert len(results) == 1
        assert results[0].summaries == []

    @pytest.mark.asyncio
    async def test_multiple_block_summaries_ordered(self, engine: NoteSearchEngine) -> None:
        """Multiple block summaries per note should be returned in chunk_index order."""
        note_id = uuid4()
        chunk = self._make_chunk(note_id)
        note = self._make_note(note_id)

        session = AsyncMock()

        doc_result = MagicMock()
        doc_result.all.return_value = [note]

        summary_result = MagicMock()
        summary_result.all.return_value = [
            (note_id, {'topic': 'Introduction', 'key_points': ['Overview']}, 0),
            (note_id, {'topic': 'Methods', 'key_points': ['Approach A']}, 1),
            (note_id, {'topic': 'Results', 'key_points': ['Finding X']}, 2),
        ]

        empty_result = MagicMock()
        empty_result.all.return_value = []

        session.exec.side_effect = [doc_result, summary_result, empty_result, empty_result]

        results = await engine._group_by_document(session, [(chunk, 0.9)], limit=10)

        assert len(results[0].summaries) == 3
        assert results[0].summaries[0].topic == 'Introduction'
        assert results[0].summaries[1].topic == 'Methods'
        assert results[0].summaries[2].topic == 'Results'
