import os
import re
import pytest
import nest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
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
    os.environ['MEMEX_SERVER__DEFAULT_ACTIVE_VAULT'] = 'test-vault'

    yield

    os.environ.pop('MEMEX_CLI__SERVER_URL', None)
    os.environ.pop('MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL', None)
    os.environ.pop('MEMEX_SERVER__DEFAULT_ACTIVE_VAULT', None)


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

    async with lifespan(server_app):
        with (
            patch('memex_cli.utils.httpx.AsyncClient') as mock_client_class,
            patch(
                'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
            ) as mock_run_dspy,
            patch(
                'memex_core.processing.dates.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=MagicMock(extracted_date=None),
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
            mock_run_dspy.return_value = mock_prediction

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


def _extract_note_id(output: str) -> str | None:
    """Extract note UUID from CLI output (handles both dashed and hex formats)."""
    # Try dashed UUID first
    match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', output)
    if match:
        return match.group(0)
    # Try plain 32-char hex (e.g. "UUID: 5c8f9f4c35964b141b991f07f230b84a")
    match = re.search(r'UUID:\s*([0-9a-f]{32})', output)
    if match:
        hex_id = match.group(1)
        return f'{hex_id[:8]}-{hex_id[8:12]}-{hex_id[12:16]}-{hex_id[16:20]}-{hex_id[20:]}'
    return None


@pytest.mark.asyncio
async def test_cli_note_add_asset_to_existing(db_session: AsyncSession, setup_cli_e2e, tmp_path):
    """Test 'memex note assets add <note_id> --asset <file>' adds asset to existing note."""
    # Create dummy assets
    original_asset = tmp_path / 'original.png'
    original_asset.write_bytes(b'original asset')
    new_asset_content = b'new asset content'
    new_asset = tmp_path / 'new_image.jpg'
    new_asset.write_bytes(new_asset_content)

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

    def _make_client(**kwargs):
        return AsyncClient(transport=ASGITransport(app=server_app), base_url='http://test/api/v1/')

    async with lifespan(server_app):
        with (
            patch('memex_cli.utils.httpx.AsyncClient', side_effect=_make_client),
            patch(
                'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
            ) as mock_run_dspy,
            patch(
                'memex_core.processing.dates.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=MagicMock(extracted_date=None),
            ),
            patch(
                'memex_core.memory.extraction.engine.get_embedding_model',
                return_value=MockEmbedder(),
            ),
            patch('memex_core.memory.extraction.engine.dspy.LM', side_effect=MockLM),
        ):
            mock_run_dspy.return_value = mock_prediction

            # Step 1: Create a note with an asset
            content = f'Note for asset add test {uuid4()}'
            result = runner.invoke(
                app,
                ['note', 'add', content, '--asset', str(original_asset)],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to create note: {result.stdout}'

            note_id = _extract_note_id(result.stdout)
            assert note_id is not None, f'Could not find note UUID in output: {result.stdout}'

            # Step 2: Add a new asset to the existing note
            result = runner.invoke(
                app,
                ['note', 'assets', 'add', note_id, '--asset', str(new_asset)],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to add asset: {result.stdout}'
            assert 'Added 1 asset(s)' in result.stdout

            # Step 3: Verify the new asset is stored
            found_new_assets = list(tmp_path.rglob('new_image.jpg'))
            stored_new = next((p for p in found_new_assets if 'assets' in str(p)), None)
            assert stored_new is not None, 'New asset was not stored in filestore'
            assert stored_new.read_bytes() == new_asset_content

            # Step 4: List assets and verify both are present
            result = runner.invoke(
                app,
                ['note', 'assets', 'list', note_id, '--json'],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to list assets: {result.stdout}'
            import json

            assets_list = json.loads(result.stdout)
            filenames = [a['filename'] for a in assets_list]
            assert 'original.png' in filenames
            assert 'new_image.jpg' in filenames


@pytest.mark.asyncio
async def test_cli_note_delete_asset(db_session: AsyncSession, setup_cli_e2e, tmp_path):
    """Test 'memex note assets delete <note_id> <path>' removes asset from note."""
    asset_content = b'Asset to delete'
    asset_file = tmp_path / 'deleteme.png'
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

    def _make_client(**kwargs):
        return AsyncClient(transport=ASGITransport(app=server_app), base_url='http://test/api/v1/')

    async with lifespan(server_app):
        with (
            patch('memex_cli.utils.httpx.AsyncClient', side_effect=_make_client),
            patch(
                'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
            ) as mock_run_dspy,
            patch(
                'memex_core.processing.dates.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=MagicMock(extracted_date=None),
            ),
            patch(
                'memex_core.memory.extraction.engine.get_embedding_model',
                return_value=MockEmbedder(),
            ),
            patch('memex_core.memory.extraction.engine.dspy.LM', side_effect=MockLM),
        ):
            mock_run_dspy.return_value = mock_prediction

            # Step 1: Create a note with an asset
            content = f'Note for asset delete test {uuid4()}'
            result = runner.invoke(
                app,
                ['note', 'add', content, '--asset', str(asset_file)],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to create note: {result.stdout}'

            note_id = _extract_note_id(result.stdout)
            assert note_id is not None, f'Could not find note UUID in output: {result.stdout}'

            # Step 2: List assets to get the full path
            result = runner.invoke(
                app,
                ['note', 'assets', 'list', note_id, '--json'],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to list assets: {result.stdout}'
            import json

            assets_list = json.loads(result.stdout)
            assert len(assets_list) >= 1
            asset_path = assets_list[0]['path']

            # Step 3: Delete the asset
            result = runner.invoke(
                app,
                ['note', 'assets', 'delete', note_id, asset_path],
                env=os.environ,
            )
            assert result.exit_code == 0, f'Failed to delete asset: {result.stdout}'
            assert 'Deleted 1 asset(s)' in result.stdout

            # Step 4: Verify asset is gone from listing
            result = runner.invoke(
                app,
                ['note', 'assets', 'list', note_id],
                env=os.environ,
            )
            assert result.exit_code == 0
            assert 'No assets found' in result.stdout
