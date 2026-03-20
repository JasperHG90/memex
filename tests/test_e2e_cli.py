import os
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport

from memex_cli import app
from memex_core.server import app as server_app
from memex_common.client import RemoteMemexAPI

runner = CliRunner()


@asynccontextmanager
async def _mock_api_context(*_args, **_kwargs):
    """Replacement for ``get_api_context`` that routes CLI requests through the
    in-process FastAPI app via ASGITransport instead of a real HTTP server.

    The server's own lifespan handles MemexAPI initialization, so we do not
    need to duplicate that logic here.
    """
    from memex_core.server import lifespan

    async with lifespan(server_app):
        async with AsyncClient(
            transport=ASGITransport(app=server_app),
            base_url='http://test/api/v1/',
        ) as client:
            yield RemoteMemexAPI(client)


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

    with patch('memex_cli.vaults.get_api_context', _mock_api_context):
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

    with patch('memex_cli.vaults.get_api_context', _mock_api_context):
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
