import pytest
from uuid import uuid4
from unittest.mock import MagicMock, AsyncMock, patch
from memex_core.api import MemexAPI
from memex_common.config import MemexConfig


@pytest.fixture
def mock_deps():
    embedder = MagicMock()
    # Mock encode to return a list of floats (embedding)
    embedder.encode.return_value = MagicMock(tolist=lambda: [0.1] * 384)
    reranker = MagicMock()
    return embedder, reranker


@pytest.mark.asyncio
async def test_api_search_propagates_budget(mock_deps):
    embedder, reranker = mock_deps

    config = MemexConfig()
    config.server.memory.retrieval.token_budget = 500

    # Mock models
    embedder = MagicMock()
    reranker = MagicMock()
    ner_model = MagicMock()

    # Mock Storage
    mock_ms = MagicMock()
    mock_fs = MagicMock()

    with patch('dspy.LM', MagicMock()):
        api = MemexAPI(
            embedding_model=embedder,
            reranking_model=reranker,
            ner_model=ner_model,
            metastore=mock_ms,
            filestore=mock_fs,
            config=config,
        )

        # Mock internal retrieval engine to capture calls
        mock_retrieval = MagicMock()
        mock_retrieval.retrieve = AsyncMock(return_value=([], None))

        # Inject mock engine
        api._retrieval = mock_retrieval
        api.memory.retrieval = mock_retrieval

        # Mock vault resolution on the VaultService used by SearchService
        api._vaults.resolve_vault_identifier = AsyncMock(return_value=uuid4())

        # Test 1: No budget passed -> Should pass None (Engine handles default)
        await api.search('query')
        args = mock_retrieval.retrieve.call_args[0]
        # Args are (session, request)
        assert args[1].token_budget is None

        # Test 2: Budget passed -> Should pass value
        await api.search('query', token_budget=999)
        args = mock_retrieval.retrieve.call_args[0]
        assert args[1].token_budget == 999
