import os
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from memex_cli import app
from memex_core.server import app as server_app

runner = CliRunner()


class MockAsyncClientContext:
    """Mock context manager for httpx.AsyncClient"""

    def __init__(self, *args, **kwargs):
        self.base_url = kwargs.get('base_url', 'http://test')

    async def __aenter__(self):
        # Always initialize API for the current loop/context to avoid loop mismatch errors
        # (Since each runner.invoke creates a new loop, reusing the global state's API is dangerous)
        from memex_core.config import parse_memex_config
        from memex_core.storage.metastore import get_metastore
        from memex_core.storage.filestore import get_filestore
        from memex_core.api import MemexAPI
        from memex_core.memory.models import get_embedding_model, get_reranking_model, get_ner_model

        # Env vars should already be set by the test function
        config = parse_memex_config()
        metastore = get_metastore(config.server.meta_store)
        filestore = get_filestore(config.server.file_store)
        await metastore.connect()

        embedding_model = await get_embedding_model()
        reranking_model = await get_reranking_model()
        ner_model = await get_ner_model()

        api = MemexAPI(
            embedding_model=embedding_model,
            reranking_model=reranking_model,
            ner_model=ner_model,
            metastore=metastore,
            filestore=filestore,
            config=config,
        )
        await api.initialize()
        server_app.state.api = api

        # Create a new client connected to the FastAPI app
        self.client = AsyncClient(transport=ASGITransport(app=server_app), base_url=self.base_url)
        await self.client.__aenter__()
        return self.client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)
        # Close the API's metastore to release connections and avoid OOM
        if hasattr(server_app.state, 'api'):
            await server_app.state.api.metastore.close()


def _setup_env(postgres_container):
    from urllib.parse import urlparse

    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)

    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'


@pytest.mark.integration
def test_cli_vault_list(postgres_container):
    """Test 'memex vault list' command."""
    _setup_env(postgres_container)

    with patch('memex_cli.utils.httpx.AsyncClient', side_effect=MockAsyncClientContext):
        result = runner.invoke(
            app,
            [
                '--set',
                'server_url=http://test',
                '--set',
                'server.meta_store.type=postgres',
                '--set',
                'server.meta_store.instance.host=localhost',
                '--set',
                'server.meta_store.instance.database=dummy',
                '--set',
                'server.meta_store.instance.user=dummy',
                '--set',
                'server.meta_store.instance.password=dummy',
                '--set',
                'server.memory.extraction.model.model=gemini/gemini-3-flash-preview',
                'vault',
                'list',
            ],
        )

        assert result.exit_code == 0, f'Command failed: {result.stdout}'
        assert 'Active Vault' in result.stdout


@pytest.mark.integration
def test_cli_vault_create_delete(postgres_container):
    """Test creating and deleting a vault via CLI."""
    _setup_env(postgres_container)

    with patch('memex_cli.utils.httpx.AsyncClient', side_effect=MockAsyncClientContext):
        # Create
        result = runner.invoke(
            app,
            [
                '--set',
                'server_url=http://test',
                '--set',
                'server.meta_store.type=postgres',
                '--set',
                'server.meta_store.instance.host=localhost',
                '--set',
                'server.meta_store.instance.database=dummy',
                '--set',
                'server.meta_store.instance.user=dummy',
                '--set',
                'server.meta_store.instance.password=dummy',
                '--set',
                'server.memory.extraction.model.model=gemini/gemini-3-flash-preview',
                'vault',
                'create',
                'CLI Test Vault',
            ],
        )
        assert result.exit_code == 0, f'Create failed: {result.stdout}'
        assert 'CLI Test Vault' in result.stdout

        # Delete
        result = runner.invoke(
            app,
            [
                '--set',
                'server_url=http://test',
                '--set',
                'server.meta_store.type=postgres',
                '--set',
                'server.meta_store.instance.host=localhost',
                '--set',
                'server.meta_store.instance.database=dummy',
                '--set',
                'server.meta_store.instance.user=dummy',
                '--set',
                'server.meta_store.instance.password=dummy',
                '--set',
                'server.memory.extraction.model.model=gemini/gemini-3-flash-preview',
                'vault',
                'delete',
                'CLI Test Vault',
                '--force',
            ],
        )
        assert result.exit_code == 0, f'Delete failed: {result.stdout}'
        assert 'deleted successfully' in result.stdout
