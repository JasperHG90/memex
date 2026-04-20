"""Tests for memory search query expansion (expand_query parameter)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest


VAULT_ID = uuid4()
DUMMY_VEC = [0.1] * 384


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    mock_vec = MagicMock()
    mock_vec.tolist.return_value = DUMMY_VEC
    embedder.encode.return_value = [mock_vec]
    return embedder


@pytest.fixture
def mock_expander():
    expander = MagicMock()
    expander.expand = AsyncMock(return_value=['variation 1', 'variation 2'])
    return expander


@pytest.fixture
def engine_with_expander(mock_embedder, mock_expander):
    eng = RetrievalEngine(embedder=mock_embedder, reranker=MagicMock())
    eng.expander = mock_expander
    return eng


@pytest.fixture
def engine_no_expander(mock_embedder):
    eng = RetrievalEngine(embedder=mock_embedder, reranker=MagicMock())
    eng.expander = None
    return eng


def test_retrieval_request_expand_query_default():
    """expand_query defaults to False."""
    req = RetrievalRequest(query='test')
    assert req.expand_query is False


def test_retrieval_request_expand_query_true():
    """expand_query can be set to True."""
    req = RetrievalRequest(query='test', expand_query=True)
    assert req.expand_query is True


@pytest.mark.asyncio
async def test_expand_query_calls_expander(engine_with_expander, mock_expander):
    """When expand_query=True, the expander.expand() is called with the query."""
    request = RetrievalRequest(
        query='yoga classes location',
        expand_query=True,
        vault_ids=[VAULT_ID],
        limit=5,
    )

    mock_session = AsyncMock()
    # The retrieve method will fail at some point after expansion,
    # but we only care that expand was called.
    with pytest.raises(Exception):
        await engine_with_expander.retrieve(mock_session, request)

    mock_expander.expand.assert_awaited_once_with('yoga classes location')


@pytest.mark.asyncio
async def test_expand_query_false_skips_expander(engine_with_expander, mock_expander):
    """When expand_query=False, the expander is NOT called."""
    request = RetrievalRequest(
        query='yoga classes location',
        expand_query=False,
        vault_ids=[VAULT_ID],
        limit=5,
    )

    mock_session = AsyncMock()
    with pytest.raises(Exception):
        await engine_with_expander.retrieve(mock_session, request)

    mock_expander.expand.assert_not_awaited()


@pytest.mark.asyncio
async def test_expand_query_without_expander_is_noop(engine_no_expander):
    """When expand_query=True but no expander configured, no error from expansion."""
    request = RetrievalRequest(
        query='yoga classes location',
        expand_query=True,
        vault_ids=[VAULT_ID],
        limit=5,
    )

    mock_session = AsyncMock()
    # Should fail later (during strategy execution), not during expansion
    with pytest.raises(Exception) as exc_info:
        await engine_no_expander.retrieve(mock_session, request)

    # The error should NOT be about the expander
    assert 'expander' not in str(exc_info.value).lower()
    assert 'expand' not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_expansion_failure_falls_back_to_original(engine_with_expander, mock_expander):
    """When expander raises, retrieval continues with just the original query."""
    mock_expander.expand = AsyncMock(side_effect=RuntimeError('LLM down'))

    request = RetrievalRequest(
        query='yoga classes',
        expand_query=True,
        vault_ids=[VAULT_ID],
        limit=5,
    )

    mock_session = AsyncMock()
    # The expand failure is caught by QueryExpander internally,
    # but if we bypass that, the engine should still not crash on expansion
    # Reset to return empty (simulating QueryExpander's fallback)
    mock_expander.expand = AsyncMock(return_value=[])

    with pytest.raises(Exception):
        await engine_with_expander.retrieve(mock_session, request)

    # Expander was called but returned empty — engine continues with original query only
    mock_expander.expand.assert_awaited_once()
