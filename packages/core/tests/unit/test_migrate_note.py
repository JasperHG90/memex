"""Unit tests for NoteService.migrate_note."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.exceptions import NoteNotFoundError, VaultNotFoundError
from memex_core.services.notes import NoteService


@pytest.fixture
def note_service():
    """NoteService with mocked dependencies."""
    metastore = MagicMock()
    filestore = MagicMock()
    filestore.exists = AsyncMock(return_value=False)
    filestore.move_file = AsyncMock()
    filestore.begin_staging = MagicMock()
    filestore.commit_staging = AsyncMock()
    filestore.rollback_staging = AsyncMock()
    config = MagicMock()
    vaults = MagicMock()
    return NoteService(metastore=metastore, filestore=filestore, config=config, vaults=vaults)


def _make_txn_session(get_side_effects: list | None = None, exec_results: list | None = None):
    """Build a mock AsyncSession for use inside AsyncTransaction.

    Args:
        get_side_effects: list of return values for successive session.get() calls.
        exec_results: list of mock results for successive session.exec() calls.
            Each should be a MagicMock with .all() / .first() configured.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.delete = MagicMock()

    if get_side_effects is not None:
        session.get = AsyncMock(side_effect=get_side_effects)
    else:
        session.get = AsyncMock(return_value=None)

    if exec_results is not None:
        session.exec = AsyncMock(side_effect=exec_results)
    else:
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.first.return_value = None
        session.exec = AsyncMock(return_value=mock_result)

    return session


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_note_not_found(note_service):
    """Raises NoteNotFoundError when the note doesn't exist."""
    note_id = uuid4()
    target_vault_id = uuid4()

    session = _make_txn_session(get_side_effects=[None])

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(NoteNotFoundError, match=str(note_id)):
            await note_service.migrate_note(note_id, target_vault_id)


@pytest.mark.asyncio
async def test_migrate_note_same_vault_raises(note_service):
    """Raises ValueError when source and target vault are identical."""
    note_id = uuid4()
    vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = vault_id

    session = _make_txn_session(get_side_effects=[mock_note])

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(ValueError, match='same'):
            await note_service.migrate_note(note_id, vault_id)


@pytest.mark.asyncio
async def test_migrate_note_target_vault_not_found(note_service):
    """Raises VaultNotFoundError when the target vault doesn't exist."""
    note_id = uuid4()
    source_vault_id = uuid4()
    target_vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = source_vault_id

    # session.get: 1st=note, 2nd=target vault (None), 3rd=source vault
    session = _make_txn_session(get_side_effects=[mock_note, None])

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(VaultNotFoundError, match=str(target_vault_id)):
            await note_service.migrate_note(note_id, target_vault_id)


# ---------------------------------------------------------------------------
# Path rewriting logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filestore_path_rewritten(note_service):
    """filestore_path and assets are rewritten to the target vault name."""
    note_id = uuid4()
    source_vault_id = uuid4()
    target_vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = source_vault_id
    mock_note.filestore_path = f'assets/source_vault/{note_id}'
    mock_note.assets = [
        f'assets/source_vault/{note_id}/image.png',
        f'assets/source_vault/{note_id}/doc.pdf',
    ]

    mock_target_vault = MagicMock()
    mock_target_vault.name = 'target_vault'

    mock_source_vault = MagicMock()
    mock_source_vault.name = 'source_vault'

    # session.get calls: note, target_vault, source_vault
    session = _make_txn_session(
        get_side_effects=[mock_note, mock_target_vault, mock_source_vault],
    )

    # session.exec for unit_ids query (empty)
    empty_result = MagicMock()
    empty_result.all.return_value = []
    session.exec = AsyncMock(return_value=empty_result)

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await note_service.migrate_note(note_id, target_vault_id)

    assert mock_note.filestore_path == f'assets/target_vault/{note_id}'
    assert mock_note.assets == [
        f'assets/target_vault/{note_id}/image.png',
        f'assets/target_vault/{note_id}/doc.pdf',
    ]
    assert result['status'] == 'success'
    assert result['source_vault_id'] == str(source_vault_id)
    assert result['target_vault_id'] == str(target_vault_id)


@pytest.mark.asyncio
async def test_null_filestore_path_not_rewritten(note_service):
    """When filestore_path is None, it stays None (no crash)."""
    note_id = uuid4()
    source_vault_id = uuid4()
    target_vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = source_vault_id
    mock_note.filestore_path = None
    mock_note.assets = []

    mock_target_vault = MagicMock()
    mock_target_vault.name = 'target_vault'

    mock_source_vault = MagicMock()
    mock_source_vault.name = 'source_vault'

    session = _make_txn_session(
        get_side_effects=[mock_note, mock_target_vault, mock_source_vault],
    )

    empty_result = MagicMock()
    empty_result.all.return_value = []
    session.exec = AsyncMock(return_value=empty_result)

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await note_service.migrate_note(note_id, target_vault_id)

    assert mock_note.filestore_path is None
    assert mock_note.assets == []
    assert result['entities_affected'] == 0


# ---------------------------------------------------------------------------
# Filestore move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filestore_move_called_when_dir_exists(note_service):
    """After transaction, move_file is called when the old assets dir exists."""
    note_id = uuid4()
    source_vault_id = uuid4()
    target_vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = source_vault_id
    mock_note.filestore_path = f'assets/src/{note_id}'
    mock_note.assets = []

    mock_target = MagicMock()
    mock_target.name = 'dst'

    mock_source = MagicMock()
    mock_source.name = 'src'

    session = _make_txn_session(
        get_side_effects=[mock_note, mock_target, mock_source],
    )

    empty_result = MagicMock()
    empty_result.all.return_value = []
    session.exec = AsyncMock(return_value=empty_result)

    # Make filestore.exists return True
    note_service.filestore.exists = AsyncMock(return_value=True)

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        await note_service.migrate_note(note_id, target_vault_id)

    note_service.filestore.move_file.assert_awaited_once_with(
        f'assets/src/{note_id}', f'assets/dst/{note_id}'
    )


@pytest.mark.asyncio
async def test_filestore_move_skipped_when_no_dir(note_service):
    """Filestore move is skipped when old assets dir doesn't exist."""
    note_id = uuid4()
    source_vault_id = uuid4()
    target_vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.vault_id = source_vault_id
    mock_note.filestore_path = None
    mock_note.assets = []

    mock_target = MagicMock()
    mock_target.name = 'dst'

    mock_source = MagicMock()
    mock_source.name = 'src'

    session = _make_txn_session(
        get_side_effects=[mock_note, mock_target, mock_source],
    )

    empty_result = MagicMock()
    empty_result.all.return_value = []
    session.exec = AsyncMock(return_value=empty_result)

    note_service.filestore.exists = AsyncMock(return_value=False)

    with patch('memex_core.services.notes.AsyncTransaction') as mock_txn:
        mock_txn.return_value.__aenter__ = AsyncMock(return_value=MagicMock(db_session=session))
        mock_txn.return_value.__aexit__ = AsyncMock(return_value=False)

        await note_service.migrate_note(note_id, target_vault_id)

    note_service.filestore.move_file.assert_not_awaited()
