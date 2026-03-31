import pytest
from unittest.mock import MagicMock, patch
import numpy as np
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
        mock_base_onnx_init.assert_called_once()
        _, kwargs = mock_base_onnx_init.call_args
        model_dir = str(kwargs['model_dir'])
        assert 'minilm-l12-v2-hindsight-embeddings' in model_dir
        assert model_dir.endswith('/main')


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
