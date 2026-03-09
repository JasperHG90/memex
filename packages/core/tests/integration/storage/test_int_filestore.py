import pytest
from pathlib import Path
from memex_core.storage.filestore import LocalAsyncFileStore
from memex_core.config import LocalFileStoreConfig


@pytest.fixture
def store(tmp_path: Path) -> LocalAsyncFileStore:
    config = LocalFileStoreConfig(root=str(tmp_path))
    return LocalAsyncFileStore(config)


@pytest.mark.asyncio
async def test_save_load_exists(store: LocalAsyncFileStore) -> None:
    key = 'test.txt'
    data = b'integration test content'

    await store.save(key, data)
    assert await store.exists(key) is True

    loaded = await store.load(key)
    assert loaded == data


@pytest.mark.asyncio
async def test_glob(store: LocalAsyncFileStore) -> None:
    await store.save('data/1.txt', b'1')
    await store.save('data/2.txt', b'2')
    await store.save('other.log', b'other')

    results = await store.glob('data/*.txt')
    assert len(results) == 2
    assert any(r.endswith('1.txt') for r in results)
    assert any(r.endswith('2.txt') for r in results)


@pytest.mark.asyncio
async def test_staging_commit(store: LocalAsyncFileStore, tmp_path: Path) -> None:
    txn_id = 'test-txn'
    key = 'permanent.txt'
    data = b'staged'

    # 1. Start staging
    store.begin_staging(txn_id)
    await store.save(key, data, txn_id=txn_id)

    # 2. Check that it is NOT in final location but IS in stage location
    assert (tmp_path / key).exists() is False
    assert (tmp_path / f'{key}.stage_{txn_id}').exists() is True

    # 3. Commit
    await store.commit_staging(txn_id)

    # 4. Check final location
    assert (tmp_path / key).exists() is True
    assert (tmp_path / f'{key}.stage_{txn_id}').exists() is False
    assert await store.load(key) == data


@pytest.mark.asyncio
async def test_staging_rollback(store: LocalAsyncFileStore, tmp_path: Path) -> None:
    txn_id = 'rollback-txn'
    key = 'never_born.txt'

    store.begin_staging(txn_id)
    await store.save(key, b'gone', txn_id=txn_id)

    assert (tmp_path / f'{key}.stage_{txn_id}').exists() is True

    await store.rollback_staging(txn_id)

    assert (tmp_path / key).exists() is False
    assert (tmp_path / f'{key}.stage_{txn_id}').exists() is False


@pytest.mark.asyncio
async def test_concurrent_staging_commit_integration(
    store: LocalAsyncFileStore, tmp_path: Path
) -> None:
    """Two concurrent transactions on the same filestore both commit their files."""
    store.begin_staging('txn_a')
    await store.save('a.txt', b'aaa', txn_id='txn_a')

    store.begin_staging('txn_b')
    await store.save('b.txt', b'bbb', txn_id='txn_b')

    await store.commit_staging('txn_a')
    assert (tmp_path / 'a.txt').exists() is True

    await store.commit_staging('txn_b')
    assert (tmp_path / 'b.txt').exists() is True

    assert await store.load('a.txt') == b'aaa'
    assert await store.load('b.txt') == b'bbb'
