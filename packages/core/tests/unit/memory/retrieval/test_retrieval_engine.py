from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import (
    MentalModel,
    Observation,
    EvidenceItem as ObservationEvidence,
    MemoryUnit,
)


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    # Return a dummy vector that has a .tolist() method
    mock_vec = MagicMock()
    mock_vec.tolist.return_value = [0.1] * 384
    embedder.encode.return_value = [mock_vec]
    return embedder


@pytest.fixture
def mock_reranker():
    reranker = MagicMock()
    # Return scores for 2 items
    reranker.score.return_value = [0.9, 0.1]
    return reranker


@pytest.mark.asyncio
async def test_convert_mm_to_units():
    """Test that MentalModels are correctly flattened into virtual MemoryUnits."""
    engine = RetrievalEngine(embedder=MagicMock(), reranker=MagicMock())

    mm_id = uuid4()
    # Evidence is needed for trend computation to not be 'stale'
    now = datetime.now(timezone.utc)
    ev = ObservationEvidence(memory_id=uuid4(), quote='q', timestamp=now)

    obs1 = Observation(title='Obs 1', content='Content 1', evidence=[ev])
    obs2 = Observation(title='Obs 2', content='Content 2', evidence=[])

    model = MentalModel(
        id=mm_id,
        name='Test Model',
        summary='Summary',
        observations=[obs1, obs2],
        last_refreshed=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    units = engine._convert_mm_to_units(model)

    assert len(units) == 2
    assert units[0].text == '[Test Model] Obs 1: Content 1'
    assert units[0].fact_type == 'observation'
    assert units[0].note_id == mm_id
    assert units[0].unit_metadata['observation'] is True
    # New observation with recent evidence defaults to 'new'
    assert units[0].unit_metadata['trend'] == 'new'

    assert units[1].text == '[Test Model] Obs 2: Content 2'
    assert units[1].unit_metadata['trend'] == 'stale'


@pytest.mark.asyncio
async def test_retrieve_empty_results(mock_embedder):
    """Test retrieval when no results are found in DB."""
    session = MagicMock(spec=AsyncSession)

    # Mock session.exec() to return an empty result
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result

    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)

    results, _ = await engine.retrieve(session, RetrievalRequest(query='test query'))

    assert results == []
    session.exec.assert_called()


@pytest.mark.asyncio
@patch('memex_core.memory.retrieval.engine.get_embedding_model')
@patch('memex_core.memory.retrieval.engine.get_reranking_model')
@patch('memex_core.memory.retrieval.engine.get_ner_model')
async def test_engine_init_defaults(mock_get_ner, mock_get_rerank, mock_get_embed):
    """Test that engine loads models if not provided."""
    from memex_core.memory.retrieval.engine import get_retrieval_engine

    mock_get_embed.return_value = MagicMock()
    mock_get_rerank.return_value = MagicMock()
    mock_get_ner.return_value = MagicMock()

    await get_retrieval_engine()

    mock_get_embed.assert_called_once()
    mock_get_rerank.assert_called_once()
    mock_get_ner.assert_called_once()


@pytest.mark.asyncio
async def test_token_budget_filtering(mock_embedder):
    """Test that results are filtered based on token budget using tiktoken."""
    engine = RetrievalEngine(embedder=mock_embedder, reranker=None)

    units = [
        MemoryUnit(id=uuid4(), text='Unit 1 content', fact_type='fact'),
        MemoryUnit(id=uuid4(), text='Unit 2 content', fact_type='fact'),
        MemoryUnit(id=uuid4(), text='Unit 3 content', fact_type='fact'),
    ]

    # "Unit 1 content" is 3 tokens in o200k_base
    # We want a budget that allows 2 units but not 3.
    # Each unit is ~3-4 tokens.
    # Budget of 10 should allow 2 units (approx 6-8 tokens).
    filtered = engine._filter_by_token_budget(units, budget=10)

    assert len(filtered) == 2
    assert filtered[0].text == 'Unit 1 content'
    assert filtered[1].text == 'Unit 2 content'


@pytest.mark.asyncio
async def test_min_score_filtering(mock_embedder, mock_reranker):
    """Test that results are filtered based on minimum score."""
    engine = RetrievalEngine(embedder=mock_embedder, reranker=mock_reranker)

    units = [
        MemoryUnit(id=uuid4(), text='High Relevance', fact_type='fact'),
        MemoryUnit(id=uuid4(), text='Low Relevance', fact_type='fact'),
    ]

    # Mock reranker scores: High=5.0 (sigmoid ~0.99), Low=-5.0 (sigmoid ~0.006)
    mock_reranker.score.return_value = [5.0, -5.0]

    # Filter with min_score=0.5
    # High (0.99) > 0.5 -> Keep
    # Low (0.006) < 0.5 -> Drop
    filtered = engine._rerank_results('query', units, min_score=0.5)

    assert len(filtered) == 1
    assert filtered[0].text == 'High Relevance'

    # Filter with min_score=0.0 (Keep all)
    filtered_all = engine._rerank_results('query', units, min_score=0.0)
    assert len(filtered_all) == 2

    # Filter with min_score=0.999 (Drop all)
    filtered_none = engine._rerank_results('query', units, min_score=0.999)
    assert len(filtered_none) == 0


def test_custom_retrieval_config_propagation():
    """Test that custom RetrievalConfig values propagate to engine and strategies."""
    from memex_common.config import RetrievalConfig
    from memex_core.memory.retrieval.strategies import GraphStrategy

    config = RetrievalConfig(
        similarity_threshold=0.5,
        temporal_decay_days=15.0,
        temporal_decay_base=3.0,
        rrf_k=40,
        candidate_pool_size=100,
    )

    engine = RetrievalEngine(
        embedder=MagicMock(),
        retrieval_config=config,
    )

    # Verify engine-level constants from config
    assert engine.k_rrf == 40
    assert engine.candidate_pool_size == 100

    # Verify graph strategy received config values
    graph_strategy, _ = engine.strategies['graph']
    assert isinstance(graph_strategy, GraphStrategy)
    assert graph_strategy.similarity_threshold == 0.5
    assert graph_strategy.temporal_decay_days == 15.0
    assert graph_strategy.temporal_decay_base == 3.0


def test_default_retrieval_config_when_none():
    """Test that default config is created when retrieval_config is None."""
    engine = RetrievalEngine(embedder=MagicMock())

    # Should use defaults from RetrievalConfig
    assert engine.k_rrf == 60
    assert engine.candidate_pool_size == 60
    assert engine.retrieval_config.similarity_threshold == 0.3
    assert engine.retrieval_config.temporal_decay_days == 30.0
    assert engine.retrieval_config.temporal_decay_base == 2.0
