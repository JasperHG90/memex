import os
import pytest
import nest_asyncio
from unittest.mock import patch, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from memex_cli import app
from memex_core.server import app as server_app, lifespan
from memex_core.memory.sql_models import ReflectionQueue, Entity
from memex_core.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME

nest_asyncio.apply()
runner = CliRunner()


@pytest.fixture(scope='function')
async def setup_cli_e2e():
    """Additional setup for CLI E2E tests."""
    os.environ['MEMEX_CLI__SERVER_URL'] = 'http://test'
    os.environ['MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL'] = 'gemini/flash'
    os.environ['MEMEX_SERVER__ACTIVE_VAULT'] = GLOBAL_VAULT_NAME

    yield

    os.environ.pop('MEMEX_CLI__SERVER_URL', None)
    os.environ.pop('MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL', None)
    os.environ.pop('MEMEX_SERVER__ACTIVE_VAULT', None)


@pytest.mark.asyncio
async def test_cli_reflect_e2e(db_session: AsyncSession, setup_cli_e2e):
    """
    Test 'memex memory reflect' via CLI.
    1. Seed an entity in the ReflectionQueue.
    2. Mock the Hindsight reflection process.
    3. Verify CLI output matches the async scheduling flow.
    """
    # 1. Seed Data
    entity_id = uuid4()
    entity = Entity(
        id=entity_id, canonical_name='Test Entity', type='person', vault_id=GLOBAL_VAULT_ID
    )
    db_session.add(entity)

    queue_item = ReflectionQueue(
        entity_id=entity_id,
        vault_id=GLOBAL_VAULT_ID,
        status='pending',
        priority_score=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(queue_item)
    await db_session.commit()

    # 2. Run reflection through the server
    async with lifespan(server_app):
        with (
            patch('memex_cli.utils.httpx.AsyncClient') as mock_client_class,
        ):
            mock_client = AsyncClient(
                transport=ASGITransport(app=server_app), base_url='http://test/api/v1/'
            )
            mock_client_class.return_value = mock_client

            with patch.object(
                server_app.state.api, 'background_reflect_batch', new_callable=AsyncMock
            ) as mock_reflect:
                # 3. Run CLI
                result = runner.invoke(app, ['memory', 'reflect', str(entity_id)], env=os.environ)

                assert result.exit_code == 0
                assert 'Batch Reflection Scheduled' in result.stdout
                assert 'Reflection is running in the background' in result.stdout
                mock_reflect.assert_called_once()
