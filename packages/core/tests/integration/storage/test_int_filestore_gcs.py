"""Integration tests for GCSAsyncFileStore using the fake-gcs-server emulator."""

import pytest
import httpx
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from memex_common.config import GCSFileStoreConfig
from memex_core.storage.filestore import GCSAsyncFileStore


BUCKET_NAME = 'test-memex'
EMULATOR_PORT = 4443

# Use a session-scoped event loop so the GCSFileSystem's aiohttp session
# survives across tests (gcsfs caches filesystem instances by constructor args).
pytestmark = pytest.mark.asyncio(loop_scope='session')


@pytest.fixture(scope='session')
def gcs_container():
    container = (
        DockerContainer('fsouza/fake-gcs-server:latest')
        .with_exposed_ports(EMULATOR_PORT)
        .with_command('-scheme http -port 4443')
    )
    container.start()
    wait_for_logs(container, 'server started at', timeout=30)
    yield container
    container.stop()


@pytest.fixture(scope='session')
def gcs_endpoint(gcs_container: DockerContainer) -> str:
    host = gcs_container.get_container_host_ip()
    port = gcs_container.get_exposed_port(EMULATOR_PORT)
    return f'http://{host}:{port}'


@pytest.fixture(scope='session', autouse=True)
def create_bucket(gcs_endpoint: str):
    """Create the test bucket via the emulator's HTTP API."""
    resp = httpx.post(
        f'{gcs_endpoint}/storage/v1/b',
        json={'name': BUCKET_NAME},
    )
    # 200 = created, 409 = already exists
    assert resp.status_code in (200, 409), f'Bucket creation failed: {resp.text}'


@pytest.fixture(scope='session')
def gcs_store(gcs_endpoint: str) -> GCSAsyncFileStore:
    config = GCSFileStoreConfig(
        bucket=BUCKET_NAME,
        root='test-data',
        token='anon',
        endpoint_url=gcs_endpoint,
    )
    return GCSAsyncFileStore(config)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_save_load_exists(gcs_store: GCSAsyncFileStore) -> None:
    from uuid import uuid4

    key = f'notes/{uuid4()}.txt'
    data = b'gcs integration test content'

    await gcs_store.save(key, data)
    assert await gcs_store.exists(key) is True

    loaded = await gcs_store.load(key)
    assert loaded == data


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_delete(gcs_store: GCSAsyncFileStore) -> None:
    from uuid import uuid4

    key = f'notes/{uuid4()}.txt'
    await gcs_store.save(key, b'to delete')
    assert await gcs_store.exists(key) is True

    await gcs_store.delete(key)
    assert await gcs_store.exists(key) is False


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_glob(gcs_store: GCSAsyncFileStore) -> None:
    from uuid import uuid4

    prefix = f'glob-test-{uuid4()}'
    await gcs_store.save(f'{prefix}/a.txt', b'a')
    await gcs_store.save(f'{prefix}/b.txt', b'b')
    await gcs_store.save(f'{prefix}/c.log', b'c')

    results = await gcs_store.glob(f'{prefix}/*.txt')
    assert len(results) == 2
    assert any(r.endswith('a.txt') for r in results)
    assert any(r.endswith('b.txt') for r in results)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_staging_commit(gcs_store: GCSAsyncFileStore) -> None:
    from uuid import uuid4

    txn_id = str(uuid4())
    key = f'notes/{uuid4()}.txt'
    data = b'staged on gcs'

    gcs_store.begin_staging(txn_id)
    await gcs_store.save(key, data, txn_id=txn_id)

    assert await gcs_store.exists(key) is False
    assert await gcs_store.exists(f'{key}.stage_{txn_id}') is True

    await gcs_store.commit_staging(txn_id)

    assert await gcs_store.exists(key) is True
    assert await gcs_store.exists(f'{key}.stage_{txn_id}') is False
    assert await gcs_store.load(key) == data


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_staging_rollback(gcs_store: GCSAsyncFileStore) -> None:
    from uuid import uuid4

    txn_id = str(uuid4())
    key = f'notes/{uuid4()}.txt'

    gcs_store.begin_staging(txn_id)
    await gcs_store.save(key, b'will be rolled back', txn_id=txn_id)

    await gcs_store.rollback_staging(txn_id)

    assert await gcs_store.exists(key) is False
    assert await gcs_store.exists(f'{key}.stage_{txn_id}') is False


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope='session')
async def test_check_connection(gcs_store: GCSAsyncFileStore) -> None:
    result = await gcs_store.check_connection()
    assert result is True
