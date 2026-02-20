import pytest
import pytest_asyncio
from typing import AsyncGenerator, Generator
from sqlmodel import SQLModel, text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    async_sessionmaker,
)
from sqlalchemy import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession
from testcontainers.postgres import PostgresContainer
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_common.config import MemexConfig

postgres = PostgresContainer('pgvector/pgvector:pg18-trixie')


from unittest.mock import patch, AsyncMock, MagicMock


@pytest.fixture
def mock_embedding_model():
    mock = MagicMock()

    def encode_side_effect(texts):
        import numpy as np

        return np.array([[0.1] * 384] * len(texts))

    mock.encode.side_effect = encode_side_effect
    return mock


@pytest.fixture
def mock_reranking_model():
    mock = AsyncMock()
    mock.rerank.return_value = []
    return mock


@pytest.fixture
def mock_ner_model():
    mock = MagicMock()
    mock.extract_entities.return_value = []
    return mock


@pytest.fixture
def patch_api_engines():
    """Context-managed patch for all MemexAPI engine components in integration tests."""
    with (
        patch('memex_core.api.dspy'),
        patch('memex_core.api.EntityResolver') as m1,
        patch('memex_core.api.ExtractionEngine') as m2,
        patch('memex_core.api.RetrievalEngine') as m3,
        patch('memex_core.api.MemoryEngine') as m4,
        patch('memex_core.api.ReflectionQueueService') as m5,
        patch('memex_core.api.FileContentProcessor') as m6,
    ):
        # Ensure the instances (return_value of the class mock) are AsyncMocks
        m1.return_value = AsyncMock()
        m2.return_value = AsyncMock()
        m3.return_value = AsyncMock()
        m4.return_value = AsyncMock()
        m5.return_value = AsyncMock()
        m6.return_value = AsyncMock()
        yield


@pytest.fixture
def fake_retain_factory():
    """Factory to create a fake retain function that persists minimal data to DB."""
    from memex_core.memory.sql_models import Document, MemoryUnit
    from memex_common.types import FactTypes

    async def _fake_retain(session, contents, document_id, **kwargs):
        content_item = contents[0]
        vault_id = content_item.vault_id
        payload = content_item.payload or {}

        doc = Document(
            id=document_id,
            content_hash=payload.get('content_fingerprint', 'hash'),
            vault_id=vault_id,
            original_text=content_item.content,
            filestore_path=payload.get('filestore_path'),
            assets=payload.get('assets', []),
        )
        session.add(doc)
        unit = MemoryUnit(
            document_id=document_id,
            text='Extracted fact',
            fact_type=FactTypes.WORLD,
            vault_id=vault_id,
            embedding=[0.1] * 384,
            event_date=content_item.event_date,
        )
        session.add(unit)
        return {'unit_ids': [unit.id], 'status': 'success'}

    return _fake_retain


@pytest.fixture(scope='session')
def postgres_container(request: pytest.FixtureRequest) -> Generator[PostgresContainer, None, None]:
    postgres.start()

    def remove_container():
        postgres.stop()

    request.addfinalizer(remove_container)

    yield postgres


@pytest.fixture(scope='session')
def postgres_uri(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url().replace('psycopg2', 'asyncpg')


@pytest_asyncio.fixture(scope='session')
async def engine(postgres_uri: str) -> AsyncGenerator[AsyncEngine, None]:
    # Ensure all SQLModel tables are registered in metadata before create_all
    import memex_core.memory.sql_models  # noqa: F401

    engine = create_async_engine(
        postgres_uri,
        future=True,
        echo=False,
        pool_pre_ping=True,
        poolclass=NullPool,
    )

    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await conn.run_sync(SQLModel.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
def api(
    metastore,
    filestore,
    memex_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    patch_api_engines,
):
    """Provides a MemexAPI instance with all internal engines patched."""
    from memex_core.api import MemexAPI

    return MemexAPI(
        embedding_model=mock_embedding_model,
        reranking_model=mock_reranking_model,
        ner_model=mock_ner_model,
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
    )


@pytest.fixture(scope='session')
def session_manager(engine: AsyncEngine):
    # This factory generates AsyncSessions
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest_asyncio.fixture(scope='function')
async def session(session_manager) -> AsyncGenerator[AsyncSession, None]:
    async with session_manager() as session:
        yield session


@pytest_asyncio.fixture(scope='function', autouse=True)
async def clean_tables(session: AsyncSession):
    for table in SQLModel.metadata.sorted_tables:
        await session.exec(text(f'TRUNCATE TABLE {table.name} CASCADE'))
    await session.commit()


@pytest_asyncio.fixture(scope='function', autouse=True)
async def init_global_vault(session: AsyncSession, clean_tables: None) -> None:
    """Ensure Global Vault exists after cleanup."""
    from memex_core.memory.sql_models import Vault
    from memex_common.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME

    vault = await session.get(Vault, GLOBAL_VAULT_ID)
    if not vault:
        vault = Vault(
            id=GLOBAL_VAULT_ID,
            name=GLOBAL_VAULT_NAME,
            description='Test Global Vault',
        )
        session.add(vault)
        await session.commit()


@pytest_asyncio.fixture(scope='function')
async def metastore(
    engine: AsyncEngine, postgres_uri: str
) -> AsyncGenerator[AsyncPostgresMetaStoreEngine, None]:
    """Fixture for the AsyncPostgresMetaStoreEngine."""
    from memex_common.config import PostgresMetaStoreConfig, PostgresInstanceConfig, SecretStr
    from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
    from urllib.parse import urlparse

    parsed = urlparse(postgres_uri)
    config = PostgresMetaStoreConfig(
        instance=PostgresInstanceConfig(
            host=parsed.hostname or 'localhost',
            port=parsed.port or 5432,
            database=parsed.path.lstrip('/'),
            user=parsed.username or 'postgres',
            password=SecretStr(parsed.password or 'postgres'),
        )
    )

    engine = AsyncPostgresMetaStoreEngine(config)
    await engine.connect()
    yield engine
    await engine.close()


@pytest.fixture(scope='session')
def filestore(tmp_path_factory: pytest.TempPathFactory) -> BaseAsyncFileStore:
    """Fixture for a real LocalFileStore using a temp directory."""
    from memex_common.config import LocalFileStoreConfig
    from memex_core.storage.filestore import LocalAsyncFileStore

    # Create a temporary directory for the session
    root_dir = tmp_path_factory.mktemp('memex_filestore')
    config = LocalFileStoreConfig(root=str(root_dir))
    return LocalAsyncFileStore(config)


@pytest.fixture(scope='function')
def memex_config(postgres_uri: str) -> 'MemexConfig':
    """
    Returns a full MemexConfig initialized with the test container DB.
    Optimized for integration tests:
    - Lower confidence thresholds.
    - Low max_concurrency for predictable execution.
    """
    from memex_common.config import (
        MemexConfig,
        ExtractionConfig,
        SimpleTextSplitting,
        ModelConfig,
        ConfidenceConfig,
        ReflectionConfig,
        PostgresMetaStoreConfig,
        PostgresInstanceConfig,
        SecretStr,
        GLOBAL_VAULT_NAME,
        ServerConfig,
        MemoryConfig,
        RetrievalConfig,
        OpinionFormationConfig,
    )
    from urllib.parse import urlparse

    parsed = urlparse(postgres_uri)
    return MemexConfig(
        server=ServerConfig(
            active_vault=GLOBAL_VAULT_NAME,
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 5432,
                    database=parsed.path.lstrip('/'),
                    user=parsed.username or 'postgres',
                    password=SecretStr(parsed.password or 'postgres'),
                )
            ),
            memory=MemoryConfig(
                extraction=ExtractionConfig(
                    model=ModelConfig(model='gemini/gemini-3-flash-preview'),
                    text_splitting=SimpleTextSplitting(
                        chunk_size_tokens=1000, chunk_overlap_tokens=100
                    ),
                    max_concurrency=2,
                ),
                reflection=ReflectionConfig(
                    similarity_threshold=0.3,  # Very permissive for hunting in small test datasets
                    search_limit=20,
                    model=ModelConfig(model='gemini/gemini-3-flash-preview'),
                ),
                retrieval=RetrievalConfig(token_budget=2000),
                opinion_formation=OpinionFormationConfig(
                    confidence=ConfidenceConfig(
                        similarity_threshold=0.5,  # Permissive for tests
                        damping_factor=0.1,
                        max_inherited_mass=10.0,
                    )
                ),
            ),
        )
    )
