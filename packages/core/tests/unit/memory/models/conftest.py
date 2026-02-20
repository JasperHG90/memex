import pytest
from unittest.mock import MagicMock, patch
from typing import Generator


@pytest.fixture
def mock_tokenizer() -> Generator[MagicMock, None, None]:
    with patch('memex_core.memory.models.base.Tokenizer') as MockTokenizer:
        # Setup the instance returned by from_file
        instance: MagicMock = MockTokenizer.from_file.return_value

        def side_effect_encode_batch(texts: list[str]) -> list[MagicMock]:
            encodings = []
            for _ in texts:
                encoding = MagicMock()
                # Create fake ids and masks
                encoding.ids = [101, 200, 300, 102]
                encoding.attention_mask = [1, 1, 1, 1]
                encoding.type_ids = [0, 0, 0, 0]
                encodings.append(encoding)
            return encodings

        instance.encode_batch.side_effect = side_effect_encode_batch

        yield MockTokenizer


@pytest.fixture
def mock_onnx_session() -> Generator[MagicMock, None, None]:
    with patch('memex_core.memory.models.base.ort.InferenceSession') as MockSession:
        yield MockSession


@pytest.fixture
def mock_httpx_get() -> Generator[MagicMock, None, None]:
    with patch('memex_core.memory.models.base.httpx.get') as mock_get:
        yield mock_get
