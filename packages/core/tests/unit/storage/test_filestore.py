import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from memex_core.storage.filestore import (
    LocalAsyncFileStore,
    S3AsyncFileStore,
    GCSAsyncFileStore,
    get_filestore,
)
from memex_core.config import LocalFileStoreConfig
from memex_common.config import S3FileStoreConfig, GCSFileStoreConfig


@pytest.fixture
def store(tmp_path: Path) -> LocalAsyncFileStore:
    config = LocalFileStoreConfig(root=str(tmp_path))
    return LocalAsyncFileStore(config)


@pytest.fixture
def fresh_store(tmp_path: Path) -> LocalAsyncFileStore:
    """Store without LRU cache interference for path traversal tests."""
    config = LocalFileStoreConfig(root=str(tmp_path))
    return LocalAsyncFileStore(config)


@pytest.mark.parametrize(
    ('key', 'expected_suffix'),
    [
        ('test.txt', 'test.txt'),
        ('/test.txt', 'test.txt'),
        ('dir/test.txt', 'dir/test.txt'),
    ],
)
def test_join_path(store: LocalAsyncFileStore, key: str, expected_suffix: str) -> None:
    path = store.join_path(key)
    assert path.endswith(expected_suffix)
    assert path == store.join_path(expected_suffix)


def test_join_path_no_root() -> None:
    config = LocalFileStoreConfig(root='')
    store = LocalAsyncFileStore(config)
    with pytest.raises(ValueError, match='Root path is not set'):
        store.join_path('test.txt')


@pytest.mark.asyncio
async def test_save_load_exists(store: LocalAsyncFileStore) -> None:
    key = 'hello.txt'
    data = b'hello world'

    await store.save(key, data)
    assert await store.exists(key) is True

    loaded_data = await store.load(key)
    assert loaded_data == data


@pytest.mark.asyncio
async def test_delete(store: LocalAsyncFileStore) -> None:
    key = 'to_delete.txt'
    await store.save(key, b'content')
    assert await store.exists(key) is True

    await store.delete(key)
    assert await store.exists(key) is False


@pytest.mark.asyncio
async def test_is_dir(store: LocalAsyncFileStore, tmp_path: Path) -> None:
    # Create a directory
    (tmp_path / 'subdir').mkdir()
    assert await store.is_dir('subdir') is True

    # Create a file
    await store.save('file.txt', b'content')
    assert await store.is_dir('file.txt') is False


@pytest.mark.asyncio
async def test_glob(store: LocalAsyncFileStore) -> None:
    await store.save('a.txt', b'a')
    await store.save('b.txt', b'b')
    await store.save('c.log', b'c')

    results = await store.glob('*.txt')
    # glob returns full paths based on join_path
    assert len(results) == 2
    assert any(r.endswith('a.txt') for r in results)
    assert any(r.endswith('b.txt') for r in results)


@pytest.mark.asyncio
async def test_staging_commit(store: LocalAsyncFileStore) -> None:
    txn_id = 'txn123'
    key = 'permanent.txt'
    data = b'staged content'

    store.begin_staging(txn_id)
    await store.save(key, data)

    # Final file should not exist yet
    assert await store.exists(key) is False
    # Staged file should exist
    assert await store.exists(f'{key}.stage_{txn_id}') is True

    await store.commit_staging()

    # Final file should exist now
    assert await store.exists(key) is True
    # Staged file should be gone
    assert await store.exists(f'{key}.stage_{txn_id}') is False
    assert await store.load(key) == data


@pytest.mark.asyncio
async def test_staging_rollback(store: LocalAsyncFileStore) -> None:
    txn_id = 'txn456'
    key = 'rollback.txt'

    store.begin_staging(txn_id)
    await store.save(key, b'will be rolled back')

    await store.rollback_staging()

    assert await store.exists(key) is False
    assert await store.exists(f'{key}.stage_{txn_id}') is False


@pytest.mark.asyncio
async def test_staging_context_manager(store: LocalAsyncFileStore) -> None:
    key = 'context.txt'
    data = b'context content'

    async with store.staging('txn789'):
        await store.save(key, data)
        assert await store.exists(key) is False

    assert await store.exists(key) is True
    assert await store.load(key) == data


@pytest.mark.asyncio
async def test_staging_context_manager_error(store: LocalAsyncFileStore) -> None:
    key = 'error.txt'

    try:
        async with store.staging('txn_fail'):
            await store.save(key, b'fail')
            raise RuntimeError('something went wrong')
    except RuntimeError:
        pass

    assert await store.exists(key) is False
    assert await store.exists(f'{key}.stage_txn_fail') is False


@pytest.mark.asyncio
async def test_deferred_delete(store: LocalAsyncFileStore) -> None:
    key = 'delete_me.txt'
    await store.save(key, b'initial')

    store.begin_staging('txn_del')
    await store.delete(key)

    # Should still exist
    assert await store.exists(key) is True

    await store.commit_staging()

    # Now should be gone
    assert await store.exists(key) is False


class TestPathTraversalPrevention:
    """Tests for path traversal vulnerability fix in join_path()."""

    def test_normal_path(self, fresh_store: LocalAsyncFileStore, tmp_path: Path) -> None:
        result = fresh_store.join_path('notes/test.txt')
        assert result == str(tmp_path / 'notes' / 'test.txt')

    def test_nested_path(self, fresh_store: LocalAsyncFileStore, tmp_path: Path) -> None:
        result = fresh_store.join_path('a/b/c/file.md')
        assert result == str(tmp_path / 'a' / 'b' / 'c' / 'file.md')

    def test_root_path(self, fresh_store: LocalAsyncFileStore, tmp_path: Path) -> None:
        result = fresh_store.join_path('')
        assert result == str(tmp_path)

    def test_traversal_dotdot(self, fresh_store: LocalAsyncFileStore) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            fresh_store.join_path('../../etc/passwd')

    def test_traversal_single_dotdot(self, fresh_store: LocalAsyncFileStore) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            fresh_store.join_path('..')

    def test_traversal_nested_dotdot(self, fresh_store: LocalAsyncFileStore) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            fresh_store.join_path('foo/../../../etc/shadow')

    def test_leading_slash_stays_under_root(
        self, fresh_store: LocalAsyncFileStore, tmp_path: Path
    ) -> None:
        """Leading slashes are stripped, so /etc/passwd resolves under root."""
        result = fresh_store.join_path('/etc/passwd')
        assert result == str(tmp_path / 'etc' / 'passwd')

    def test_traversal_dotdot_at_end(self, fresh_store: LocalAsyncFileStore) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            fresh_store.join_path('subdir/../../..')

    def test_safe_dotdot_within_root(
        self, fresh_store: LocalAsyncFileStore, tmp_path: Path
    ) -> None:
        """A .. that still resolves under root is allowed."""
        result = fresh_store.join_path('a/b/../c.txt')
        assert result == str(tmp_path / 'a' / 'c.txt')


# ---------------------------------------------------------------------------
# S3 join_path tests
# ---------------------------------------------------------------------------


class TestS3JoinPath:
    """Tests for S3AsyncFileStore.join_path()."""

    def _make_store(self, bucket: str = 'my-bucket', root: str = 'data') -> S3AsyncFileStore:
        config = S3FileStoreConfig(bucket=bucket, root=root)
        with patch(
            'memex_core.storage.filestore.S3AsyncFileStore.initialize', return_value=MagicMock()
        ):
            return S3AsyncFileStore(config)

    def test_normal_key(self) -> None:
        store = self._make_store()
        assert store.join_path('notes/file.md') == 'my-bucket/data/notes/file.md'

    def test_empty_key(self) -> None:
        store = self._make_store()
        assert store.join_path('') == 'my-bucket/data'

    def test_leading_slash_stripped(self) -> None:
        store = self._make_store()
        assert store.join_path('/notes/file.md') == 'my-bucket/data/notes/file.md'

    def test_no_prefix(self) -> None:
        store = self._make_store(root='')
        assert store.join_path('file.txt') == 'my-bucket/file.txt'

    def test_no_prefix_empty_key(self) -> None:
        store = self._make_store(root='')
        assert store.join_path('') == 'my-bucket'

    def test_traversal_rejected(self) -> None:
        store = self._make_store()
        with pytest.raises(ValueError, match='Path traversal detected'):
            store.join_path('../../etc/passwd')


# ---------------------------------------------------------------------------
# GCS join_path tests
# ---------------------------------------------------------------------------


class TestGCSJoinPath:
    """Tests for GCSAsyncFileStore.join_path()."""

    def _make_store(self, bucket: str = 'my-bucket', root: str = 'prefix') -> GCSAsyncFileStore:
        config = GCSFileStoreConfig(bucket=bucket, root=root)
        with patch(
            'memex_core.storage.filestore.GCSAsyncFileStore.initialize', return_value=MagicMock()
        ):
            return GCSAsyncFileStore(config)

    def test_normal_key(self) -> None:
        store = self._make_store()
        assert store.join_path('notes/file.md') == 'my-bucket/prefix/notes/file.md'

    def test_empty_key(self) -> None:
        store = self._make_store()
        assert store.join_path('') == 'my-bucket/prefix'

    def test_leading_slash_stripped(self) -> None:
        store = self._make_store()
        assert store.join_path('/notes/file.md') == 'my-bucket/prefix/notes/file.md'

    def test_no_prefix(self) -> None:
        store = self._make_store(root='')
        assert store.join_path('file.txt') == 'my-bucket/file.txt'

    def test_traversal_rejected(self) -> None:
        store = self._make_store()
        with pytest.raises(ValueError, match='Path traversal detected'):
            store.join_path('../../etc/passwd')


# ---------------------------------------------------------------------------
# Import guard tests
# ---------------------------------------------------------------------------


class TestImportGuards:
    """Test that missing optional dependencies raise helpful errors."""

    def test_s3_import_guard(self) -> None:
        import sys

        config = S3FileStoreConfig(bucket='test')
        with patch.dict(sys.modules, {'s3fs': None}):
            with pytest.raises(ImportError, match='s3fs'):
                S3AsyncFileStore(config)

    def test_gcs_import_guard(self) -> None:
        import sys

        config = GCSFileStoreConfig(bucket='test')
        with patch.dict(sys.modules, {'gcsfs': None}):
            with pytest.raises(ImportError, match='gcsfs'):
                GCSAsyncFileStore(config)


# ---------------------------------------------------------------------------
# check_connection tests
# ---------------------------------------------------------------------------


class TestCheckConnection:
    """Tests for BaseAsyncFileStore.check_connection()."""

    @pytest.mark.asyncio
    async def test_check_connection_success(self, store: LocalAsyncFileStore) -> None:
        result = await store.check_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_connection_failure(self) -> None:
        config = LocalFileStoreConfig(root='/nonexistent/path/that/does/not/exist')
        s = LocalAsyncFileStore(config)
        result = await s.check_connection()
        assert result is False


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestGetFilestore:
    """Tests for the get_filestore factory function."""

    def test_local(self, tmp_path: Path) -> None:
        config = LocalFileStoreConfig(root=str(tmp_path))
        store = get_filestore(config)
        assert isinstance(store, LocalAsyncFileStore)

    def test_s3(self) -> None:
        config = S3FileStoreConfig(bucket='test')
        with patch(
            'memex_core.storage.filestore.S3AsyncFileStore.initialize', return_value=MagicMock()
        ):
            store = get_filestore(config)
            assert isinstance(store, S3AsyncFileStore)

    def test_gcs(self) -> None:
        config = GCSFileStoreConfig(bucket='test')
        with patch(
            'memex_core.storage.filestore.GCSAsyncFileStore.initialize', return_value=MagicMock()
        ):
            store = get_filestore(config)
            assert isinstance(store, GCSAsyncFileStore)

    def test_unsupported(self) -> None:
        config = MagicMock()
        config.model_dump.return_value = {'type': 'ftp'}
        with pytest.raises(ValueError, match='Unsupported file store type'):
            get_filestore(config)
