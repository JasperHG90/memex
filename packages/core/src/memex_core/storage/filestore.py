import asyncio
import os
import posixpath

import logging
from dataclasses import dataclass, field
from typing import Generic, TypeVar, cast, Self, AsyncGenerator
from abc import ABCMeta, abstractmethod
from contextlib import asynccontextmanager

from cachetools import LRUCache
from fsspec.asyn import AsyncFileSystem
from fsspec.implementations.local import LocalFileSystem
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

from memex_core.config import (
    LocalFileStoreConfig,
    S3FileStoreConfig,
    GCSFileStoreConfig,
    FileStoreConfig,
)

T = TypeVar('T', bound=FileStoreConfig)


def is_root_key(key: str) -> bool:
    """Return True if *key* would resolve to the storage root.

    Empty / whitespace-only / all-slash keys are rewritten to the root by
    :func:`validate_path_safe` (the empty-key form is intentional — see
    :meth:`BaseAsyncFileStore.check_connection`). Public read and write
    entry points must reject such keys so callers don't end up issuing
    operations against the bucket prefix itself.

    Convention for callers using this guard:
      * Read methods (``load``) raise ``FileNotFoundError`` so the HTTP
        layer maps to 404.
      * Mutating methods (``save`` / ``delete`` / ``move_file``) raise
        ``ValueError`` because operating on the root is a programmer
        error / refusal-to-act, not a "missing resource".
      * Existence-style queries (``exists`` / ``is_dir`` / ``glob``) return
        a falsy value (``False`` / ``[]``) — empty input legitimately maps
        to "no such resource".
    """
    return not key or not key.strip().strip('/')


def validate_path_safe(base_path: str, requested_path: str, *, local: bool = False) -> str:
    """Validate that a requested path does not escape the base directory.

    Uses ``os.path.realpath`` for local paths (resolving symlinks) and
    ``posixpath.normpath`` for remote/cloud paths.

    Args:
        base_path: The root directory or prefix.
        requested_path: The relative path to join under *base_path*.
        local: If ``True``, use OS-native resolution (``os.path.realpath``).

    Returns:
        The resolved, joined path.

    Raises:
        ValueError: If *requested_path* resolves outside *base_path*.
    """
    stripped = requested_path.lstrip('/')
    if local:
        root = os.path.realpath(base_path)
        joined = os.path.realpath(os.path.join(root, stripped))
        separator = os.sep
    else:
        root = base_path.rstrip('/')
        joined = posixpath.normpath(f'{root}/{stripped}') if stripped else root
        separator = '/'

    if not joined.startswith(root + separator) and joined != root:
        raise ValueError(
            f'Path traversal detected: {requested_path!r} resolves outside root directory.'
        )
    return joined


@dataclass
class _StagingState:
    """Per-transaction staging state."""

    staged_files: dict[str, str] = field(default_factory=dict)  # staged_key -> final_key
    pending_deletes: dict[str, bool] = field(default_factory=dict)  # key -> recursive


class BaseAsyncFileStore(Generic[T], metaclass=ABCMeta):
    def __init__(self, config: T):
        self.config = config
        self._logger = logging.getLogger(f'memex.core.storage.{self.__class__.__name__}')
        self._logger.debug(f'Initialized storage backend with config: {self.config}')
        self._fs = self.initialize()
        self._logger.debug(f'Storage backend filesystem initialized: {self._fs}')
        self._semaphore = asyncio.Semaphore(20)

        self._active_stages: dict[str, _StagingState] = {}

    @abstractmethod
    def initialize(self) -> AsyncFileSystem | AsyncFileSystemWrapper:
        """Initialize the storage backend and return the filesystem object."""
        pass

    def join_path(self, key: str) -> str:
        """Join root path with a key using POSIX semantics (cloud-safe).

        Args:
            key: relative path to the file, e.g. path/to/file.txt

        Returns:
            Joined path under root.

        Raises:
            ValueError: If root path is not set or key resolves outside root.
        """
        if not self.config.root:
            raise ValueError('Root path is not set.')
        return validate_path_safe(self.config.root, key)

    async def check_connection(self) -> bool:
        """Verify the backend is reachable by listing the root."""
        try:
            async with self._semaphore:
                await self._fs._ls(self.join_path(''), detail=False)
            return True
        except Exception:
            self._logger.warning('Connection check failed for %s', self.__class__.__name__)
            return False

    def begin_staging(self, transaction_id: str) -> None:
        """Set up staging for a transaction. Files saved with this txn_id will be
        stored temporarily until commit or rollback.

        Args:
            transaction_id: unique transaction id.

        Raises:
            ValueError: If a staging session with this id is already active.
        """
        if transaction_id in self._active_stages:
            raise ValueError(f'Staging transaction {transaction_id!r} is already active.')
        self._active_stages[transaction_id] = _StagingState()

    def _get_stage(self, txn_id: str) -> _StagingState:
        """Look up active staging state, raising if not found."""
        try:
            return self._active_stages[txn_id]
        except KeyError:
            raise ValueError(f'No active staging transaction {txn_id!r}.')

    async def commit_staging(self, txn_id: str) -> None:
        """Commit staged files for *txn_id* to their final locations."""
        stage = self._get_stage(txn_id)

        if stage.staged_files:
            self._logger.debug(
                f'Committing {len(stage.staged_files)} staged files for transaction {txn_id}'
            )
            tasks = [self.move_file(tmp, final) for tmp, final in stage.staged_files.items()]
            if tasks:
                await asyncio.gather(*tasks)

        if stage.pending_deletes:
            tasks = [
                self._delete(key, recursive=recursive)
                for key, recursive in stage.pending_deletes.items()
            ]
            if tasks:
                await asyncio.gather(*tasks)

        self._reset(txn_id)

    async def rollback_staging(self, txn_id: str) -> None:
        """Roll back staged files for *txn_id*, removing temporary files."""
        stage = self._active_stages.get(txn_id)
        if stage is None or not stage.staged_files:
            self._reset(txn_id)
            return
        tasks = []
        for tmp in stage.staged_files:
            if await self.exists(tmp):
                tasks.append(self._delete(tmp, recursive=False))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._reset(txn_id)

    def _reset(self, txn_id: str) -> None:
        """Remove staging state for *txn_id*."""
        self._active_stages.pop(txn_id, None)

    @asynccontextmanager
    async def staging(self, transaction_id: str) -> AsyncGenerator[Self, None]:
        """Context manager for staging files with a transaction id."""
        self.begin_staging(transaction_id)
        try:
            yield self
            await self.commit_staging(transaction_id)
        except Exception as e:
            self._logger.error(f'Staging error: {e}. Rolling back staged files.')
            await self.rollback_staging(transaction_id)
            raise

    async def move_file(
        self,
        src_key: str,
        dest_key: str,
    ) -> None:
        """Move a file or directory from src to dest using the provided filesystem.

        Args:
            src_key: Source path relative to root.
            dest_key: Destination path relative to root.
        """
        if is_root_key(src_key) or is_root_key(dest_key):
            raise ValueError(f'Refusing to move with root key: src={src_key!r}, dest={dest_key!r}')
        src_fp = self.join_path(src_key)
        dest_fp = self.join_path(dest_key)
        self._logger.debug(f'Moving file from {src_fp} to {dest_fp}')
        async with self._semaphore:
            if await self._fs._isdir(src_fp):
                files = await self._fs._find(src_fp)
                for f in files:
                    rel = f[len(src_fp.rstrip('/')) + 1 :]
                    target = posixpath.join(dest_fp, rel)
                    await self._fs._makedirs(posixpath.dirname(target), exist_ok=True)
                    await self._fs._cp_file(f, target)
                await self._fs._rm(src_fp, recursive=True)
            else:
                await self._fs._cp_file(src_fp, dest_fp)
                await self._fs._rm_file(src_fp)

    async def _save(self, key: str, data: bytes) -> None:
        """Save data to the storage backend without transaction logging."""
        fp = self.join_path(key)
        self._logger.debug(f'Saving data to path: {fp}')
        async with self._semaphore:
            await self._fs._makedirs(self._fs._parent(fp), exist_ok=True)
            await self._fs._pipe(fp, data)

    async def save(self, key: str, data: bytes, *, txn_id: str | None = None) -> None:
        """
        Save data to the storage backend.

        Args:
            key: The identifier for the data.
            data: The data to be saved.
            txn_id: Optional transaction id. When provided the write is staged.
        """
        if is_root_key(key):
            raise ValueError(f'Refusing to save to root key: {key!r}')
        if txn_id is not None:
            stage = self._get_stage(txn_id)
            target = f'{key}.stage_{txn_id}'
            await self._save(target, data)
            stage.staged_files[target] = key
        else:
            await self._save(key, data)

    async def _delete(self, key: str, recursive: bool) -> None:
        """Delete data from the storage backend without transaction logging."""
        fp = self.join_path(key)
        self._logger.debug(f'Deleting data with key: {key}')
        if await self._fs._exists(fp):
            async with self._semaphore:
                await self._fs._rm(fp, recursive=recursive)

    async def delete(self, key: str, *, txn_id: str | None = None, recursive: bool = False) -> None:
        """
        Delete data from the storage backend.

        Args:
            key: The identifier for the data to be deleted.
            txn_id: Optional transaction id. When provided the delete is deferred.
            recursive: Whether to delete recursively.
        """
        if is_root_key(key):
            raise ValueError(f'Refusing to delete root key: {key!r}')
        if txn_id is not None:
            stage = self._get_stage(txn_id)
            stage.pending_deletes[key] = recursive
        else:
            await self._delete(key, recursive=recursive)

    async def load(self, key: str) -> bytes:
        """
        Load data from the storage backend.

        Args:
            key: The identifier for the data.

        Returns:
            The loaded string data.
        """
        if is_root_key(key):
            raise FileNotFoundError(f'Resource key is empty: {key!r}')
        fp = self.join_path(key)
        self._logger.debug(f'Loading data from path: {fp}')
        async with self._semaphore:
            return await self._fs._cat_file(fp)

    async def exists(self, key: str) -> bool:
        """
        Check if a key exists in the storage backend.

        Args:
            key: The identifier for the data.

        Returns:
            True if the key exists, False otherwise.
        """
        if is_root_key(key):
            return False
        self._logger.debug(f'Checking for existence of key: {key}')
        async with self._semaphore:
            return await self._fs._exists(self.join_path(key))

    async def is_dir(self, key: str) -> bool:
        """
        Check if a key is a directory in the storage backend.

        Args:
            key: The identifier for the data.

        Returns:
            True if the key is a directory, False otherwise.
        """
        if is_root_key(key):
            return False
        async with self._semaphore:
            return await self._fs._isdir(self.join_path(key))

    async def glob(self, pattern: str) -> list[str]:
        """
        Glob for files matching a pattern in the storage backend.

        Args:
            pattern: The glob pattern to match files.

        Returns:
            A list of matching file paths.
        """
        if is_root_key(pattern):
            return []
        async with self._semaphore:
            return cast(list[str], await self._fs._glob(self.join_path(pattern)))


class LocalAsyncFileStore(BaseAsyncFileStore[LocalFileStoreConfig]):
    """Async File store implementation for local file systems.

    This class provides an asynchronous interface for interacting with the
    local file system, leveraging the `fsspec` and `aiofile` libraries (via
    AsyncFileSystemWrapper).
    """

    def __init__(self, config: LocalFileStoreConfig):
        self._join_path_cache: LRUCache = LRUCache(maxsize=128)
        super().__init__(config)

    def initialize(self) -> AsyncFileSystemWrapper:
        """Initialize the local filesystem backend.

        Returns:
            AsyncFileSystemWrapper: A wrapper around the LocalFileSystem that
                allows for asynchronous operations.
        """
        return AsyncFileSystemWrapper(LocalFileSystem())

    async def check_connection(self) -> bool:
        """Verify the backend is reachable, creating the root directory if needed."""
        root = self.config.root
        if root:
            try:
                os.makedirs(root, exist_ok=True)
            except OSError:
                return False
        return await super().check_connection()

    def join_path(self, key: str) -> str:
        """Join root path with a key using OS-native path resolution.

        Results are cached per-instance via an LRU cache.

        Args:
            key: relative path to the file, e.g. path/to/file.txt

        Returns:
            Joined absolute path under root.

        Raises:
            ValueError: If root path is not set or key resolves outside root.
        """
        if not self.config.root:
            raise ValueError('Root path is not set.')
        cached_result = self._join_path_cache.get(key)
        if cached_result is not None:
            return cached_result
        result = validate_path_safe(self.config.root, key, local=True)
        self._join_path_cache[key] = result
        return result


class S3AsyncFileStore(BaseAsyncFileStore['S3FileStoreConfig']):
    """Async File store implementation for S3-compatible storage (AWS S3, MinIO)."""

    def initialize(self) -> AsyncFileSystem:
        try:
            from s3fs import S3FileSystem
        except ImportError:
            raise ImportError(
                'S3 file store requires the s3fs package. Install with: uv add "memex-core[s3]"'
            )
        cfg = self.config
        kwargs: dict = {'anon': False}
        if cfg.endpoint_url:
            kwargs['endpoint_url'] = cfg.endpoint_url
            kwargs['client_kwargs'] = {'endpoint_url': cfg.endpoint_url}
        if cfg.region:
            kwargs['client_kwargs'] = {**kwargs.get('client_kwargs', {}), 'region_name': cfg.region}
        if cfg.access_key_id:
            kwargs['key'] = cfg.access_key_id.get_secret_value()
        if cfg.secret_access_key:
            kwargs['secret'] = cfg.secret_access_key.get_secret_value()
        if cfg.session_token:
            kwargs['token'] = cfg.session_token.get_secret_value()
        return S3FileSystem(**kwargs)

    def join_path(self, key: str) -> str:
        """Join bucket/prefix with a key. Returns bare path (no s3:// protocol).

        Args:
            key: relative path to the file.

        Returns:
            Joined path as bucket/prefix/key.

        Raises:
            ValueError: If path traversal is detected.
        """
        bucket = self.config.bucket
        prefix = self.config.root.strip('/')
        root = f'{bucket}/{prefix}' if prefix else bucket
        return validate_path_safe(root, key)


class GCSAsyncFileStore(BaseAsyncFileStore['GCSFileStoreConfig']):
    """Async File store implementation for Google Cloud Storage."""

    def initialize(self) -> AsyncFileSystem:
        try:
            from gcsfs import GCSFileSystem
        except ImportError:
            raise ImportError(
                'GCS file store requires the gcsfs package. Install with: uv add "memex-core[gcs]"'
            )
        cfg = self.config
        kwargs: dict = {}
        if cfg.project:
            kwargs['project'] = cfg.project
        if cfg.token:
            kwargs['token'] = cfg.token
        if cfg.endpoint_url:
            kwargs['endpoint_url'] = cfg.endpoint_url
        return GCSFileSystem(**kwargs)

    def join_path(self, key: str) -> str:
        """Join bucket/prefix with a key. Returns bare path (no gs:// protocol).

        Args:
            key: relative path to the file.

        Returns:
            Joined path as bucket/prefix/key.

        Raises:
            ValueError: If path traversal is detected.
        """
        bucket = self.config.bucket
        prefix = self.config.root.strip('/')
        root = f'{bucket}/{prefix}' if prefix else bucket
        return validate_path_safe(root, key)


# Alias for type hinting
FileStore = BaseAsyncFileStore


def get_filestore(config: FileStoreConfig) -> FileStore:
    """Factory function to get the appropriate file store backend."""
    if isinstance(config, LocalFileStoreConfig):
        return LocalAsyncFileStore(config)
    if isinstance(config, S3FileStoreConfig):
        return S3AsyncFileStore(config)
    if isinstance(config, GCSFileStoreConfig):
        return GCSAsyncFileStore(config)

    # Fallback for dicts or other models where 'type' might be present
    config_dict = config.model_dump() if hasattr(config, 'model_dump') else config
    ctype = config_dict.get('type') if isinstance(config_dict, dict) else None

    if ctype == 'local':
        return LocalAsyncFileStore(cast(LocalFileStoreConfig, config))
    if ctype == 's3':
        return S3AsyncFileStore(cast(S3FileStoreConfig, config))
    if ctype == 'gcs':
        return GCSAsyncFileStore(cast(GCSFileStoreConfig, config))

    raise ValueError(f'Unsupported file store type: {ctype}')
