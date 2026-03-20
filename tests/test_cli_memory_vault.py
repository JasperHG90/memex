import os
import pytest
import nest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from memex_core.memory.sql_models import Vault
from memex_cli import app
from memex_core.server import app as server_app
from memex_core.memory.extraction.models import (
    BlockSummary,
    ExtractedOutput,
    SectionSummary,
)
from memex_core.memory.sql_models import TokenUsage
from memex_core.api import MemexAPI
from memex_core.services.ingestion import IngestionService

nest_asyncio.apply()
runner = CliRunner()


class MockAsyncClientContext:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs.get('base_url', 'http://test')

    async def __aenter__(self):
        from memex_core.config import parse_memex_config
        from memex_core.storage.metastore import get_metastore
        from memex_core.storage.filestore import get_filestore

        config = parse_memex_config()
        config.server.memory.extraction.model.model = 'mock-model'

        metastore = get_metastore(config.server.meta_store)
        filestore = get_filestore(config.server.file_store)
        await metastore.connect()

        config.server.memory.reflection.background_reflection_enabled = False

        self.patches = [
            patch('dspy.LM', return_value=MagicMock()),
            patch('memex_core.memory.models.embedding.FastEmbedder'),
            patch('memex_core.memory.models.reranking.FastReranker'),
            patch('memex_core.memory.models.ner.FastNERModel'),
        ]

        self.mock_lm = self.patches[0].start()
        self.MockEmbedder = self.patches[1].start()
        self.patches[2].start()
        self.patches[3].start()

        mock_embedder = self.MockEmbedder.return_value
        mock_embedder.encode.return_value = [[0.1] * 384]

        api = MemexAPI(
            embedding_model=mock_embedder,
            reranking_model=MagicMock(),
            ner_model=MagicMock(),
            metastore=metastore,
            filestore=filestore,
            config=config,
        )
        await api.initialize()
        server_app.state.api = api

        self.client = AsyncClient(transport=ASGITransport(app=server_app), base_url=self.base_url)
        await self.client.__aenter__()
        return self.client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for p in reversed(self.patches):
            p.stop()

        await self.client.__aexit__(exc_type, exc_val, exc_tb)
        if hasattr(server_app.state, 'api'):
            await server_app.state.api.metastore.close()


@pytest.fixture(scope='function')
def setup_env():
    os.environ['MEMEX_CLI__SERVER_URL'] = 'http://test'
    os.environ['MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL'] = 'mock-model'
    yield
    os.environ.pop('MEMEX_CLI__SERVER_URL', None)
    os.environ.pop('MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL', None)


@pytest.mark.asyncio
async def test_cli_memory_add_url_with_vault_name(db_session: AsyncSession, setup_env):
    """Verify that --url ingestion correctly resolves vault name and propagates to all tables."""
    custom_vault_id = uuid4()
    vault_name = f'url-vault-{uuid4()}'
    vault = Vault(id=custom_vault_id, name=vault_name, description='URL Vault')
    db_session.add(vault)
    await db_session.commit()

    url = f'https://example.com/test-{uuid4()}'

    # Mock WebContentProcessor
    mock_extracted = MagicMock()
    mock_extracted.content = 'Extracted content from URL'
    mock_extracted.metadata = {
        'title': 'Test Page',
        'date': '2023-01-01',
        'author': 'Test Author',
        'hostname': 'example.com',
    }
    mock_extracted.source = url
    mock_extracted.document_date = None

    mock_usage = TokenUsage(total_tokens=10)

    with (
        patch('memex_cli.utils.httpx.AsyncClient', side_effect=MockAsyncClientContext),
        patch(
            'memex_core.processing.web.WebContentProcessor.fetch_and_extract',
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
        ) as mock_run_dspy_core,
        patch(
            'memex_core.services.ingestion.extract_document_date', new_callable=AsyncMock
        ) as mock_date,
    ):
        mock_fetch.return_value = mock_extracted
        mock_date.return_value = None

        # Mock core extraction — run_dspy_operation in core.py is used for:
        # 1. Fact extraction (returns pred.extracted_facts)
        # 2. Node summarization (returns pred.summary as SectionSummary)
        # 3. Block summarization (returns pred.block_summary as BlockSummary)
        mock_core_prediction = MagicMock()
        mock_core_prediction.extracted_facts = ExtractedOutput(extracted_facts=[])
        mock_core_prediction.summary = SectionSummary(what='Mock summary')
        mock_core_prediction.block_summary = BlockSummary(topic='Mock topic', key_points=[])
        mock_run_dspy_core.return_value = (mock_core_prediction, mock_usage)

        # Wrap IngestionService.ingest to spy on it
        original_ingest = IngestionService.ingest
        with patch.object(
            IngestionService, 'ingest', side_effect=original_ingest, autospec=True
        ) as mock_ingest:
            # Run CLI
            result = runner.invoke(
                app, ['note', 'add', '--url', url, '-v', vault_name], env=os.environ
            )

            assert result.exit_code == 0, f'CLI failed: {result.stdout}'

            # Verify the call to api.ingest HAD THE CORRECT vault_id
            assert mock_ingest.called
            assert str(mock_ingest.call_args.kwargs['vault_id']) == str(custom_vault_id)
