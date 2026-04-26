import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from memex_core.storage.filestore import (
    LocalAsyncFileStore,
    S3AsyncFileStore,
    GCSAsyncFileStore,
    get_filestore,
    is_root_key,
)


# Inputs that ``posixpath.normpath`` collapses to no real path component:
# empty / whitespace / slash, bare ``.``/``..`` (and URL-decoded
# ``%2E``/``%2E%2E`` variants), and mixed-segment shapes that cancel back
# to root (``foo/..``). All are rejected by the empty-path guards on every
# public entry point.
ROOT_EQUIVALENT_KEYS = [
    '',
    '/',
    '///',
    '   ',
    '.',
    '..',
    './',
    '../',
    '/.',
    '/..',
    '/./',
    '/../',
    './..',
    '../..',
    'foo/..',
    'a/b/../..',
    'foo/../bar/..',
    './foo/..',
    '/foo/..',
]
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
    await store.save(key, data, txn_id=txn_id)

    # Final file should not exist yet
    assert await store.exists(key) is False
    # Staged file should exist
    assert await store.exists(f'{key}.stage_{txn_id}') is True

    await store.commit_staging(txn_id)

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
    await store.save(key, b'will be rolled back', txn_id=txn_id)

    await store.rollback_staging(txn_id)

    assert await store.exists(key) is False
    assert await store.exists(f'{key}.stage_{txn_id}') is False


@pytest.mark.asyncio
async def test_staging_context_manager(store: LocalAsyncFileStore) -> None:
    key = 'context.txt'
    data = b'context content'
    txn_id = 'txn789'

    async with store.staging(txn_id):
        await store.save(key, data, txn_id=txn_id)
        assert await store.exists(key) is False

    assert await store.exists(key) is True
    assert await store.load(key) == data


@pytest.mark.asyncio
async def test_staging_context_manager_error(store: LocalAsyncFileStore) -> None:
    key = 'error.txt'
    txn_id = 'txn_fail'

    try:
        async with store.staging(txn_id):
            await store.save(key, b'fail', txn_id=txn_id)
            raise RuntimeError('something went wrong')
    except RuntimeError:
        pass

    assert await store.exists(key) is False
    assert await store.exists(f'{key}.stage_{txn_id}') is False


@pytest.mark.asyncio
async def test_deferred_delete(store: LocalAsyncFileStore) -> None:
    key = 'delete_me.txt'
    txn_id = 'txn_del'
    await store.save(key, b'initial')

    store.begin_staging(txn_id)
    await store.delete(key, txn_id=txn_id)

    # Should still exist
    assert await store.exists(key) is True

    await store.commit_staging(txn_id)

    # Now should be gone
    assert await store.exists(key) is False


@pytest.mark.asyncio
async def test_deferred_recursive_delete(store: LocalAsyncFileStore) -> None:
    """Staging a recursive delete must actually remove nested files on commit."""
    # Create a directory structure simulating a note with assets
    await store.save('notes/abc/note.md', b'# Hello')
    await store.save('notes/abc/assets/image.png', b'PNG_DATA')
    await store.save('notes/abc/assets/deep/chart.svg', b'SVG_DATA')
    assert await store.exists('notes/abc/note.md') is True
    assert await store.exists('notes/abc/assets/image.png') is True
    assert await store.exists('notes/abc/assets/deep/chart.svg') is True

    txn_id = 'txn_recursive'
    store.begin_staging(txn_id)
    await store.delete('notes/abc', txn_id=txn_id, recursive=True)

    # Files should still exist before commit
    assert await store.exists('notes/abc/note.md') is True

    await store.commit_staging(txn_id)

    # All files including nested ones must be gone
    assert await store.exists('notes/abc/note.md') is False
    assert await store.exists('notes/abc/assets/image.png') is False
    assert await store.exists('notes/abc/assets/deep/chart.svg') is False
    assert await store.is_dir('notes/abc') is False


@pytest.mark.asyncio
async def test_deferred_non_recursive_delete_preserves_flag(store: LocalAsyncFileStore) -> None:
    """Staging a non-recursive delete must not remove nested files."""
    await store.save('dir/file.txt', b'content')
    await store.save('dir/sub/nested.txt', b'nested')

    txn_id = 'txn_non_recursive'
    store.begin_staging(txn_id)
    # Delete a single file (non-recursive) via staging
    await store.delete('dir/file.txt', txn_id=txn_id, recursive=False)

    await store.commit_staging(txn_id)

    assert await store.exists('dir/file.txt') is False
    # The nested file should still exist
    assert await store.exists('dir/sub/nested.txt') is True


@pytest.mark.asyncio
async def test_staging_context_manager_recursive_delete(store: LocalAsyncFileStore) -> None:
    """Recursive delete through the staging context manager works end-to-end."""
    await store.save('project/docs/readme.md', b'readme')
    await store.save('project/docs/img/logo.png', b'logo')
    txn_id = 'txn_ctx_recursive'

    async with store.staging(txn_id):
        await store.delete('project/docs', txn_id=txn_id, recursive=True)

    assert await store.exists('project/docs/readme.md') is False
    assert await store.exists('project/docs/img/logo.png') is False
    assert await store.is_dir('project/docs') is False


@pytest.mark.asyncio
async def test_move_file_single(store: LocalAsyncFileStore) -> None:
    """move_file works for a single file."""
    await store.save('src.txt', b'data')
    await store.move_file('src.txt', 'dst.txt')
    assert await store.exists('dst.txt') is True
    assert await store.exists('src.txt') is False
    assert await store.load('dst.txt') == b'data'


@pytest.mark.asyncio
async def test_move_file_directory(store: LocalAsyncFileStore, tmp_path: Path) -> None:
    """move_file moves an entire directory tree."""
    await store.save('src/a.txt', b'aaa')
    await store.save('src/sub/b.txt', b'bbb')

    await store.move_file('src', 'dst')

    assert await store.exists('dst/a.txt') is True
    assert await store.exists('dst/sub/b.txt') is True
    assert await store.load('dst/a.txt') == b'aaa'
    assert await store.load('dst/sub/b.txt') == b'bbb'
    assert await store.is_dir('src') is False


# ---------------------------------------------------------------------------
# Concurrent staging tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_staging_isolation(store: LocalAsyncFileStore) -> None:
    """Two concurrent transactions on the same filestore must not interfere."""
    # Transaction A starts and saves a file
    store.begin_staging('txn_a')
    await store.save('file_a.txt', b'data_a', txn_id='txn_a')

    # Transaction B starts (previously this wiped A's staged_files)
    store.begin_staging('txn_b')
    await store.save('file_b.txt', b'data_b', txn_id='txn_b')

    # Commit A — must still know about file_a
    await store.commit_staging('txn_a')
    assert await store.exists('file_a.txt') is True
    assert await store.load('file_a.txt') == b'data_a'

    # Commit B — must still know about file_b
    await store.commit_staging('txn_b')
    assert await store.exists('file_b.txt') is True
    assert await store.load('file_b.txt') == b'data_b'


@pytest.mark.asyncio
async def test_concurrent_staging_rollback_isolation(store: LocalAsyncFileStore) -> None:
    """Rolling back one transaction must not affect another's staged files."""
    store.begin_staging('txn_a')
    await store.save('keep.txt', b'keep', txn_id='txn_a')

    store.begin_staging('txn_b')
    await store.save('discard.txt', b'discard', txn_id='txn_b')

    # Rollback B
    await store.rollback_staging('txn_b')

    # A's staged file must still be intact
    assert await store.exists('keep.txt.stage_txn_a') is True

    # Commit A
    await store.commit_staging('txn_a')
    assert await store.exists('keep.txt') is True
    assert await store.exists('discard.txt') is False


@pytest.mark.asyncio
async def test_concurrent_staging_with_deletes(store: LocalAsyncFileStore) -> None:
    """Deferred deletes in one transaction don't affect another."""
    await store.save('shared.txt', b'original')

    store.begin_staging('txn_a')
    await store.delete('shared.txt', txn_id='txn_a')

    store.begin_staging('txn_b')
    await store.save('new.txt', b'new', txn_id='txn_b')

    # Commit B first — shared.txt should still exist (A hasn't committed)
    await store.commit_staging('txn_b')
    assert await store.exists('shared.txt') is True
    assert await store.exists('new.txt') is True

    # Commit A — now shared.txt should be deleted
    await store.commit_staging('txn_a')
    assert await store.exists('shared.txt') is False


@pytest.mark.asyncio
async def test_begin_staging_duplicate_txn_id_raises(store: LocalAsyncFileStore) -> None:
    store.begin_staging('txn_x')
    with pytest.raises(ValueError, match='already active'):
        store.begin_staging('txn_x')


@pytest.mark.asyncio
async def test_commit_unknown_txn_id_raises(store: LocalAsyncFileStore) -> None:
    with pytest.raises(ValueError, match='No active staging'):
        await store.commit_staging('nonexistent')


@pytest.mark.asyncio
async def test_save_without_txn_id_writes_directly(store: LocalAsyncFileStore) -> None:
    await store.save('direct.txt', b'direct')
    assert await store.exists('direct.txt') is True


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


# ---------------------------------------------------------------------------
# Empty-key rejection
#
# An empty / whitespace-only / all-slash key would be silently rewritten to the
# storage root by ``validate_path_safe`` (root is allowed there because
# ``check_connection`` lists the root). Public read methods must reject this so
# callers don't end up issuing a "GET bucket/" against S3, which boto rejects
# with ParamValidationError on the empty Key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_load_rejects_root_key(store: LocalAsyncFileStore, bad_key: str) -> None:
    with pytest.raises(FileNotFoundError, match='Resource key is empty'):
        await store.load(bad_key)


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_exists_returns_false_for_root_key(store: LocalAsyncFileStore, bad_key: str) -> None:
    assert await store.exists(bad_key) is False


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_is_dir_returns_false_for_root_key(store: LocalAsyncFileStore, bad_key: str) -> None:
    assert await store.is_dir(bad_key) is False


@pytest.mark.asyncio
async def test_check_connection_still_works_after_guard(store: LocalAsyncFileStore) -> None:
    # check_connection bypasses load/exists/is_dir and calls _fs._ls(join_path(''))
    # directly, so it must still succeed.
    assert await store.check_connection() is True


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_save_rejects_root_key(store: LocalAsyncFileStore, bad_key: str) -> None:
    # save with a root key would write at the bucket prefix on S3 / fail with
    # IsADirectoryError locally — refuse explicitly so the error is loud.
    with pytest.raises(ValueError, match='Refusing to save'):
        await store.save(bad_key, b'data')


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_delete_rejects_root_key(store: LocalAsyncFileStore, bad_key: str) -> None:
    # delete(root_key, recursive=True) on S3 would target the bucket prefix —
    # potentially destroying every object under it.
    with pytest.raises(ValueError, match='Refusing to delete'):
        await store.delete(bad_key, recursive=True)


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_move_file_rejects_root_src(store: LocalAsyncFileStore, bad_key: str) -> None:
    with pytest.raises(ValueError, match='Refusing to move'):
        await store.move_file(bad_key, 'dst.txt')


@pytest.mark.parametrize('bad_key', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_move_file_rejects_root_dest(store: LocalAsyncFileStore, bad_key: str) -> None:
    await store.save('src.txt', b'data')
    with pytest.raises(ValueError, match='Refusing to move'):
        await store.move_file('src.txt', bad_key)
    # Source must still exist — the guard runs before any side effects.
    assert await store.exists('src.txt') is True


@pytest.mark.parametrize('bad_pattern', ROOT_EQUIVALENT_KEYS)
@pytest.mark.asyncio
async def test_glob_root_pattern_returns_empty(
    store: LocalAsyncFileStore, bad_pattern: str
) -> None:
    # glob('') would otherwise list everything under the storage root, leaking
    # an enumeration of the bucket prefix. Existence-style queries return [].
    await store.save('a.txt', b'a')
    assert await store.glob(bad_pattern) == []


@pytest.mark.parametrize('key', ROOT_EQUIVALENT_KEYS)
def test_is_root_key_rejects_root_equivalent(key: str) -> None:
    """All empty / whitespace / slash / dot-equivalent inputs are root-equivalent.

    Covers the URL-decoded ``%2E`` / ``%2E%2E`` reproducer that previously
    bypassed the guard and returned 500 from the resources route.
    """
    assert is_root_key(key) is True


@pytest.mark.parametrize(
    'key',
    [
        'foo',
        'foo/bar',
        '/foo',
        '..foo',
        '.foo',
        # ``foo/.`` and ``./foo`` normalise to ``foo`` — a real path.
        'foo/.',
        './foo',
        # ``../foo`` escapes root but still names a real component, so
        # it's not "no path"; ``validate_path_safe`` raises on it.
        '../foo',
        'a/./b',
    ],
)
def test_is_root_key_accepts_real_paths(key: str) -> None:
    """Inputs that normalise to a real path component are not root-equivalent.

    ``foo/..`` is *not* in this list — it cancels to ``.`` and is covered
    by ``test_is_root_key_rejects_root_equivalent`` instead.
    """
    assert is_root_key(key) is False


@pytest.mark.asyncio
async def test_delete_root_key_does_not_delete_files(store: LocalAsyncFileStore) -> None:
    # Regression for the HIGH finding: an accidental delete('/', recursive=True)
    # must not remove existing data under the root.
    await store.save('keep/a.txt', b'a')
    await store.save('keep/b.txt', b'b')
    with pytest.raises(ValueError):
        await store.delete('/', recursive=True)
    assert await store.exists('keep/a.txt') is True
    assert await store.exists('keep/b.txt') is True
