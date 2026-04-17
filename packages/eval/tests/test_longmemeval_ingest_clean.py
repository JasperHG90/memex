"""Tests for the ``--clean`` path of the LongMemEval ingest adapter.

These assert the concrete ``RemoteMemexAPI`` contract that
``_setup_vault(..., clean=True)`` relies on: it must look up existing notes
via ``list_notes`` and delete each by id via ``delete_note``. If either
method is renamed upstream (or the adapter is changed to call a name that
does not exist), these tests fail loudly instead of at runtime against a
live server.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_common.client import RemoteMemexAPI
from memex_eval.external.longmemeval_ingest import _setup_vault


def test_remote_memex_api_exposes_delete_note() -> None:
    """Guard against the adapter calling a nonexistent client method."""
    assert hasattr(RemoteMemexAPI, 'delete_note')
    assert hasattr(RemoteMemexAPI, 'list_notes')
    assert hasattr(RemoteMemexAPI, 'list_vaults')


@pytest.mark.asyncio
async def test_setup_vault_clean_deletes_existing_notes_by_id() -> None:
    """With clean=True, existing notes in a matched vault must be deleted
    via ``delete_note(note.id)`` — not via a delete-by-title or any other
    method name.
    """
    vault_id = uuid4()
    note_a_id = uuid4()
    note_b_id = uuid4()

    vault = SimpleNamespace(id=vault_id, name='longmemeval_oracle_test-run')
    note_a = SimpleNamespace(id=note_a_id)
    note_b = SimpleNamespace(id=note_b_id)

    api = AsyncMock(spec=RemoteMemexAPI)
    api.list_vaults.return_value = [vault]
    api.list_notes.return_value = [note_a, note_b]
    api.delete_note.return_value = True

    result = await _setup_vault(api, 'longmemeval_oracle_test-run', clean=True)

    assert result == vault_id
    api.list_notes.assert_awaited_once()
    # Each existing note must be deleted, by id.
    assert api.delete_note.await_count == 2
    called_ids = {call.args[0] for call in api.delete_note.await_args_list}
    assert called_ids == {note_a_id, note_b_id}


@pytest.mark.asyncio
async def test_setup_vault_no_clean_does_not_delete() -> None:
    vault_id = uuid4()
    vault = SimpleNamespace(id=vault_id, name='longmemeval_oracle_test-run')

    api = AsyncMock(spec=RemoteMemexAPI)
    api.list_vaults.return_value = [vault]

    result = await _setup_vault(api, 'longmemeval_oracle_test-run', clean=False)

    assert result == vault_id
    api.delete_note.assert_not_awaited()
    api.list_notes.assert_not_awaited()
