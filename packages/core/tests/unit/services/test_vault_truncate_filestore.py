"""Unit tests for vault truncate filestore cleanup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.vaults import VaultService


@pytest.fixture
def vault_service(mock_metastore, mock_filestore, mock_config):
    return VaultService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )


def _make_exec_side_effect(note_rows):
    """Build an async side_effect for session.exec() in truncate_vault.

    Call order:
      0 — SELECT note filestore paths + assets
      1 — SELECT orphan entity IDs
      2..6 — 5 DELETE statements (vault-scoped tables)
      7 — DELETE orphan entities (if any)
    """
    call_idx = 0

    async def _exec(stmt, *a, **kw):
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        result = MagicMock()
        if idx == 0:
            result.all.return_value = note_rows
        elif idx == 1:
            result.all.return_value = []
        else:
            result.rowcount = 0
        return result

    return _exec


@pytest.mark.asyncio
async def test_truncate_vault_deletes_filestore_paths(vault_service, mock_session, mock_filestore):
    """truncate_vault should delete filestore_path and assets from the filestore."""
    vault_id = uuid4()
    note_rows = [
        ('notes/v/abc/content.md', ['assets/v/abc/img1.png', 'assets/v/abc/img2.png']),
        ('notes/v/def/content.md', None),
        (None, ['assets/v/ghi/doc.pdf']),
    ]
    mock_session.exec = AsyncMock(side_effect=_make_exec_side_effect(note_rows))
    mock_filestore.delete = AsyncMock()

    await vault_service.truncate_vault(vault_id)

    expected_paths = [
        'notes/v/abc/content.md',
        'assets/v/abc/img1.png',
        'assets/v/abc/img2.png',
        'notes/v/def/content.md',
        'assets/v/ghi/doc.pdf',
    ]
    actual_paths = [call.args[0] for call in mock_filestore.delete.call_args_list]
    assert actual_paths == expected_paths
    for call in mock_filestore.delete.call_args_list:
        assert call.kwargs.get('recursive') is True


@pytest.mark.asyncio
async def test_truncate_vault_no_notes_skips_filestore(vault_service, mock_session, mock_filestore):
    """truncate_vault with no notes should not call filestore.delete."""
    vault_id = uuid4()
    mock_session.exec = AsyncMock(side_effect=_make_exec_side_effect([]))
    mock_filestore.delete = AsyncMock()

    await vault_service.truncate_vault(vault_id)

    mock_filestore.delete.assert_not_called()


@pytest.mark.asyncio
async def test_truncate_vault_filestore_error_does_not_raise(
    vault_service, mock_session, mock_filestore
):
    """Filestore errors during cleanup should be logged but not raised."""
    vault_id = uuid4()
    note_rows = [('notes/v/abc/content.md', None)]
    mock_session.exec = AsyncMock(side_effect=_make_exec_side_effect(note_rows))
    mock_filestore.delete = AsyncMock(side_effect=OSError('disk error'))

    # Should not raise
    result = await vault_service.truncate_vault(vault_id)
    assert isinstance(result, dict)
