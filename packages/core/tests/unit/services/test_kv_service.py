"""Unit tests for KVService CRUD operations."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.kv import KVService, _pattern_to_prefix, _validate_namespace


@pytest.fixture
def kv_service(mock_metastore, mock_filestore, mock_config):
    """KVService with mocked dependencies."""
    return KVService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )


# ---------------------------------------------------------------------------
# namespace validation
# ---------------------------------------------------------------------------


def test_validate_namespace_global():
    """global: prefix should be accepted."""
    _validate_namespace('global:test:key')


def test_validate_namespace_user():
    """user: prefix should be accepted."""
    _validate_namespace('user:work:employer')


def test_validate_namespace_project():
    """project: prefix should be accepted."""
    _validate_namespace('project:github.com/user/repo:vault')


def test_validate_namespace_app():
    """app: prefix should be accepted."""
    _validate_namespace('app:claude-code:theme')


def test_validate_namespace_rejects_bare_key():
    """Keys without a valid namespace prefix should be rejected."""
    with pytest.raises(ValueError, match='namespace prefix'):
        _validate_namespace('tool:python:pkg_mgr')


def test_validate_namespace_rejects_agents_prefix():
    """Old agents: prefix should be rejected."""
    with pytest.raises(ValueError, match='namespace prefix'):
        _validate_namespace('agents:some:key')


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_returns_entry(kv_service, mock_session):
    """put() should upsert and return the KVEntry."""
    from memex_core.memory.sql_models import KVEntry

    entry_id = uuid4()
    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.id = entry_id
    mock_entry.key = 'global:tool:python:pkg_mgr'
    mock_entry.value = 'uv'

    # session.exec returns a result with .first() for RETURNING
    mock_row = MagicMock()
    mock_row.id = entry_id
    mock_result = MagicMock()
    mock_result.first.return_value = mock_row
    mock_session.exec.return_value = mock_result

    # session.get returns the full ORM object
    mock_session.get.return_value = mock_entry

    result = await kv_service.put(key='global:tool:python:pkg_mgr', value='uv')

    assert result.key == 'global:tool:python:pkg_mgr'
    assert result.value == 'uv'
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_put_rejects_unnamespaced_key(kv_service):
    """put() should reject keys without a valid namespace prefix."""
    with pytest.raises(ValueError, match='namespace prefix'):
        await kv_service.put(key='pref:theme', value='dark')


@pytest.mark.asyncio
async def test_put_raises_on_no_row(kv_service, mock_session):
    """put() should raise RuntimeError if upsert returns no row."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    with pytest.raises(RuntimeError, match='Upsert returned no row'):
        await kv_service.put(key='global:k', value='v')


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry(kv_service, mock_session):
    """get() should look up entries by key."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'global:tool:python:pkg_mgr'
    mock_entry.value = 'uv'
    mock_entry.expires_at = None

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='global:tool:python:pkg_mgr')
    assert result is not None
    assert result.value == 'uv'


@pytest.mark.asyncio
async def test_get_not_found(kv_service, mock_session):
    """get() returns None when key not found."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='global:nonexistent')
    assert result is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_existing(kv_service, mock_session):
    """delete() returns True and commits when entry exists."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result
    mock_session.delete = AsyncMock()

    result = await kv_service.delete(key='global:pref:theme')
    assert result is True
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_delete_not_found(kv_service, mock_session):
    """delete() returns False when key not found."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    result = await kv_service.delete(key='global:nonexistent')
    assert result is False


# ---------------------------------------------------------------------------
# list_entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all(kv_service, mock_session):
    """list_entries() without namespaces returns all entries."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry) for _ in range(3)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.list_entries()
    assert len(result) == 3


@pytest.mark.asyncio
async def test_list_with_namespace_filter(kv_service, mock_session):
    """list_entries() with namespaces filters by prefix."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.list_entries(namespaces=['global', 'user'])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_entries(kv_service, mock_session):
    """search() returns entries ordered by embedding distance."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry) for _ in range(2)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.search(query_embedding=[0.1] * 384, limit=5)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_search_with_namespace_filter(kv_service, mock_session):
    """search() with namespaces filters by prefix."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.search(query_embedding=[0.1] * 384, namespaces=['global'], limit=5)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_search_empty_results(kv_service, mock_session):
    """search() returns empty list when no matching entries."""
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.exec.return_value = mock_result

    result = await kv_service.search(query_embedding=[0.1] * 384)
    assert result == []


# ---------------------------------------------------------------------------
# _pattern_to_prefix
# ---------------------------------------------------------------------------


def test_pattern_to_prefix_trailing_wildcard():
    """Trailing wildcard should be stripped to produce a prefix."""
    assert _pattern_to_prefix('global:*') == 'global:'


def test_pattern_to_prefix_star_only():
    """A bare '*' pattern should return None (match all)."""
    assert _pattern_to_prefix('*') is None


def test_pattern_to_prefix_middle_wildcard():
    """Wildcards not at the end should raise ValueError."""
    with pytest.raises(ValueError, match='trailing wildcards'):
        _pattern_to_prefix('a:*:b')


def test_pattern_to_prefix_no_wildcard():
    """Pattern without wildcard should be returned as-is (exact prefix)."""
    assert _pattern_to_prefix('no-wildcard') == 'no-wildcard'


def test_pattern_to_prefix_deep_path():
    """Multi-segment pattern should strip only the trailing *."""
    assert _pattern_to_prefix('global:preferences:*') == 'global:preferences:'


# ---------------------------------------------------------------------------
# list_entries with pattern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_pattern(kv_service, mock_session):
    """list_entries(pattern=...) should resolve pattern to key_prefix."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.list_entries(pattern='global:*')
    assert len(result) == 1


# ---------------------------------------------------------------------------
# TTL support
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_with_ttl_sets_expires_at(kv_service, mock_session):
    """put() with ttl_seconds should compute an expires_at value."""
    from memex_core.memory.sql_models import KVEntry

    entry_id = uuid4()
    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.id = entry_id
    mock_entry.key = 'global:test:ttl'
    mock_entry.value = 'ephemeral'
    mock_entry.expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)

    mock_row = MagicMock()
    mock_row.id = entry_id
    mock_result = MagicMock()
    mock_result.first.return_value = mock_row
    mock_session.exec.return_value = mock_result
    mock_session.get.return_value = mock_entry

    result = await kv_service.put(key='global:test:ttl', value='ephemeral', ttl_seconds=3600)

    assert result.expires_at is not None
    # Should be roughly 1 hour in the future
    delta = result.expires_at - datetime.now(timezone.utc)
    assert 3500 < delta.total_seconds() < 3700


@pytest.mark.asyncio
async def test_put_without_ttl_sets_null(kv_service, mock_session):
    """put() without ttl_seconds should set expires_at=None."""
    from memex_core.memory.sql_models import KVEntry

    entry_id = uuid4()
    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.id = entry_id
    mock_entry.key = 'global:test:no-ttl'
    mock_entry.value = 'permanent'
    mock_entry.expires_at = None

    mock_row = MagicMock()
    mock_row.id = entry_id
    mock_result = MagicMock()
    mock_result.first.return_value = mock_row
    mock_session.exec.return_value = mock_result
    mock_session.get.return_value = mock_entry

    result = await kv_service.put(key='global:test:no-ttl', value='permanent')
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_put_zero_ttl_raises(kv_service):
    """put() with ttl_seconds=0 should raise ValueError."""
    with pytest.raises(ValueError, match='ttl_seconds must be a positive integer'):
        await kv_service.put(key='global:k', value='v', ttl_seconds=0)


@pytest.mark.asyncio
async def test_put_negative_ttl_raises(kv_service):
    """put() with negative ttl_seconds should raise ValueError."""
    with pytest.raises(ValueError, match='ttl_seconds must be a positive integer'):
        await kv_service.put(key='global:k', value='v', ttl_seconds=-10)


@pytest.mark.asyncio
async def test_get_deletes_expired_entry(kv_service, mock_session):
    """get() should delete an expired entry and return None."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'global:expired'
    mock_entry.value = 'old'
    mock_entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result
    mock_session.delete = AsyncMock()

    result = await kv_service.get(key='global:expired')
    assert result is None
    mock_session.delete.assert_called_once_with(mock_entry)
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_get_returns_non_expired_entry(kv_service, mock_session):
    """get() should return an entry that has not yet expired."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'global:fresh'
    mock_entry.value = 'still valid'
    mock_entry.expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='global:fresh')
    assert result is not None
    assert result.value == 'still valid'


@pytest.mark.asyncio
async def test_get_returns_entry_with_null_expires(kv_service, mock_session):
    """get() should return entries with expires_at=None (never expires)."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'global:permanent'
    mock_entry.value = 'forever'
    mock_entry.expires_at = None

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='global:permanent')
    assert result is not None
    assert result.value == 'forever'
