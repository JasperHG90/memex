import httpx
from memex_common.client import RemoteMemexAPI
from memex_common.config import parse_memex_config

config = parse_memex_config()


class APIClient:
    _instance = None
    _client = None
    _api = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Use server_url from config
        base_url = config.server_url
        if not base_url.endswith('/api/v1/'):
            base_url = base_url.rstrip('/') + '/api/v1/'

        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._api = RemoteMemexAPI(self._client)

    @property
    def api(self) -> RemoteMemexAPI:
        return self._api

    async def close(self):
        if self._client:
            await self._client.aclose()


api_client: APIClient = APIClient.get_instance()
