import pytest
from unittest.mock import MagicMock, patch
import numpy as np
import onnxruntime as ort
from typing import AsyncGenerator, cast

import memex_core.memory.models.embedding as _emb_mod
from memex_core.memory.models.embedding import get_embedding_model, FastEmbedder


@pytest.fixture(autouse=True)
def _clear_embedding_cache() -> None:
    """Clear the module-level ONNX embedding cache between tests."""
    _emb_mod._onnx_embedder_cache = None


@pytest.fixture
async def mock_base_onnx_init() -> AsyncGenerator[MagicMock, None]:
    with patch(
        'memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None
    ) as mock_init:
        yield mock_init


@pytest.mark.asyncio
async def test_get_embedding_model_defaults(mock_base_onnx_init: MagicMock) -> None:
    with patch('pathlib.Path.exists', return_value=True):
        model = await get_embedding_model()
        assert isinstance(model, FastEmbedder)
        assert model.batch_size == 0
        mock_base_onnx_init.assert_called_once()
        _, kwargs = mock_base_onnx_init.call_args
        model_dir = str(kwargs['model_dir'])
        assert 'minilm-l12-v2-hindsight-embeddings' in model_dir
        assert model_dir.endswith('/main')


@pytest.mark.asyncio
async def test_get_embedding_model_batch_size(mock_base_onnx_init: MagicMock) -> None:
    with patch('pathlib.Path.exists', return_value=True):
        model = await get_embedding_model(batch_size=8)
        assert isinstance(model, FastEmbedder)
        assert model.batch_size == 8


@pytest.mark.asyncio
async def test_get_embedding_model_cache_updates_batch_size(
    mock_base_onnx_init: MagicMock,
) -> None:
    with patch('pathlib.Path.exists', return_value=True):
        model_a = await get_embedding_model(batch_size=0)
        model_b = await get_embedding_model(batch_size=8)
        assert model_a is model_b
        assert model_b.batch_size == 8


class TestFastEmbedder:
    def test_encode(self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            embedder = FastEmbedder('/fake/path')

        expected_embedding = np.random.rand(2, 384).astype(np.float32)

        # Cast session to MagicMock to avoid type checking errors
        mock_session = cast(MagicMock, embedder.session)
        mock_session.run.return_value = [expected_embedding]

        texts = ['hello', 'world']
        result = embedder.encode(texts)

        assert np.array_equal(result, expected_embedding)

        mock_tokenizer_instance = cast(MagicMock, embedder.tokenizer)
        mock_tokenizer_instance.encode_batch.assert_called_once_with(texts)

        call_args = mock_session.run.call_args
        inputs = call_args[0][1]

        assert 'input_ids' in inputs
        assert 'attention_mask' in inputs
        assert inputs['input_ids'].shape[0] == 2

    def test_encode_empty(self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            embedder = FastEmbedder('/fake/path')

        result = embedder.encode([])
        assert result.shape == (0, 0)
        assert result.dtype == np.float32

    def test_encode_batched(self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock) -> None:
        with patch('pathlib.Path.exists', return_value=True):
            embedder = FastEmbedder('/fake/path', batch_size=2)

        dim = 384
        batch_1 = np.random.rand(2, dim).astype(np.float32)
        batch_2 = np.random.rand(2, dim).astype(np.float32)
        batch_3 = np.random.rand(1, dim).astype(np.float32)

        mock_session = cast(MagicMock, embedder.session)
        mock_session.run.side_effect = [[batch_1], [batch_2], [batch_3]]

        texts = ['a', 'b', 'c', 'd', 'e']
        result = embedder.encode(texts)

        assert result.shape == (5, dim)
        assert np.array_equal(result[:2], batch_1)
        assert np.array_equal(result[2:4], batch_2)
        assert np.array_equal(result[4:], batch_3)
        assert mock_session.run.call_count == 3

    def test_encode_halves_on_oom(
        self, mock_tokenizer: MagicMock, mock_onnx_session: MagicMock
    ) -> None:
        """When GPU OOMs on a batch of 4, it should halve to 2 and succeed."""
        with patch('pathlib.Path.exists', return_value=True):
            embedder = FastEmbedder('/fake/path', batch_size=4)

        dim = 384
        oom = ort.capi.onnxruntime_pybind11_state.RuntimeException(
            'Failed to allocate memory for requested buffer'
        )
        half_1 = np.random.rand(2, dim).astype(np.float32)
        half_2 = np.random.rand(2, dim).astype(np.float32)

        mock_session = cast(MagicMock, embedder.session)
        # First call (batch=4) OOMs, then two calls (batch=2) succeed
        mock_session.run.side_effect = [oom, [half_1], [half_2]]

        result = embedder.encode(['a', 'b', 'c', 'd'])

        assert result.shape == (4, dim)
        assert np.array_equal(result[:2], half_1)
        assert np.array_equal(result[2:], half_2)
