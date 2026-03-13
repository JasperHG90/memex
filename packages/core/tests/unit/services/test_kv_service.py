"""Unit tests for KVService CRUD operations."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.kv import KVService


@pytest.fixture
def kv_service(mock_metastore, mock_filestore, mock_config):
    """KVService with mocked dependencies."""
    return KVService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )


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
    mock_entry.key = 'tool:python:pkg_mgr'
    mock_entry.value = 'uv'
    mock_entry.vault_id = None

    # session.exec returns a result with .first() for RETURNING
    mock_row = MagicMock()
    mock_row.id = entry_id
    mock_result = MagicMock()
    mock_result.first.return_value = mock_row
    mock_session.exec.return_value = mock_result

    # session.get returns the full ORM object
    mock_session.get.return_value = mock_entry

    result = await kv_service.put(vault_id=None, key='tool:python:pkg_mgr', value='uv')

    assert result.key == 'tool:python:pkg_mgr'
    assert result.value == 'uv'
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_put_with_vault_id(kv_service, mock_session):
    """put() should accept a vault_id for scoped entries."""
    from memex_core.memory.sql_models import KVEntry

    vault_id = uuid4()
    entry_id = uuid4()

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.id = entry_id
    mock_entry.vault_id = vault_id

    mock_row = MagicMock()
    mock_row.id = entry_id
    mock_result = MagicMock()
    mock_result.first.return_value = mock_row
    mock_session.exec.return_value = mock_result
    mock_session.get.return_value = mock_entry

    result = await kv_service.put(vault_id=vault_id, key='pref:theme', value='dark')

    assert result.vault_id == vault_id


@pytest.mark.asyncio
async def test_put_raises_on_no_row(kv_service, mock_session):
    """put() should raise RuntimeError if upsert returns no row."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    with pytest.raises(RuntimeError, match='Upsert returned no row'):
        await kv_service.put(vault_id=None, key='k', value='v')


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_global_entry(kv_service, mock_session):
    """get() without vault_id should look up global entries."""
    from memex_core.memory.sql_models import KVEntry

    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'tool:python:pkg_mgr'
    mock_entry.value = 'uv'
    mock_entry.vault_id = None

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='tool:python:pkg_mgr')
    assert result is not None
    assert result.value == 'uv'


@pytest.mark.asyncio
async def test_get_vault_specific_fallback_to_global(kv_service, mock_session):
    """get() with vault_id should check vault-specific first, then fall back to global."""
    from memex_core.memory.sql_models import KVEntry

    vault_id = uuid4()

    # First call (vault-specific) returns None, second call (global) returns entry
    mock_global_entry = MagicMock(spec=KVEntry)
    mock_global_entry.key = 'pref:theme'
    mock_global_entry.value = 'dark'
    mock_global_entry.vault_id = None

    mock_result_empty = MagicMock()
    mock_result_empty.first.return_value = None
    mock_result_global = MagicMock()
    mock_result_global.first.return_value = mock_global_entry

    mock_session.exec.side_effect = [mock_result_empty, mock_result_global]

    result = await kv_service.get(key='pref:theme', vault_id=vault_id)
    assert result is not None
    assert result.value == 'dark'
    assert mock_session.exec.call_count == 2


@pytest.mark.asyncio
async def test_get_vault_specific_found(kv_service, mock_session):
    """get() with vault_id returns vault-specific without checking global."""
    from memex_core.memory.sql_models import KVEntry

    vault_id = uuid4()
    mock_entry = MagicMock(spec=KVEntry)
    mock_entry.key = 'pref:theme'
    mock_entry.value = 'light'
    mock_entry.vault_id = vault_id

    mock_result = MagicMock()
    mock_result.first.return_value = mock_entry
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='pref:theme', vault_id=vault_id)
    assert result is not None
    assert result.value == 'light'
    # Should only call exec once (vault-specific found)
    assert mock_session.exec.call_count == 1


@pytest.mark.asyncio
async def test_get_not_found(kv_service, mock_session):
    """get() returns None when key not found."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    result = await kv_service.get(key='nonexistent')
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

    result = await kv_service.delete(key='pref:theme')
    assert result is True
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_delete_not_found(kv_service, mock_session):
    """delete() returns False when key not found."""
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    result = await kv_service.delete(key='nonexistent')
    assert result is False


# ---------------------------------------------------------------------------
# list_entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_global_only(kv_service, mock_session):
    """list_entries() without vault_id returns global entries only."""
    from memex_core.memory.sql_models import KVEntry

    entries = [MagicMock(spec=KVEntry, vault_id=None) for _ in range(3)]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.list_entries()
    assert len(result) == 3


@pytest.mark.asyncio
async def test_list_with_vault_includes_global(kv_service, mock_session):
    """list_entries() with vault_id returns both vault-scoped and global entries."""
    from memex_core.memory.sql_models import KVEntry

    vault_id = uuid4()
    entries = [
        MagicMock(spec=KVEntry, vault_id=vault_id),
        MagicMock(spec=KVEntry, vault_id=None),
    ]
    mock_result = MagicMock()
    mock_result.all.return_value = entries
    mock_session.exec.return_value = mock_result

    result = await kv_service.list_entries(vault_id=vault_id)
    assert len(result) == 2


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
async def test_search_empty_results(kv_service, mock_session):
    """search() returns empty list when no matching entries."""
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.exec.return_value = mock_result

    result = await kv_service.search(query_embedding=[0.1] * 384)
    assert result == []
