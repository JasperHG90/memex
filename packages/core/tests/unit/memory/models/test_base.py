import pathlib as plb
from unittest.mock import MagicMock, patch, AsyncMock
import httpx
import pytest

from memex_core.memory.models.base import BaseOnnxModel, ModelDownloader


class TestModelDownloader:
    def test_init(self) -> None:
        downloader = ModelDownloader('repo/id', app_name='app-name')
        assert downloader.repo_id == 'repo/id'
        assert downloader.app_name == 'app-name'
        assert 'repo__id' in str(downloader.cache_dir)

    @pytest.mark.asyncio
    async def test_fetch_file_list(self) -> None:
        downloader = ModelDownloader('repo/id')
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'siblings': [{'rfilename': 'file1.json'}, {'rfilename': 'model.onnx'}]
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        files = await downloader._fetch_file_list(mock_client)
        assert files == ['file1.json', 'model.onnx']
        mock_client.get.assert_called_once_with(downloader.api_url)

    @pytest.mark.asyncio
    async def test_download_file(self, tmp_path) -> None:
        downloader = ModelDownloader('repo/id')
        downloader.cache_dir = tmp_path

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = MagicMock(return_value=AsyncMock())
        mock_response.aiter_bytes.return_value.__aiter__.return_value = [b'chunk1', b'chunk2']

        # mock_client.stream is a context manager
        mock_client.stream.return_value.__aenter__.return_value = mock_response

        await downloader._download_file(mock_client, 'test.txt', force=False)

        local_path = tmp_path / 'test.txt'
        assert local_path.exists()
        assert local_path.read_bytes() == b'chunk1chunk2'


class TestBaseOnnxModel:
    def test_init(self) -> None:
        with (
            patch('memex_core.memory.models.base.Tokenizer') as mock_tokenizer,
            patch('memex_core.memory.models.base.ort.InferenceSession') as mock_session,
            patch('pathlib.Path.exists', return_value=True),
        ):
            model = BaseOnnxModel('/fake/path')

            assert model.model_path == plb.Path('/fake/path')
            mock_tokenizer.from_file.assert_called_once()
            mock_session.assert_called_once()
