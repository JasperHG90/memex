"""Tests for memex_common.asset_cache.SessionAssetCache."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
