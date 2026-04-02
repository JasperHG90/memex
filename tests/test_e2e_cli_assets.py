import json
import re

import pytest
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4

from memex_cli import app
from memex_core.server import app as server_app, lifespan
from memex_core.memory.extraction.models import ExtractedOutput, RawFact
from memex_common.client import RemoteMemexAPI
from memex_common.types import FactTypes, FactKindTypes

runner = CliRunner()

# CLI --set flags shared by every invocation in this module.
_CLI_SET_FLAGS = [
    '--set',
    'server_url=http://test',
    '--set',
    'server.memory.extraction.model.model=gemini/flash',
    '--set',
    'server.default_active_vault=global',
]


@asynccontextmanager
async def _mock_api_context(*_args, **_kwargs):
    """Replacement for ``get_api_context`` that routes CLI requests through the
    in-process FastAPI app via ASGITransport instead of a real HTTP server."""
    async with lifespan(server_app):
        async with AsyncClient(
            transport=ASGITransport(app=server_app),
            base_url='http://test/api/v1/',
        ) as client:
            yield RemoteMemexAPI(client)


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


def _make_mock_prediction():
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
    return mock_prediction


@contextmanager
def _all_patches():
    """Context manager that patches get_api_context and LLM internals."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in (
            patch('memex_cli.notes.get_api_context', _mock_api_context),
            patch('memex_cli.assets.get_api_context', _mock_api_context),
            patch(
                'memex_core.memory.extraction.core.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_make_mock_prediction(),
            ),
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
            stack.enter_context(p)
        yield


def _extract_note_id(output: str) -> str | None:
    """Extract note UUID from CLI output (handles both dashed and hex formats)."""
    match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', output)
    if match:
        return match.group(0)
    match = re.search(r'UUID:\s*([0-9a-f]{32})', output)
    if match:
        hex_id = match.group(1)
        return f'{hex_id[:8]}-{hex_id[8:12]}-{hex_id[12:16]}-{hex_id[16:20]}-{hex_id[20:]}'
    return None


@pytest.mark.integration
def test_cli_memory_add_with_asset(postgres_container, tmp_path):
    """Test 'memex note add "content" --asset <file>'."""
    asset_content = b'Binary asset content'
    asset_file = tmp_path / 'test_image.png'
    asset_file.write_bytes(asset_content)

    with _all_patches():
        content = f'Memory with asset {uuid4()}'
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'add', content, '--asset', str(asset_file)],
        )

        assert result.exit_code == 0, f'Command failed: {result.stdout}'
        assert 'Note added successfully' in result.stdout
        assert 'Loading 1 asset(s)...' in result.stdout

        found_assets = list(tmp_path.rglob('test_image.png'))
        assert len(found_assets) >= 2
        stored_asset = next((p for p in found_assets if 'assets' in str(p)), None)
        assert stored_asset is not None
        assert stored_asset.read_bytes() == asset_content


@pytest.mark.integration
def test_cli_note_add_asset_to_existing(postgres_container, tmp_path):
    """Test 'memex note assets add <note_id> --asset <file>' adds asset to existing note."""
    original_asset = tmp_path / 'original.png'
    original_asset.write_bytes(b'original asset')
    new_asset_content = b'new asset content'
    new_asset = tmp_path / 'new_image.jpg'
    new_asset.write_bytes(new_asset_content)

    with _all_patches():
        # Step 1: Create a note with an asset
        content = f'Note for asset add test {uuid4()}'
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'add', content, '--asset', str(original_asset)],
        )
        assert result.exit_code == 0, f'Failed to create note: {result.stdout}'

        note_id = _extract_note_id(result.stdout)
        assert note_id is not None, f'Could not find note UUID in output: {result.stdout}'

        # Step 2: Add a new asset to the existing note
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'assets', 'add', note_id, '--asset', str(new_asset)],
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
            [*_CLI_SET_FLAGS, 'note', 'assets', 'list', note_id, '--json'],
        )
        assert result.exit_code == 0, f'Failed to list assets: {result.stdout}'

        assets_list = json.loads(result.stdout)
        filenames = [a['filename'] for a in assets_list]
        assert 'original.png' in filenames
        assert 'new_image.jpg' in filenames


@pytest.mark.integration
def test_cli_note_delete_asset(postgres_container, tmp_path):
    """Test 'memex note assets delete <note_id> <path>' removes asset from note."""
    asset_content = b'Asset to delete'
    asset_file = tmp_path / 'deleteme.png'
    asset_file.write_bytes(asset_content)

    with _all_patches():
        # Step 1: Create a note with an asset
        content = f'Note for asset delete test {uuid4()}'
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'add', content, '--asset', str(asset_file)],
        )
        assert result.exit_code == 0, f'Failed to create note: {result.stdout}'

        note_id = _extract_note_id(result.stdout)
        assert note_id is not None, f'Could not find note UUID in output: {result.stdout}'

        # Step 2: List assets to get the full path
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'assets', 'list', note_id, '--json'],
        )
        assert result.exit_code == 0, f'Failed to list assets: {result.stdout}'

        assets_list = json.loads(result.stdout)
        assert len(assets_list) >= 1
        asset_path = assets_list[0]['path']

        # Step 3: Delete the asset
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'assets', 'delete', note_id, asset_path],
        )
        assert result.exit_code == 0, f'Failed to delete asset: {result.stdout}'
        assert 'Deleted 1 asset(s)' in result.stdout

        # Step 4: Verify asset is gone from listing
        result = runner.invoke(
            app,
            [*_CLI_SET_FLAGS, 'note', 'assets', 'list', note_id],
        )
        assert result.exit_code == 0
        assert 'No assets found' in result.stdout
