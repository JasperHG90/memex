"""Integration tests for S3AsyncFileStore using MinIO via testcontainers."""

import pytest
from testcontainers.minio import MinioContainer

from memex_common.config import S3FileStoreConfig, SecretStr
from memex_core.storage.filestore import S3AsyncFileStore


BUCKET_NAME = 'test-memex'
ACCESS_KEY = 'minioadmin'
SECRET_KEY = 'minioadmin'

# Use a session-scoped event loop so the S3FileSystem's aiohttp session
# survives across tests (s3fs caches filesystem instances by constructor args).
pytestmark = pytest.mark.asyncio(loop_scope='session')


@pytest.fixture(scope='session')
def minio_container():
    container = MinioContainer(
        image='minio/minio:latest',
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
    )
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope='session')
def minio_endpoint(minio_container: MinioContainer) -> str:
    host = minio_container.get_container_host_ip()
    port = minio_container.get_exposed_port(9000)
    return f'http://{host}:{port}'


@pytest.fixture(scope='session', autouse=True)
def create_bucket(minio_endpoint: str):
    from minio import Minio
    from urllib.parse import urlparse

    parsed = urlparse(minio_endpoint)
    client = Minio(
        f'{parsed.hostname}:{parsed.port}',
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        secure=False,
    )
    if not client.bucket_exists(BUCKET_NAME):
        client.make_bucket(BUCKET_NAME)


@pytest.fixture(scope='session')
def s3_store(minio_endpoint: str) -> S3AsyncFileStore:
    config = S3FileStoreConfig(
        bucket=BUCKET_NAME,
        root='test-data',
        endpoint_url=minio_endpoint,
        access_key_id=SecretStr(ACCESS_KEY),
        secret_access_key=SecretStr(SECRET_KEY),
    )
    return S3AsyncFileStore(config)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_save_load_exists(s3_store: S3AsyncFileStore) -> None:
    from uuid import uuid4

    key = f'notes/{uuid4()}.txt'
    data = b's3 integration test content'

    await s3_store.save(key, data)
    assert await s3_store.exists(key) is True

    loaded = await s3_store.load(key)
    assert loaded == data


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_delete(s3_store: S3AsyncFileStore) -> None:
    from uuid import uuid4

    key = f'notes/{uuid4()}.txt'
    await s3_store.save(key, b'to delete')
    assert await s3_store.exists(key) is True

    await s3_store.delete(key)
    assert await s3_store.exists(key) is False


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_glob(s3_store: S3AsyncFileStore) -> None:
    from uuid import uuid4

    prefix = f'glob-test-{uuid4()}'
    await s3_store.save(f'{prefix}/a.txt', b'a')
    await s3_store.save(f'{prefix}/b.txt', b'b')
    await s3_store.save(f'{prefix}/c.log', b'c')

    results = await s3_store.glob(f'{prefix}/*.txt')
    assert len(results) == 2
    assert any(r.endswith('a.txt') for r in results)
    assert any(r.endswith('b.txt') for r in results)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_staging_commit(s3_store: S3AsyncFileStore) -> None:
    from uuid import uuid4

    txn_id = str(uuid4())
    key = f'notes/{uuid4()}.txt'
    data = b'staged on s3'

    s3_store.begin_staging(txn_id)
    await s3_store.save(key, data, txn_id=txn_id)

    # Final file should NOT exist; staged file should
    assert await s3_store.exists(key) is False
    assert await s3_store.exists(f'{key}.stage_{txn_id}') is True

    await s3_store.commit_staging(txn_id)

    assert await s3_store.exists(key) is True
    assert await s3_store.exists(f'{key}.stage_{txn_id}') is False
    assert await s3_store.load(key) == data


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_staging_rollback(s3_store: S3AsyncFileStore) -> None:
    from uuid import uuid4

    txn_id = str(uuid4())
    key = f'notes/{uuid4()}.txt'

    s3_store.begin_staging(txn_id)
    await s3_store.save(key, b'will be rolled back', txn_id=txn_id)

    await s3_store.rollback_staging(txn_id)

    assert await s3_store.exists(key) is False
    assert await s3_store.exists(f'{key}.stage_{txn_id}') is False


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_check_connection(s3_store: S3AsyncFileStore) -> None:
    result = await s3_store.check_connection()
    assert result is True
