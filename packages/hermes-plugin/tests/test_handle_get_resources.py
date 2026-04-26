"""Tests for the disk-handoff variant of ``handle_get_resources`` and the
provider-level ``SessionAssetCache`` lifecycle.

The MCP / Hermes harness has a ``tool_output.max_bytes`` ceiling that
silently truncated base64 payloads; issue #59 replaces inline bytes with
local-file paths under a per-session tempdir. These tests cover:

- AC-006: response shape contains ``local_path`` / ``size_bytes`` and
  explicitly does NOT contain ``content_b64``.
- AC-004 / cache hit: repeat fetches of the same path call the API once.
- AC-011: oversize assets surface as ``error`` entries AND the bytes are
  unlinked so the tempdir doesn't leak the rejected payload.
- AC-007: the provider mints a ``SessionAssetCache`` in ``initialize`` and
  ``_atexit_shutdown`` removes its tempdir.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from memex_common.asset_cache import SessionAssetCache
from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.provider import MemexMemoryProvider
from memex_hermes_plugin.memex.tools import dispatch


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


@pytest.fixture
def asset_cache(tmp_path: Path):
    cache = SessionAssetCache(tempdir=tmp_path / 'cache')
    yield cache
    cache.cleanup()


def test_get_resources_returns_local_path(config, vault_id, asset_cache):
    """AC-006: response shape has ``local_path``, ``path``, ``filename``,
    ``mime_type``, ``size_bytes``; ``content_b64`` is NEVER set."""
    api = Mock()
    raw = b'PNG_FAKE'
    api.get_resource = AsyncMock(return_value=raw)
    out = dispatch(
        'memex_get_resources',
        {'paths': ['abc/diagram.png']},
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    entry = data['results'][0]
    assert entry['path'] == 'abc/diagram.png'
    assert entry['filename'] == 'diagram.png'
    assert entry['mime_type'] == 'image/png'
    assert entry['size_bytes'] == len(raw)
    assert 'local_path' in entry
    assert 'content_b64' not in entry, 'disk-handoff must never inline bytes'
    assert 'error' not in entry

    # The local_path resolves inside the cache's tempdir and contains the
    # bytes returned by api.get_resource.
    local = Path(entry['local_path'])
    assert local.exists()
    assert local.read_bytes() == raw
    assert local.resolve().is_relative_to(asset_cache.tempdir.resolve())


def test_get_resources_caches_repeat_calls(config, vault_id, asset_cache):
    """AC-004: a second fetch for the same path is served from the LRU cache,
    so ``api.get_resource`` is awaited exactly once."""
    api = Mock()
    api.get_resource = AsyncMock(return_value=b'cached-bytes')

    args = {'paths': ['abc/repeat.png']}

    out1 = dispatch(
        'memex_get_resources',
        args,
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    out2 = dispatch(
        'memex_get_resources',
        args,
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )

    assert api.get_resource.await_count == 1, 'Repeat fetches must hit the cache, not the API'
    # Both responses point at the same on-disk file.
    p1 = json.loads(out1)['results'][0]['local_path']
    p2 = json.loads(out2)['results'][0]['local_path']
    assert p1 == p2


def test_get_resources_oversize_rejected_after_download(config, vault_id, asset_cache, monkeypatch):
    """AC-011: an asset whose bytes exceed ``_MAX_RESOURCE_BYTES`` after the
    fetch lands as an ``error`` entry AND the file is removed from disk —
    the oversize bytes must not leak into the tempdir."""
    from memex_hermes_plugin.memex import tools as tools_mod

    monkeypatch.setattr(tools_mod, '_MAX_RESOURCE_BYTES', 4, raising=True)

    api = Mock()
    api.get_resource = AsyncMock(return_value=b'TOOBIG!')

    out = dispatch(
        'memex_get_resources',
        {'paths': ['oversize.png']},
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    entry = json.loads(out)['results'][0]
    assert entry['path'] == 'oversize.png'
    assert 'error' in entry
    assert 'exceeds max size' in entry['error']
    assert 'local_path' not in entry
    assert 'content_b64' not in entry

    # No leftover bytes in the cache tempdir.
    leftover = list(asset_cache.tempdir.iterdir())
    assert leftover == [], f'oversize asset bytes leaked into tempdir: {leftover!r}'


def test_get_resources_partial_failure_isolation(config, vault_id, asset_cache):
    """Per-path partial failure isolation: a missing asset becomes an error
    entry without aborting the whole batch, AND no ``content_b64`` ever
    appears (regression guard)."""
    api = Mock()
    api.get_resource = AsyncMock(
        side_effect=[b'ok1', FileNotFoundError('missing'), b'ok3'],
    )

    out = dispatch(
        'memex_get_resources',
        {'paths': ['p1.png', 'p2.png', 'p3.png']},
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    results = json.loads(out)['results']
    assert len(results) == 3
    assert 'local_path' in results[0]
    assert 'error' not in results[0]
    assert results[1]['path'] == 'p2.png'
    assert 'error' in results[1]
    assert 'local_path' not in results[1]
    assert 'local_path' in results[2]
    for entry in results:
        assert 'content_b64' not in entry


def test_get_resources_rejects_too_many_paths(config, vault_id, asset_cache):
    api = Mock()
    api.get_resource = AsyncMock()
    out = dispatch(
        'memex_get_resources',
        {'paths': [f'p{i}.png' for i in range(51)]},
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'too many' in data['error'].lower()
    api.get_resource.assert_not_awaited()


def test_get_resources_without_cache_returns_error(config, vault_id):
    """Defensive: without an initialized cache the handler refuses to run
    rather than silently writing to a process-wide tempdir."""
    api = Mock()
    api.get_resource = AsyncMock()
    out = dispatch(
        'memex_get_resources',
        {'paths': ['x.png']},
        api=api,
        config=config,
        vault_id=vault_id,
        asset_cache=None,
    )
    data = json.loads(out)
    assert 'error' in data
    api.get_resource.assert_not_awaited()


# ---------------------------------------------------------------------------
# Provider-level lifecycle (AC-007)
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_provider(tmp_path, monkeypatch):
    """Spin up a fully initialized ``MemexMemoryProvider`` with a stubbed API.

    Mirrors ``provider_with_stubbed_api`` from ``test_provider.py`` so we can
    exercise the cache lifecycle without standing up a real Memex server.
    """
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'test-vault')

    fake_api = Mock()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    fake_api.get_session_briefing = AsyncMock(return_value='# Briefing')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))
    fake_api.kv_put = AsyncMock()

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('session-cache', hermes_home=str(tmp_path), platform='cli')
        try:
            yield provider
        finally:
            try:
                provider.shutdown()
            except Exception:
                pass


def test_provider_initialize_creates_cache(stubbed_provider):
    """AC-007: ``initialize`` mints a ``SessionAssetCache`` exposed as a
    public attribute."""
    cache = stubbed_provider.asset_cache
    assert isinstance(cache, SessionAssetCache)
    assert cache.tempdir.exists()
    assert cache.tempdir.is_dir()


def test_atexit_shutdown_cleans_cache(tmp_path, monkeypatch):
    """AC-007: directly invoking ``_atexit_shutdown`` (without waiting for
    real ``atexit`` firing) removes the session tempdir."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'test-vault')

    fake_api = Mock()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    fake_api.get_session_briefing = AsyncMock(return_value='')
    fake_api.ingest = AsyncMock()
    fake_api.kv_put = AsyncMock()

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('session-atexit', hermes_home=str(tmp_path), platform='cli')

    cache = provider.asset_cache
    assert cache is not None
    cache_tempdir = cache.tempdir
    assert cache_tempdir.exists()

    # Drop a marker file so we can prove the directory was wiped.
    marker = cache_tempdir / 'marker.bin'
    marker.write_bytes(b'before shutdown')
    assert marker.exists()

    provider._atexit_shutdown()

    assert not cache_tempdir.exists(), f'atexit shutdown left tempdir behind: {cache_tempdir}'
    # Cache reference is dropped so a re-entry mints a fresh one.
    assert provider.asset_cache is None
