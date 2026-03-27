"""Tests for the LiteLLM embedding adapter."""

from unittest.mock import MagicMock, patch

import numpy as np

from memex_common.config import LitellmEmbeddingBackend
from memex_core.memory.models.backends.litellm_embedder import LiteLLMEmbedder
from memex_core.memory.models.protocols import EmbeddingsModel


def _make_embedding_response(vectors: list[list[float]]) -> MagicMock:
    """Create a mock litellm.EmbeddingResponse."""
    response = MagicMock()
    response.data = [{'embedding': v} for v in vectors]
    return response


class TestLiteLLMEmbedder:
    def test_satisfies_protocol(self) -> None:
        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        embedder = LiteLLMEmbedder(config)
        assert isinstance(embedder, EmbeddingsModel)

    @patch('memex_core.memory.models.backends.litellm_embedder.litellm')
    def test_encode_returns_ndarray(self, mock_litellm: MagicMock) -> None:
        vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_litellm.embedding.return_value = _make_embedding_response(vectors)

        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        embedder = LiteLLMEmbedder(config)
        result = embedder.encode(['hello', 'world'])

        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 3)
        assert result.dtype == np.float32
        np.testing.assert_allclose(result, vectors, atol=1e-6)

    @patch('memex_core.memory.models.backends.litellm_embedder.litellm')
    def test_passes_api_base_and_key(self, mock_litellm: MagicMock) -> None:
        mock_litellm.embedding.return_value = _make_embedding_response([[0.1]])

        config = LitellmEmbeddingBackend(
            model='ollama/nomic-embed-text',
            api_base='http://localhost:11434',
            api_key='test-key',
            dimensions=384,
        )
        embedder = LiteLLMEmbedder(config)
        embedder.encode(['test'])

        mock_litellm.embedding.assert_called_once_with(
            model='ollama/nomic-embed-text',
            input=['test'],
            api_base='http://localhost:11434/',
            api_key='test-key',
            dimensions=384,
        )

    @patch('memex_core.memory.models.backends.litellm_embedder.litellm')
    def test_omits_none_kwargs(self, mock_litellm: MagicMock) -> None:
        """api_base/api_key/dimensions should not be passed when None."""
        mock_litellm.embedding.return_value = _make_embedding_response([[0.1]])

        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        embedder = LiteLLMEmbedder(config)
        embedder.encode(['test'])

        call_kwargs = mock_litellm.embedding.call_args
        assert 'api_base' not in call_kwargs.kwargs
        assert 'api_key' not in call_kwargs.kwargs
        assert 'dimensions' not in call_kwargs.kwargs

    @patch('memex_core.memory.models.backends.litellm_embedder.litellm')
    def test_single_text(self, mock_litellm: MagicMock) -> None:
        mock_litellm.embedding.return_value = _make_embedding_response([[0.5, 0.6]])

        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        embedder = LiteLLMEmbedder(config)
        result = embedder.encode(['single'])

        assert result.shape == (1, 2)
