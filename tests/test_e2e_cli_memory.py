import os
import pytest
import nest_asyncio
import numpy as np
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from httpx import AsyncClient, ASGITransport
from uuid import uuid4
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from memex_core.memory.sql_models import MemoryUnit, TokenUsage
from memex_core.memory.extraction.models import ExtractedOutput, RawFact
from memex_common.types import FactTypes, FactKindTypes
from memex_cli import app
from memex_core.server import app as server_app
from memex_core.config import GLOBAL_VAULT_ID

nest_asyncio.apply()
runner = CliRunner()


class MockEmbedder:
    def encode(self, texts):
        return np.random.rand(len(texts), 384)


class MockAsyncClientContext:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs.get('base_url', 'http://test')

    async def __aenter__(self):
        from memex_core.config import parse_memex_config
        from memex_core.storage.metastore import get_metastore
        from memex_core.storage.filestore import get_filestore
        from memex_core.api import MemexAPI
        from memex_core.memory.models import get_embedding_model, get_reranking_model, get_ner_model

        config = parse_memex_config()
        # Ensure extraction config uses mock model
        config.server.memory.extraction.model.model = 'mock-model'

        metastore = get_metastore(config.server.meta_store)
        filestore = get_filestore(config.server.file_store)
        await metastore.connect()

        config.server.memory.reflection.background_reflection_enabled = False

        # We need to await these here to ensure they are ready and (possibly) patched
        embedding_model = await get_embedding_model()
        reranking_model = await get_reranking_model()
        ner_model = await get_ner_model()

        # Patch dspy.LM globally during API init to bypass validation
        with patch('dspy.LM', return_value=MagicMock()):
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

        self.client = AsyncClient(transport=ASGITransport(app=server_app), base_url=self.base_url)
        await self.client.__aenter__()
        return self.client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)
        if hasattr(server_app.state, 'api'):
            await server_app.state.api.metastore.close()


@pytest.fixture(scope='function')
async def setup_cli_e2e(db_session: AsyncSession):
    os.environ['MEMEX_CLI__SERVER_URL'] = 'http://test'
    os.environ['MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL'] = 'mock-model'
    os.environ['MEMEX_SERVER__ACTIVE_VAULT'] = str(GLOBAL_VAULT_ID)
    yield
    os.environ.pop('MEMEX_CLI__SERVER_URL', None)
    os.environ.pop('MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL', None)
    os.environ.pop('MEMEX_SERVER__ACTIVE_VAULT', None)


@pytest.mark.asyncio
async def test_cli_memory_add(db_session: AsyncSession, setup_cli_e2e):
    content = f'Hello world memory {uuid4()}'

    mock_raw_fact = RawFact(
        what='User added a memory',
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

    # Patch run_dspy_operation where it is used in extraction core
    with (
        patch('memex_cli.utils.httpx.AsyncClient', side_effect=MockAsyncClientContext),
        patch(
            'memex_core.memory.models.embedding.get_embedding_model', return_value=MockEmbedder()
        ),
        patch(
            'memex_core.memory.extraction.core.run_dspy_operation', new_callable=AsyncMock
        ) as mock_run_dspy,
        patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(MagicMock(extracted_date=None), TokenUsage(total_tokens=0)),
        ),
    ):
        mock_run_dspy.return_value = (mock_prediction, mock_usage)
        result = runner.invoke(app, ['memory', 'add', content], env=os.environ)

        assert result.exit_code == 0, f'Error: {result.stdout}'
        assert 'Memory added successfully' in result.stdout


@pytest.mark.asyncio
async def test_cli_memory_search(db_session: AsyncSession, setup_cli_e2e):
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        text='Python is a programming language.',
        fact_type='world',
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.1] * 384,
        event_date=datetime.now(timezone.utc),
        mentioned_at=datetime.now(timezone.utc),
        metadata_={'note_name': 'test_note'},
    )
    db_session.add(unit)
    await db_session.commit()

    mock_prediction = MagicMock()
    mock_prediction.summary = 'A summary of Python.'
    mock_usage = TokenUsage(total_tokens=10)

    # Patch run_dspy_operation where it is used in API
    with (
        patch('memex_cli.utils.httpx.AsyncClient', side_effect=MockAsyncClientContext),
        patch(
            'memex_core.memory.retrieval.engine.get_embedding_model', return_value=MockEmbedder()
        ),
        patch(
            'memex_core.services.search.run_dspy_operation', new_callable=AsyncMock
        ) as mock_run_dspy,
    ):
        mock_run_dspy.return_value = (mock_prediction, mock_usage)
        result = runner.invoke(
            app,
            ['memory', 'search', 'Python', '--token-budget', '1000'],
            env=os.environ,
        )

        assert result.exit_code == 0, f'Error: {result.stdout}'
        assert 'Searching: Python' in result.stdout
        # The result includes the search hit which comes from the REAL retrieval engine
        assert 'Python is a programming language.' in result.stdout
