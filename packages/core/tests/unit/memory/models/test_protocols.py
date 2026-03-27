"""Tests for inference model protocols."""

from unittest.mock import MagicMock, patch

import numpy as np

from memex_core.memory.models.protocols import EmbeddingsModel, RerankerModel
from memex_core.memory.models.embedding import FastEmbedder
from memex_core.memory.models.reranking import FastReranker


class TestProtocolConformance:
    """Verify that ONNX model classes satisfy the protocols structurally."""

    def test_fast_embedder_satisfies_protocol(self) -> None:
        with (
            patch('memex_core.memory.models.base.Tokenizer'),
            patch('memex_core.memory.models.base.ort.InferenceSession'),
            patch('pathlib.Path.exists', return_value=True),
        ):
            embedder = FastEmbedder('/fake/path')
        assert isinstance(embedder, EmbeddingsModel)

    def test_fast_reranker_satisfies_protocol(self) -> None:
        with (
            patch('memex_core.memory.models.base.Tokenizer'),
            patch('memex_core.memory.models.base.ort.InferenceSession'),
            patch('pathlib.Path.exists', return_value=True),
        ):
            reranker = FastReranker('/fake/path')
        assert isinstance(reranker, RerankerModel)

    def test_mock_embedder_satisfies_protocol(self) -> None:
        """MagicMock with encode method satisfies EmbeddingsModel."""
        mock = MagicMock()
        mock.encode = MagicMock(return_value=np.zeros((1, 384)))
        assert isinstance(mock, EmbeddingsModel)

    def test_mock_reranker_satisfies_protocol(self) -> None:
        """MagicMock with score method satisfies RerankerModel."""
        mock = MagicMock()
        mock.score = MagicMock(return_value=np.zeros(1))
        assert isinstance(mock, RerankerModel)
