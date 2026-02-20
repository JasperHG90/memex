import pytest
from pathlib import Path
from sqlalchemy import text
from pydantic import SecretStr
import re

from memex_core.config import PostgresMetaStoreConfig, PostgresInstanceConfig, LocalFileStoreConfig
from memex_core.storage.filestore import LocalAsyncFileStore
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
from memex_core.storage.transaction import AsyncTransaction


@pytest.fixture
def store_config(tmp_path: Path) -> LocalFileStoreConfig:
    return LocalFileStoreConfig(root=str(tmp_path))


@pytest.fixture
def metastore_config(postgres_uri: str) -> PostgresMetaStoreConfig:
    match = re.search(r'postgresql\+asyncpg://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)', postgres_uri)
    assert match is not None
    user, password, host, port, database = match.groups()
    instance = PostgresInstanceConfig(
        host=host, port=int(port), database=database, user=user, password=SecretStr(password)
    )
    return PostgresMetaStoreConfig(instance=instance)


@pytest.mark.asyncio
async def test_transaction_commit(
    metastore_config: PostgresMetaStoreConfig, store_config: LocalFileStoreConfig, tmp_path: Path
) -> None:
    # 1. Setup engine and store
    engine = AsyncPostgresMetaStoreEngine(metastore_config)
    store = LocalAsyncFileStore(store_config)

    async with engine.open():
        # Prepare DB: create a real table (not TEMP because sessions might be different if pooled,
        # though here they should be fine)
        async with engine.session() as setup_session:
            await setup_session.exec(
                text('CREATE TABLE IF NOT EXISTS trans_test (id INT PRIMARY KEY, name TEXT)')
            )
            await setup_session.exec(text('TRUNCATE trans_test'))
            await setup_session.commit()

        # 2. Run Transaction
        async with AsyncTransaction(engine, store, 'txn-commit') as txn:
            # DB Action
            await txn.db_session.exec(
                text("INSERT INTO trans_test (id, name) VALUES (1, 'success')")
            )

            # FS Action
            await store.save('txn_file.txt', b'txn content')

            # Verify FS is staged
            assert (tmp_path / 'txn_file.txt').exists() is False
            assert (tmp_path / 'txn_file.txt.stage_txn-commit').exists() is True

        # 3. Verify Commit
        # Check DB
        async with engine.session() as verify_session:
            result = await verify_session.exec(text('SELECT name FROM trans_test WHERE id = 1'))
            assert result.scalar() == 'success'

        # Check FS
        assert (tmp_path / 'txn_file.txt').exists() is True
        assert (tmp_path / 'txn_file.txt.stage_txn-commit').exists() is False
        assert (await store.load('txn_file.txt')) == b'txn content'


@pytest.mark.asyncio
async def test_transaction_rollback(
    metastore_config: PostgresMetaStoreConfig, store_config: LocalFileStoreConfig, tmp_path: Path
) -> None:
    engine = AsyncPostgresMetaStoreEngine(metastore_config)
    store = LocalAsyncFileStore(store_config)

    async with engine.open():
        async with engine.session() as setup_session:
            await setup_session.exec(
                text('CREATE TABLE IF NOT EXISTS trans_test_rb (id INT PRIMARY KEY, name TEXT)')
            )
            await setup_session.exec(text('TRUNCATE trans_test_rb'))
            await setup_session.commit()

        # 2. Run Transaction that fails
        try:
            async with AsyncTransaction(engine, store, 'txn-rollback') as txn:
                await txn.db_session.exec(
                    text("INSERT INTO trans_test_rb (id, name) VALUES (1, 'fail')")
                )
                await store.save('fail_file.txt', b'should be gone')
                raise RuntimeError('Force Rollback')
        except RuntimeError:
            pass

        # 3. Verify Rollback
        # Check DB
        async with engine.session() as verify_session:
            result = await verify_session.exec(text('SELECT count(*) FROM trans_test_rb'))
            assert result.scalar() == 0

        # Check FS
        assert (tmp_path / 'fail_file.txt').exists() is False
        assert (tmp_path / 'fail_file.txt.stage_txn-rollback').exists() is False
