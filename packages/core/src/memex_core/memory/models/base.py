import logging
import asyncio
import pathlib as plb

import httpx
import onnxruntime as ort
from tokenizers import Tokenizer
from platformdirs import user_cache_dir

logger = logging.getLogger('memex.core.memory.models.base')

# Shared ONNX Runtime options optimized for memory-constrained environments
options = ort.SessionOptions()  # type: ignore
options.log_severity_level = 3
# Disable memory pattern caching between runs to reduce resident memory
options.enable_mem_pattern = False
# Disable CPU memory arena to prevent the allocator from holding memory between runs
options.enable_cpu_mem_arena = False


class ModelDownloader:
    def __init__(self, repo_id: str, app_name: str = 'memex', max_concurrent: int = 5):
        self.repo_id = repo_id
        self.app_name = app_name
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.base_url = f'https://huggingface.co/{repo_id}'
        self.api_url = f'https://huggingface.co/api/models/{repo_id}'

        self.cache_dir = plb.Path(user_cache_dir(app_name)) / repo_id.replace('/', '__')

    async def _fetch_file_list(self, client: httpx.AsyncClient) -> list[str]:
        """
        Fetches the file list from the HF JSON API without using their library.
        """
        response = await client.get(self.api_url)

        if response.status_code == 404:
            raise ValueError(f"Repository '{self.repo_id}' not found.")

        response.raise_for_status()
        data = response.json()

        # The API returns a 'siblings' list containing dictionaries with 'rfilename'
        return [f['rfilename'] for f in data.get('siblings', [])]

    async def _download_file(self, client: httpx.AsyncClient, filename: str, force: bool):
        """
        Streams a single file from HF to the local cache.
        """
        local_path = self.cache_dir / filename

        if local_path.exists() and not force:
            return

        # Ensure directory structure exists (e.g. for 'onnx/model.onnx')
        local_path.parent.mkdir(parents=True, exist_ok=True)

        download_url = f'{self.base_url}/resolve/main/{filename}'

        async with self.semaphore:
            try:
                async with client.stream('GET', download_url, follow_redirects=True) as response:
                    response.raise_for_status()
                    with open(local_path, 'wb') as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
            except (httpx.HTTPError, OSError, RuntimeError) as e:
                print(f'  [Error] Failed to download {filename}: {e}')
                # Clean up partial corruption
                if local_path.exists():
                    local_path.unlink()

    async def download_async(self, client: httpx.AsyncClient, force: bool = False) -> plb.Path:
        """
        Main entry point: gets the list, creates tasks, and downloads.
        """
        print(f'Connecting to Hugging Face: {self.repo_id}...')
        try:
            files = await self._fetch_file_list(client)
        except (httpx.HTTPError, OSError, ValueError) as e:
            print(f'Failed to fetch repo info: {e}')
            raise e
        print(f'Found {len(files)} files. Syncing to: {self.cache_dir}')
        tasks = [self._download_file(client, filename, force) for filename in files]
        await asyncio.gather(*tasks)
        print('\nDownload complete.')
        return self.cache_dir

    def download(self, force: bool = False) -> plb.Path:
        """
        Synchronous wrapper around the async download method.
        """
        return asyncio.run(self.download_async(httpx.AsyncClient(), force=force))


class BaseOnnxModel:
    """Base wrapper for ONNX models using Tokenizers."""

    def __init__(self, model_dir: str | plb.Path, model_name: str = 'model.onnx'):
        self.model_path = plb.Path(model_dir)
        self.tokenizer: Tokenizer = Tokenizer.from_file(str(self.model_path / 'tokenizer.json'))
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        self.tokenizer.enable_truncation(max_length=512)

        self.session = ort.InferenceSession(
            str(self.model_path / model_name),
            providers=['CPUExecutionProvider'],
            sess_options=options,
        )
