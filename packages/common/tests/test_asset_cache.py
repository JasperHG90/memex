"""Tests for memex_common.asset_cache.SessionAssetCache."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from memex_common.asset_cache import SessionAssetCache


def _make_cache(tmp_path: Path, maxsize: int = 64) -> SessionAssetCache:
    return SessionAssetCache(tempdir=tmp_path / 'assets', maxsize=maxsize)


class _CountingFetcher:
    def __init__(self, payload: bytes = b'pixels') -> None:
        self.calls: list[str] = []
        self.payload = payload

    async def __call__(self, path: str) -> bytes:
        self.calls.append(path)
        return self.payload


async def test_get_or_fetch_caches_first_call(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    fetcher = _CountingFetcher()

    first_path, _, _ = await cache.get_or_fetch('a/b/foo.png', fetcher)
    second_path, _, _ = await cache.get_or_fetch('a/b/foo.png', fetcher)

    assert first_path == second_path
    assert len(fetcher.calls) == 1


async def test_get_or_fetch_returns_correct_mime(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    fetcher = _CountingFetcher(payload=b'\x89PNG\r\n\x1a\n')

    local, mime, size = await cache.get_or_fetch('image/foo.png', fetcher)

    assert mime == 'image/png'
    assert size == len(fetcher.payload)
    assert local.exists()
    assert local.read_bytes() == fetcher.payload


async def test_lru_eviction_unlinks_file(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path, maxsize=2)
    fetcher = _CountingFetcher()

    first, _, _ = await cache.get_or_fetch('one.png', fetcher)
    second, _, _ = await cache.get_or_fetch('two.png', fetcher)
    third, _, _ = await cache.get_or_fetch('three.png', fetcher)

    assert second.exists()
    assert third.exists()
    assert not first.exists()


async def test_cleanup_removes_tempdir(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    fetcher = _CountingFetcher()
    await cache.get_or_fetch('foo.png', fetcher)
    tempdir = cache.tempdir
    assert tempdir.exists()

    cache.cleanup()

    assert not tempdir.exists()
    # Idempotent — no error on second call.
    cache.cleanup()


async def test_concurrent_same_path_coalesces(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)

    barrier = asyncio.Event()
    call_count = 0

    async def slow_fetch(_path: str) -> bytes:
        nonlocal call_count
        call_count += 1
        await barrier.wait()
        return b'data'

    task_a = asyncio.create_task(cache.get_or_fetch('shared.png', slow_fetch))
    task_b = asyncio.create_task(cache.get_or_fetch('shared.png', slow_fetch))
    # Give both tasks a chance to enter get_or_fetch and contend on the lock.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    barrier.set()
    result_a, _, _ = await task_a
    result_b, _, _ = await task_b

    assert call_count == 1
    assert result_a == result_b


async def test_filename_with_path_separators_is_sanitised(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    fetcher = _CountingFetcher()

    # ``Path('foo/bar/../etc/passwd').name == 'passwd'`` — but the resolved
    # parent must still be the session tempdir, not anywhere else on disk.
    malicious = 'foo/bar/../etc/passwd'
    local, _, _ = await cache.get_or_fetch(malicious, fetcher)

    resolved_parent = local.resolve().parent
    expected_parent = cache.tempdir.resolve()
    assert resolved_parent == expected_parent
    # Sanity: the file is inside the tempdir.
    assert str(local.resolve()).startswith(str(expected_parent))


async def test_repr_and_membership(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    fetcher = _CountingFetcher()
    await cache.get_or_fetch('foo.png', fetcher)

    assert 'foo.png' in cache
    assert len(cache) == 1
    assert 'SessionAssetCache' in repr(cache)


async def test_context_manager_calls_cleanup(tmp_path: Path) -> None:
    target = tmp_path / 'ctx'
    with SessionAssetCache(tempdir=target) as cache:
        await cache.get_or_fetch('foo.png', _CountingFetcher())
        assert cache.tempdir.exists()

    assert not target.exists()


async def test_eviction_then_refetch_recreates_file(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path, maxsize=1)
    fetcher = _CountingFetcher()

    await cache.get_or_fetch('one.png', fetcher)
    await cache.get_or_fetch('two.png', fetcher)
    # one.png evicted; refetch should call fetch a third time.
    again, _, _ = await cache.get_or_fetch('one.png', fetcher)

    assert again.exists()
    assert len(fetcher.calls) == 3


async def test_lock_dict_bounded_by_lru_size(tmp_path: Path) -> None:
    """Per-path locks must be evicted alongside their cache entries.

    Without this, a long-running session that fetches many distinct paths
    would grow ``_locks`` unboundedly even though the LRU caps the file
    count. The eviction hook drops the matching lock in the same critical
    section as the file unlink.
    """
    cache = _make_cache(tmp_path, maxsize=2)
    fetcher = _CountingFetcher()

    for path in ('one.png', 'two.png', 'three.png', 'four.png', 'five.png'):
        await cache.get_or_fetch(path, fetcher)

    assert len(cache._locks) <= 2
    # The lock dict must track the same set of keys the LRU still holds.
    assert set(cache._locks).issubset(set(cache._cache))


async def test_register_inserts_into_lru(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    sibling = cache.tempdir / 'resized.png'
    sibling.write_bytes(b'\x89PNG\r\n\x1a\n')

    returned = cache.register(sibling)

    assert returned == sibling
    assert str(sibling) in cache._cache


async def test_register_evicts_when_over_maxsize(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path, maxsize=2)
    paths: list[Path] = []
    for name in ('a.png', 'b.png', 'c.png'):
        target = cache.tempdir / name
        target.write_bytes(b'\x89PNG\r\n\x1a\n')
        cache.register(target)
        paths.append(target)

    # First registration must have been evicted (LRU policy) and unlinked.
    assert not paths[0].exists()
    assert paths[1].exists()
    assert paths[2].exists()


async def test_register_rejects_path_outside_tempdir(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'\x89PNG\r\n\x1a\n')

    with pytest.raises(ValueError, match='not inside session tempdir'):
        cache.register(outside)


async def test_eviction_then_read_raises_clean_filenotfound(tmp_path: Path) -> None:
    """Documents the contract for callers reading file:// URIs after subsequent fetches.

    Once an asset is evicted under LRU pressure, its on-disk file is unlinked.
    Callers holding the previously-returned ``Path`` must handle ``FileNotFoundError``
    gracefully — the cache makes no promise that the file remains readable
    after later cache activity.
    """
    cache = _make_cache(tmp_path, maxsize=1)
    fetcher = _CountingFetcher()

    first, _, _ = await cache.get_or_fetch('a.png', fetcher)
    assert first.exists()

    # Fetching b.png evicts a.png; the underlying file must be gone.
    await cache.get_or_fetch('b.png', fetcher)
    assert not first.exists()

    with pytest.raises(FileNotFoundError):
        first.read_bytes()


async def test_eviction_oserror_logged_not_raised(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` during eviction unlink must be logged but never raised.

    Permission-denied or transient FS errors on cleanup must not crash the
    eviction path — the LRU's invariants depend on ``popitem()`` returning.
    """
    import logging as stdlib_logging
    import os as stdlib_os

    cache = _make_cache(tmp_path, maxsize=1)
    fetcher = _CountingFetcher()
    await cache.get_or_fetch('a.png', fetcher)

    real_unlink = stdlib_os.unlink

    def flaky_unlink(path: str | Path) -> None:
        if Path(path).name.endswith('a.png') or 'a.png' in str(path):
            raise OSError('simulated permission denied')
        real_unlink(path)

    monkeypatch.setattr('memex_common.asset_cache.os.unlink', flaky_unlink)

    with caplog.at_level(stdlib_logging.DEBUG, logger='memex_common.asset_cache'):
        # Evicting a.png must not raise even though unlink fails.
        await cache.get_or_fetch('b.png', fetcher)

    assert any('Failed to unlink evicted cache file' in record.message for record in caplog.records)
