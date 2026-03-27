"""Tests for the LiteLLM reranker adapter."""

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from memex_common.config import LitellmRerankerBackend
from memex_core.memory.models.backends.litellm_reranker import LiteLLMReranker
from memex_core.memory.models.protocols import RerankerModel


def _make_rerank_response(results: list[dict]) -> MagicMock:
    """Create a mock litellm.RerankResponse."""
    response = MagicMock()
    response.results = results
    return response


class TestLiteLLMReranker:
    def test_satisfies_protocol(self) -> None:
        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        reranker = LiteLLMReranker(config)
        assert isinstance(reranker, RerankerModel)

    @patch('memex_core.memory.models.backends.litellm_reranker.litellm')
    def test_score_restores_original_order(self, mock_litellm: MagicMock) -> None:
        """litellm returns results sorted by relevance; score() must restore input order."""
        mock_litellm.rerank.return_value = _make_rerank_response(
            [
                {'index': 1, 'relevance_score': 0.9},
                {'index': 0, 'relevance_score': 0.3},
                {'index': 2, 'relevance_score': 0.1},
            ]
        )

        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        reranker = LiteLLMReranker(config)
        scores = reranker.score('query', ['doc0', 'doc1', 'doc2'])

        assert isinstance(scores, np.ndarray)
        assert scores.shape == (3,)

        # After sigmoid, should recover original relevance_score values
        recovered = [1.0 / (1.0 + math.exp(-s)) for s in scores]
        assert abs(recovered[0] - 0.3) < 1e-5
        assert abs(recovered[1] - 0.9) < 1e-5
        assert abs(recovered[2] - 0.1) < 1e-5

    @patch('memex_core.memory.models.backends.litellm_reranker.litellm')
    def test_score_logit_transform(self, mock_litellm: MagicMock) -> None:
        """Verify the inverse-sigmoid (logit) transform is correct."""
        relevance = 0.75
        mock_litellm.rerank.return_value = _make_rerank_response(
            [
                {'index': 0, 'relevance_score': relevance},
            ]
        )

        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        reranker = LiteLLMReranker(config)
        scores = reranker.score('q', ['doc'])

        # logit(0.75) = log(0.75 / 0.25) = log(3)
        expected_logit = math.log(relevance / (1 - relevance))
        np.testing.assert_allclose(scores[0], expected_logit, atol=1e-5)

        # Applying sigmoid should recover the original
        sigmoid = 1.0 / (1.0 + math.exp(-scores[0]))
        assert abs(sigmoid - relevance) < 1e-5

    @patch('memex_core.memory.models.backends.litellm_reranker.litellm')
    def test_passes_api_base_and_key(self, mock_litellm: MagicMock) -> None:
        mock_litellm.rerank.return_value = _make_rerank_response(
            [
                {'index': 0, 'relevance_score': 0.5},
            ]
        )

        config = LitellmRerankerBackend(
            model='cohere/rerank-v3.5',
            api_base='http://localhost:8080',
            api_key='test-key',
        )
        reranker = LiteLLMReranker(config)
        reranker.score('q', ['doc'])

        mock_litellm.rerank.assert_called_once_with(
            model='cohere/rerank-v3.5',
            query='q',
            documents=['doc'],
            return_documents=False,
            api_base='http://localhost:8080/',
            api_key='test-key',
        )

    def test_score_empty_raises(self) -> None:
        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        reranker = LiteLLMReranker(config)
        with pytest.raises(ValueError, match='Empty text list'):
            reranker.score('q', [])

    @patch('memex_core.memory.models.backends.litellm_reranker.litellm')
    def test_extreme_scores_clamped(self, mock_litellm: MagicMock) -> None:
        """Scores of 0.0 and 1.0 should be clamped to avoid log(0)."""
        mock_litellm.rerank.return_value = _make_rerank_response(
            [
                {'index': 0, 'relevance_score': 0.0},
                {'index': 1, 'relevance_score': 1.0},
            ]
        )

        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        reranker = LiteLLMReranker(config)
        scores = reranker.score('q', ['doc0', 'doc1'])

        # Should not raise or produce inf/nan
        assert np.all(np.isfinite(scores))
        # Score for 0.0 should be very negative, for 1.0 very positive
        assert scores[0] < -10
        assert scores[1] > 10
