"""Session-scoped on-disk asset cache for binary resources.

Used by MCP and Hermes plugin tooling to hand off asset bytes to consuming
agents via the filesystem instead of inlining base64 in tool results. The
cache owns a tempdir for the duration of the session and evicts files when
the LRU bound is exceeded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import shutil
import tempfile
import threading
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from cachetools import LRUCache

logger = logging.getLogger(__name__)


def _stat_size(path: Path) -> int:
    return path.stat().st_size


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


MAX_RESOURCE_BYTES: int = 50 * 1024 * 1024
MAX_GET_RESOURCES_PATHS: int = 50


class SessionAssetCache:
    """Per-session on-disk cache for fetched assets.

    Files live in ``self.tempdir`` and are keyed by their original asset
    path; ``get_or_fetch`` uses that key. ``register`` (used for derived
    files like resized siblings) keys by the file's own absolute string
    path instead, so the two key spaces are disjoint and ``__contains__``
    accepts either form.

    Concurrent ``get_or_fetch`` calls for the same path coalesce into a
    single fetch. A single ``threading.Lock`` serialises every mutation
    of ``_cache`` and ``_locks`` so the Hermes plugin can call
    ``register`` / ``invalidate`` from its synchronous dispatch thread
    while the MCP event loop is also running.
    """

    def __init__(
        self,
        tempdir: Path | None = None,
        maxsize: int = 64,
    ) -> None:
        if tempdir is None:
            self.tempdir = Path(tempfile.mkdtemp(prefix='memex-assets-'))
            self._owns_tempdir = True
        else:
            tempdir.mkdir(parents=True, exist_ok=True)
            self.tempdir = tempdir
            self._owns_tempdir = False
        self._cache: _EvictingLRUCache = _EvictingLRUCache(
            maxsize=maxsize,
            on_evict=self._unlink_evicted,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._mutation_lock = threading.Lock()

    def _unlink_evicted(self, key: str, value: Path) -> None:
        try:
            os.unlink(value)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug('Failed to unlink evicted cache file %s: %s', value, exc)
        self._locks.pop(key, None)

    def _local_path_for(self, path: str) -> Path:
        digest = sha256(path.encode('utf-8')).hexdigest()[:16]
        safe_name = Path(path).name or 'asset'
        return self.tempdir / f'{digest}-{safe_name}'

    def _lock_for(self, path: str) -> asyncio.Lock:
        with self._mutation_lock:
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
        """Return ``(local_path, mime_type, size_bytes)`` for ``path``."""
        cached: Path | None
        with self._mutation_lock:
            cached = self._cache.get(path)
        if cached is not None:
            # Lockless fast path; eviction between checks falls through to refetch.
            try:
                if await asyncio.to_thread(cached.exists):
                    mime, _ = mimetypes.guess_type(str(cached))
                    size = await asyncio.to_thread(_stat_size, cached)
                    return cached, mime, size
            except FileNotFoundError:
                pass

        lock = self._lock_for(path)
        async with lock:
            with self._mutation_lock:
                cached = self._cache.get(path)
            if cached is not None:
                try:
                    if await asyncio.to_thread(cached.exists):
                        mime, _ = mimetypes.guess_type(str(cached))
                        size = await asyncio.to_thread(_stat_size, cached)
                        return cached, mime, size
                except FileNotFoundError:
                    pass

            try:
                data = await fetch(path)
            except Exception:
                # Drop the orphan lock if no entry was ever stored.
                with self._mutation_lock:
                    if path not in self._cache:
                        self._locks.pop(path, None)
                raise
            local = self._local_path_for(path)
            await asyncio.to_thread(local.write_bytes, data)
            with self._mutation_lock:
                self._cache[path] = local
            mime, _ = mimetypes.guess_type(str(local))
            size = await asyncio.to_thread(_stat_size, local)
            return local, mime, size

    def invalidate(self, path: str) -> None:
        """Drop ``path`` from the LRU and unlink its backing file. Idempotent."""
        with self._mutation_lock:
            local = self._cache.pop(path, None)
            self._locks.pop(path, None)
        if local is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                os.unlink(local)

    def register(self, local_path: Path) -> Path:
        """Insert an externally-created file into the LRU.

        Keyed by ``str(local_path)`` (the file's own absolute path) — distinct
        from ``get_or_fetch`` which keys by the upstream asset path. ``local_path``
        must already live under ``self.tempdir``; ``ValueError`` otherwise.
        """
        resolved = local_path.resolve()
        tempdir_resolved = self.tempdir.resolve()
        if not resolved.is_relative_to(tempdir_resolved):
            raise ValueError(
                f'register() refused: {local_path} is not inside session tempdir {self.tempdir}'
            )
        with self._mutation_lock:
            self._cache[str(local_path)] = local_path
        return local_path

    def cleanup(self) -> None:
        """Drop all cache state and unlink backing files. Idempotent.

        If the tempdir was auto-created (no ``tempdir=`` argument), it is
        removed wholesale. A caller-supplied tempdir is preserved — only the
        files this cache wrote into it are unlinked.
        """
        with self._mutation_lock:
            entries = list(self._cache.values())
            # ``LRUCache.clear()`` is dict.clear() — bypasses popitem(), so
            # file unlinks are done explicitly here.
            self._cache.clear()
            self._locks.clear()
        if self._owns_tempdir:
            shutil.rmtree(self.tempdir, ignore_errors=True)
        else:
            for local in entries:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.unlink(local)

    def __len__(self) -> int:
        with self._mutation_lock:
            return len(self._cache)

    def __contains__(self, path: object) -> bool:
        with self._mutation_lock:
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
