import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_common.config import RetrievalConfig


@pytest.fixture
def mock_dependencies():
    embedder = MagicMock()
    embedder.encode.return_value = MagicMock(tolist=lambda: [0.1] * 1536)

    reranker = MagicMock()

    return embedder, reranker


@pytest.fixture
def engine(mock_dependencies):
    embedder, reranker = mock_dependencies
    config = RetrievalConfig(token_budget=100)
    return RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)


@pytest.fixture
def mock_units():
    """Create units with controllable text lengths."""
    u1 = MemoryUnit(id=uuid4(), text='Short unit', fact_type='fact')  # ~2 tokens
    u2 = MemoryUnit(id=uuid4(), text='Medium length unit text here', fact_type='fact')  # ~5 tokens
    u3 = MemoryUnit(id=uuid4(), text='Very long unit text ' * 10, fact_type='fact')  # ~40 tokens
    return [u1, u2, u3]


def test_filter_by_token_budget_basic(engine, mock_units):
    """Test basic budget filtering - all units fit within budget."""
    with patch('memex_core.memory.retrieval.engine.tiktoken.get_encoding') as mock_encoding:
        mock_enc_instance = MagicMock()
        # Return 1 token per word
        mock_enc_instance.encode.side_effect = lambda t: [0] * len(t.split())
        mock_encoding.return_value = mock_enc_instance

        # Budget of 100 should fit all units (2 + 5 + 40 = 47 tokens)
        results = engine._filter_by_token_budget(mock_units, budget=100)

        assert len(results) == 3


def test_filter_by_token_budget_partial_fit(engine, mock_units):
    """Test budget filtering where only some units fit."""
    with patch('memex_core.memory.retrieval.engine.tiktoken.get_encoding') as mock_encoding:
        mock_enc_instance = MagicMock()
        # Return 1 token per word
        mock_enc_instance.encode.side_effect = lambda t: [0] * len(t.split())
        mock_encoding.return_value = mock_enc_instance

        # Budget of 6: first unit fits (2), second doesn't (5 more would be 7)
        results = engine._filter_by_token_budget(mock_units, budget=6)

        assert len(results) == 1
        assert results[0].text == 'Short unit'


def test_filter_by_token_budget_hard_stop(engine, mock_units):
    """Verify strictly greedy packing (Hard Stop) - stops at first overflow."""
    with patch('memex_core.memory.retrieval.engine.tiktoken.get_encoding') as mock_encoding:
        mock_enc_instance = MagicMock()
        # Return 1 token per word
        mock_enc_instance.encode.side_effect = lambda t: [0] * len(t.split())
        mock_encoding.return_value = mock_enc_instance

        # Budget of 6: u1 (2) fits, u2 (5) would make total 7, so stop
        results = engine._filter_by_token_budget(mock_units, budget=6)

        assert len(results) == 1
        assert results[0].text == 'Short unit'


def test_filter_by_token_budget_many_units(engine):
    """Test budget filtering with many small units."""
    # Create 15 units with 2 tokens each
    many_units = [MemoryUnit(id=uuid4(), text='x y', fact_type='fact') for _ in range(15)]

    with patch('memex_core.memory.retrieval.engine.tiktoken.get_encoding') as mock_encoding:
        mock_enc_instance = MagicMock()
        # Return 1 token per word (2 tokens per unit)
        mock_enc_instance.encode.side_effect = lambda t: [0] * len(t.split())
        mock_encoding.return_value = mock_enc_instance

        # Budget of 100 should fit all 15 units (30 tokens total)
        results = engine._filter_by_token_budget(many_units, budget=100)

        assert len(results) == 15


def test_filter_by_token_budget_empty_list(engine):
    """Test budget filtering with empty list."""
    results = engine._filter_by_token_budget([], budget=100)
    assert len(results) == 0


def test_filter_by_token_budget_zero_budget(engine, mock_units):
    """Test budget filtering with zero budget."""
    results = engine._filter_by_token_budget(mock_units, budget=0)
    assert len(results) == 0
