import pytest
from sqlalchemy import text
from pydantic import SecretStr
from memex_core.config import PostgresMetaStoreConfig, PostgresInstanceConfig
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine


@pytest.fixture
def metastore_config(postgres_uri: str) -> PostgresMetaStoreConfig:
    # postgres_uri looks like postgresql+asyncpg://user:pass@host:port/db
    # We need to parse it back into PostgresInstanceConfig or just hack it for testing.
    # Since PostgresInstanceConfig has a property 'connection_string', we can mock it
    # or create a dummy config and override connection_string behavior if possible.
    # Looking at metastore.py: database_url = self._config.instance.connection_string

    # Actually, let's just create a dummy instance and patch its connection_string property
    # or just use a config that happens to have the right values.

    # Alternatively, parse the URI:
    # postgresql+asyncpg://user:password@localhost:56781/test
    import re

    match = re.search(r'postgresql\+asyncpg://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)', postgres_uri)
    assert match is not None
    user, password, host, port, database = match.groups()

    instance = PostgresInstanceConfig(
        host=host, port=int(port), database=database, user=user, password=SecretStr(password)
    )
    return PostgresMetaStoreConfig(instance=instance)


@pytest.mark.asyncio
async def test_connect_and_query(metastore_config: PostgresMetaStoreConfig) -> None:
    engine = AsyncPostgresMetaStoreEngine(metastore_config)

    async with engine.open() as connected_engine:
        # Check pgvector extension
        async with connected_engine.session() as session:
            result = await session.exec(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == 'vector'

            # Simple query
            res = await session.exec(text('SELECT 1'))
            assert res.scalar() == 1


@pytest.mark.asyncio
async def test_session_lifecycle_persistence(metastore_config: PostgresMetaStoreConfig) -> None:
    engine = AsyncPostgresMetaStoreEngine(metastore_config)

    async with engine.open() as connected_engine:
        async with connected_engine.session() as session:
            # Create a temporary table for testing persistence
            await session.exec(
                text('CREATE TEMP TABLE test_table (id SERIAL PRIMARY KEY, val TEXT)')
            )
            await session.exec(text("INSERT INTO test_table (val) VALUES ('hello')"))
            await session.commit()

            result = await session.exec(text('SELECT val FROM test_table'))
            assert result.scalar() == 'hello'
