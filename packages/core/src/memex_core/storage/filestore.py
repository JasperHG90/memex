import asyncio
import os

import logging
from typing import Generic, TypeVar, cast, Self, AsyncGenerator
from abc import ABCMeta, abstractmethod
from contextlib import asynccontextmanager

from cachetools import cached, LRUCache
from fsspec.asyn import AsyncFileSystem
from fsspec.implementations.local import LocalFileSystem
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

from memex_core.config import LocalFileStoreConfig, FileStoreConfig

T = TypeVar('T', bound=FileStoreConfig)


class BaseAsyncFileStore(Generic[T], metaclass=ABCMeta):
    def __init__(self, config: T):
        self.config = config
        self._logger = logging.getLogger(f'memex.core.storage.{self.__class__.__name__}')
        self._logger.debug(f'Initialized storage backend with config: {self.config}')
        self._fs = self.initialize()
        self._logger.debug(f'Storage backend filesystem initialized: {self._fs}')
        self._semaphore = asyncio.Semaphore(20)

        self._txn_id: str | None = None
        # NB: contains mapping of staged temp file to final file locations
        self._staged_files: dict[str, str] = {}
        self._pending_deletes: set[str] = set()

    @abstractmethod
    def initialize(self) -> AsyncFileSystem | AsyncFileSystemWrapper:
        """Initialize the storage backend and return the filesystem object."""
        pass

    @cached(cache=LRUCache(maxsize=128))
    def join_path(self, key: str) -> str:
        """Join root path with a key

        Args:
            key (str): relative path to the file, e.g. path/to/file.txt

        Returns:
            str: joined path, e.g. /home/vscode/workspace/path/to/file.txt

        Raises:
            ValueError: If root path is not set or key resolves outside root.
        """
        if not self.config.root:
            raise ValueError('Root path is not set.')
        if os.path.isabs(key):
            raise ValueError(f'Path traversal detected: {key!r} resolves outside root directory.')
        root_real = os.path.realpath(self.config.root)
        joined = os.path.realpath(os.path.join(root_real, key))
        if not joined.startswith(root_real + os.sep) and joined != root_real:
            raise ValueError(f'Path traversal detected: {key!r} resolves outside root directory.')
        return joined

    def begin_staging(self, transaction_id: str):
        """If using a transaction, then this is setting up the staging process
        on the file store backend. This means that files will be stored temporarily
        with a unique transaction id. Upon finalizing the transaction, the files
        are either moved to their permanent location or they are removed if the
        transaction fails.

        Args:
            transaction_id (str): idempotent transaction id
        """
        self._txn_id = transaction_id
        self._staged_files = {}
        self._pending_deletes = set()

    async def commit_staging(self):
        """Commit staged files to their final locations."""
        if not self._txn_id:
            return

        if self._staged_files:
            self._logger.debug(
                f'Committing {len(self._staged_files)} staged files for transaction {self._txn_id}'
            )
            tasks = []
            for tmp, final in self._staged_files.items():
                tasks.append(self.move_file(tmp, final))
            if tasks:
                await asyncio.gather(*tasks)

        if self._pending_deletes:
            tasks = []
            for key in self._pending_deletes:
                tasks.append(self._delete(key, recursive=False))
            if tasks:
                await asyncio.gather(*tasks)

        self._reset()

    async def rollback_staging(self):
        """Roll back staged files, removing any temporary files."""
        if not self._staged_files:
            return
        tasks = []
        for tmp in self._staged_files.keys():
            if await self.exists(tmp):
                tasks.append(self._delete(tmp, recursive=False))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._reset()

    def _reset(self):
        """Reset staging state."""
        self._txn_id = None
        self._staged_files = {}
        self._pending_deletes = set()

    @asynccontextmanager
    async def staging(self, transaction_id: str) -> AsyncGenerator[Self, None]:
        """Context manager for staging files with a transaction id."""
        self.begin_staging(transaction_id)
        try:
            yield self
            await self.commit_staging()
        except Exception as e:
            self._logger.error(f'Staging error: {e}. Rolling back staged files.')
            await self.rollback_staging()
            raise

    async def move_file(
        self,
        src_key: str,
        dest_key: str,
    ) -> None:
        """Move a file from src to dest using the provided filesystem.

        Args:
            fs (AsyncFileSystem | AsyncFileSystemWrapper): The filesystem to use.
            src (str): Source file path.
            dest (str): Destination file path.
        """
        src_fp = self.join_path(src_key)
        dest_fp = self.join_path(dest_key)
        self._logger.debug(f'Moving file from {src_fp} to {dest_fp}')
        async with self._semaphore:
            await self._fs._cp_file(src_fp, dest_fp)
            await self._fs._rm_file(src_fp)

    async def _save(self, key: str, data: bytes) -> None:
        """Save data to the storage backend without transaction logging."""
        fp = self.join_path(key)
        self._logger.debug(f'Saving data to path: {fp}')
        async with self._semaphore:
            await self._fs._makedirs(self._fs._parent(fp), exist_ok=True)
            await self._fs._pipe(fp, data)

    async def save(self, key: str, data: bytes) -> None:
        """
        Save data to the storage backend.

        Args:
            key: The identifier for the data.
            data: The string data to be saved.
        """
        if self._txn_id:
            target = f'{key}.stage_{self._txn_id}'
            await self._save(target, data)
            self._staged_files[target] = key
        else:
            await self._save(key, data)

    async def _delete(self, key: str, recursive: bool) -> None:
        """Delete data from the storage backend without transaction logging."""
        fp = self.join_path(key)
        self._logger.debug(f'Deleting data with key: {key}')
        if await self._fs._exists(fp):
            async with self._semaphore:
                await self._fs._rm(fp, recursive=recursive)

    async def delete(self, key: str, recursive: bool = False) -> None:
        """
        Delete data from the storage backend.

        Args:
            key: The identifier for the data to be deleted.
        """
        if self._txn_id:
            self._pending_deletes.add(key)
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
        async with self._semaphore:
            return cast(list[str], await self._fs._glob(self.join_path(pattern)))


class LocalAsyncFileStore(BaseAsyncFileStore[LocalFileStoreConfig]):
    """Async File store implementation for local file systems.

    This class provides an asynchronous interface for interacting with the
    local file system, leveraging the `fsspec` and `aiofile` libraries (via
    AsyncFileSystemWrapper).
    """

    def initialize(self) -> AsyncFileSystemWrapper:
        """Initialize the local filesystem backend.

        Returns:
            AsyncFileSystemWrapper: A wrapper around the LocalFileSystem that
                allows for asynchronous operations.
        """
        return AsyncFileSystemWrapper(LocalFileSystem())


# Alias for type hinting
FileStore = BaseAsyncFileStore


def get_filestore(config: FileStoreConfig) -> FileStore:
    """Factory function to get the appropriate file store backend."""
    if isinstance(config, LocalFileStoreConfig):
        return LocalAsyncFileStore(config)

    # Fallback for dicts or other models where 'type' might be present
    config_dict = config.model_dump() if hasattr(config, 'model_dump') else config
    ctype = config_dict.get('type') if isinstance(config_dict, dict) else None

    if ctype == 'local':
        return LocalAsyncFileStore(cast(LocalFileStoreConfig, config))

    raise ValueError(f'Unsupported file store type: {ctype}')
