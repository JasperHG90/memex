"""Session-scoped on-disk asset cache for binary resources.

Used by MCP and Hermes plugin tooling to hand off asset bytes to consuming
agents via the filesystem instead of inlining base64 in tool results. The
cache owns a tempdir for the duration of the session and evicts files when
the LRU bound is exceeded.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from cachetools import LRUCache


class _EvictingLRUCache(LRUCache):
    """LRUCache subclass that calls a hook on each eviction.

    The hook receives ``(key, value)`` for the entry being pushed out so the
    owner can clean up backing resources (e.g. unlink a temp file).
    """

    def __init__(
        self,
        maxsize: int,
        on_evict: Callable[[str, Path], None],
    ) -> None:
        super().__init__(maxsize=maxsize)
        self._on_evict = on_evict

    def popitem(self) -> tuple[str, Path]:
        key, value = super().popitem()
        try:
            self._on_evict(key, value)
        except Exception:  # noqa: BLE001 - eviction must never raise
            pass
        return key, value


class SessionAssetCache:
    """Per-session on-disk cache for fetched assets.

    Files live in ``self.tempdir`` and are keyed by their original asset path.
    When the LRU bound is exceeded the evicted file is unlinked from disk.
    Concurrent ``get_or_fetch`` calls for the same path coalesce into a single
    fetch.
    """

    def __init__(
        self,
        tempdir: Path | None = None,
        maxsize: int = 64,
    ) -> None:
        if tempdir is None:
            self.tempdir = Path(tempfile.mkdtemp(prefix='memex-assets-'))
        else:
            tempdir.mkdir(parents=True, exist_ok=True)
            self.tempdir = tempdir
        self._cache: _EvictingLRUCache = _EvictingLRUCache(
            maxsize=maxsize,
            on_evict=self._unlink_evicted,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    @staticmethod
    def _unlink_evicted(_key: str, value: Path) -> None:
        try:
            os.unlink(value)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _local_path_for(self, path: str) -> Path:
        digest = sha256(path.encode('utf-8')).hexdigest()[:16]
        safe_name = Path(path).name or 'asset'
        return self.tempdir / f'{digest}-{safe_name}'

    async def _lock_for(self, path: str) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(path)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[path] = lock
            return lock

    async def get_or_fetch(
        self,
        path: str,
        fetch: Callable[[str], Awaitable[bytes]],
    ) -> tuple[Path, str | None, int]:
        """Return ``(local_path, mime_type, size_bytes)`` for ``path``.

        On cache hit the existing file is stat'd and returned; on miss the
        ``fetch`` coroutine is awaited and the bytes are written to the
        session tempdir before being inserted into the LRU cache.
        """
        cached = self._cache.get(path)
        if cached is not None and cached.exists():
            mime, _ = mimetypes.guess_type(str(cached))
            return cached, mime, cached.stat().st_size

        lock = await self._lock_for(path)
        async with lock:
            cached = self._cache.get(path)
            if cached is not None and cached.exists():
                mime, _ = mimetypes.guess_type(str(cached))
                return cached, mime, cached.stat().st_size

            data = await fetch(path)
            local = self._local_path_for(path)
            local.write_bytes(data)
            self._cache[path] = local
            mime, _ = mimetypes.guess_type(str(local))
            return local, mime, local.stat().st_size

    def cleanup(self) -> None:
        """Remove the session tempdir and all cached files. Idempotent."""
        shutil.rmtree(self.tempdir, ignore_errors=True)
        self._cache.clear()
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, path: object) -> bool:
        return path in self._cache

    def __repr__(self) -> str:
        return (
            f'SessionAssetCache(tempdir={self.tempdir!s}, '
            f'size={len(self._cache)}/{self._cache.maxsize})'
        )

    # Allow ``with SessionAssetCache(...) as cache:`` style usage in tests.
    def __enter__(self) -> 'SessionAssetCache':
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.cleanup()
