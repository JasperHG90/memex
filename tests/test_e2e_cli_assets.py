import os
import pytest
import nest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from memex_core.memory.sql_models import TokenUsage

from memex_cli import app
from memex_core.server import app as server_app, lifespan
from memex_core.memory.extraction.models import ExtractedOutput, RawFact
from memex_common.types import FactTypes, FactKindTypes

nest_asyncio.apply()
runner = CliRunner()


@pytest.fixture(scope='function')
async def setup_cli_e2e(db_session: AsyncSession):
    """Additional setup for CLI E2E tests."""
    os.environ['MEMEX_CLI__SERVER_URL'] = 'http://test'
    os.environ['MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL'] = 'gemini/flash'
    os.environ['MEMEX_SERVER__ACTIVE_VAULT'] = 'test-vault'

    yield

    os.environ.pop('MEMEX_CLI__SERVER_URL', None)
    os.environ.pop('MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL', None)
    os.environ.pop('MEMEX_SERVER__ACTIVE_VAULT', None)


class MockEmbedder:
    def encode(self, texts):
        import numpy as np

        return np.random.rand(len(texts), 384)


class MockLM:
    def __init__(self, model_name=None, **kwargs):
        self.model = model_name or 'mock-model'

    def __call__(self, *args, **kwargs):
        return ['Mocked LLM Response']

    def copy(self, **kwargs):
        return self


@pytest.mark.asyncio
async def test_cli_memory_add_with_asset(db_session: AsyncSession, setup_cli_e2e, tmp_path):
    """Test 'memex memory add "content" --asset <file>'."""
    # 1. Create a dummy asset
    asset_content = b'Binary asset content'
    asset_file = tmp_path / 'test_image.png'
    asset_file.write_bytes(asset_content)

    mock_raw_fact = RawFact(
        what='User added a memory with asset',
        when='today',
        fact_type=FactTypes.WORLD,
        fact_kind=FactKindTypes.CONVERSATION,
        entities=[],
        causal_relations=[],
    )
    mock_extracted_output = ExtractedOutput(extracted_facts=[mock_raw_fact])
    mock_prediction = MagicMock()
    mock_prediction.extracted_facts = mock_extracted_output
    mock_usage = TokenUsage(total_tokens=10)

    async with lifespan(server_app):
        with (
            patch('memex_cli.utils.httpx.AsyncClient') as mock_client_class,
            patch(
                'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
            ) as mock_run_dspy,
            patch(
                'memex_core.processing.dates.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=(MagicMock(extracted_date=None), TokenUsage(total_tokens=0)),
            ),
            patch(
                'memex_core.memory.extraction.engine.get_embedding_model',
                return_value=MockEmbedder(),
            ),
            patch('memex_core.memory.extraction.engine.dspy.LM', side_effect=MockLM),
        ):
            mock_client = AsyncClient(
                transport=ASGITransport(app=server_app), base_url='http://test/api/v1/'
            )
            mock_client_class.return_value = mock_client
            mock_run_dspy.return_value = (mock_prediction, mock_usage)

            content = f'Memory with asset {uuid4()}'
            result = runner.invoke(
                app, ['note', 'add', content, '--asset', str(asset_file)], env=os.environ
            )

            assert result.exit_code == 0
            assert 'Note added successfully' in result.stdout
            assert 'Loading 1 asset(s)...' in result.stdout

            found_assets = list(tmp_path.rglob('test_image.png'))
            assert len(found_assets) >= 2
            stored_asset = next((p for p in found_assets if 'assets' in str(p)), None)
            assert stored_asset is not None
            assert stored_asset.read_bytes() == asset_content
