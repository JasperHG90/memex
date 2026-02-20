import pytest
from unittest.mock import MagicMock, patch
import numpy as np
from typing import cast, AsyncGenerator

from memex_core.memory.models.reranking import get_reranking_model, FastReranker


@pytest.fixture
async def mock_base_onnx_init() -> AsyncGenerator[MagicMock, None]:
    get_reranking_model.cache_clear()
    with patch(
        'memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None
    ) as mock_init:
        yield mock_init
        get_reranking_model.cache_clear()


@pytest.mark.asyncio
async def test_get_reranking_model_defaults(mock_base_onnx_init: MagicMock) -> None:
    with patch('pathlib.Path.exists', return_value=True):
        model = await get_reranking_model()
        assert isinstance(model, FastReranker)
        _, kwargs = mock_base_onnx_init.call_args
        assert 'ms-marco-minilm-l12-hindsight-reranker' in str(kwargs['model_dir'])


class TestFastReranker:
    def test_score(self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            reranker = FastReranker('/fake/path')

        expected_scores = np.array([[0.9], [0.1]], dtype=np.float32)

        mock_session = cast(MagicMock, reranker.session)
        mock_session.run.return_value = [expected_scores]

        query = 'q'
        texts = ['d1', 'd2']
        scores = reranker.score(query, texts)

        assert len(scores) == 2
        assert scores[0] == 0.9
        assert scores[1] == 0.1

        expected_pairs = [('q', 'd1'), ('q', 'd2')]

        mock_tokenizer_instance = cast(MagicMock, reranker.tokenizer)
        mock_tokenizer_instance.encode_batch.assert_called_once_with(expected_pairs)

    def test_score_empty_raises(
        self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock
    ) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            reranker = FastReranker('/fake/path')
        with pytest.raises(ValueError, match='Empty text list'):
            reranker.score('q', [])

    def test_rerank(self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            reranker = FastReranker('/fake/path')

        with patch.object(reranker, 'score') as mock_score:
            mock_score.return_value = np.array([0.1, 0.9, 0.5])

            texts = ['low', 'high', 'mid']
            doc_ids = ['1', '2', '3']

            results = reranker.rerank('q', texts, doc_ids)

            assert len(results) == 3
            assert results[0]['id'] == '2'
            assert results[0]['score'] == 0.9
            assert results[1]['id'] == '3'
            assert results[2]['id'] == '1'

    def test_rerank_length_mismatch(
        self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock
    ) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            reranker = FastReranker('/fake/path')

        with patch.object(reranker, 'score', return_value=np.array([1.0])):
            with pytest.raises(ValueError, match='Length mismatch'):
                reranker.rerank('q', ['text'], [])
