"""Unit tests for MemexAPI.update_user_notes (Feature C: AC-C01 through AC-C04)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.api import inject_user_notes


# ---------------------------------------------------------------------------
# inject_user_notes helper tests
# ---------------------------------------------------------------------------


def test_inject_user_notes_into_existing_frontmatter():
    content = '---\ntitle: Test\n---\nBody text.'
    result = inject_user_notes(content, 'My annotation')
    assert 'user_notes: |' in result
    assert 'My annotation' in result
    assert 'title: Test' in result
    assert 'Body text.' in result


def test_inject_user_notes_creates_frontmatter():
    content = 'Body text without frontmatter.'
    result = inject_user_notes(content, 'My annotation')
    assert result.startswith('---\n')
    assert 'user_notes: |' in result
    assert 'My annotation' in result


def test_inject_user_notes_replaces_existing():
    content = '---\ntitle: Test\nuser_notes: |\n  Old notes\n---\nBody.'
    result = inject_user_notes(content, 'New notes')
    assert 'New notes' in result
    assert 'Old notes' not in result


def test_inject_user_notes_noop_for_none():
    content = '---\ntitle: Test\n---\nBody.'
    result = inject_user_notes(content, None)
    assert result == content


def test_inject_user_notes_noop_for_empty():
    content = '---\ntitle: Test\n---\nBody.'
    result = inject_user_notes(content, '   ')
    assert result == content


# ---------------------------------------------------------------------------
# ExtractionEngine.extract_user_notes unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_user_notes_returns_empty_for_blank():
    """extract_user_notes should return empty results for blank input."""
    from memex_core.memory.extraction.engine import ExtractionEngine

    engine = MagicMock(spec=ExtractionEngine)
    engine.extract_user_notes = ExtractionEngine.extract_user_notes.__get__(engine)
    engine.SECONDS_PER_FACT = 10

    session = AsyncMock()
    result = await engine.extract_user_notes(session, '', str(uuid4()), uuid4())
    assert result == ([], set())


@pytest.mark.asyncio
async def test_extract_user_notes_returns_empty_for_whitespace():
    """extract_user_notes should return empty results for whitespace-only input."""
    from memex_core.memory.extraction.engine import ExtractionEngine

    engine = MagicMock(spec=ExtractionEngine)
    engine.extract_user_notes = ExtractionEngine.extract_user_notes.__get__(engine)
    engine.SECONDS_PER_FACT = 10

    session = AsyncMock()
    result = await engine.extract_user_notes(session, '   \n  ', str(uuid4()), uuid4())
    assert result == ([], set())


# ---------------------------------------------------------------------------
# AC-C01: update_user_notes strips old, injects new, updates content_hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_user_notes_strips_and_reinjects():
    """AC-C01: update_user_notes strips old user_notes, injects new, updates content_hash."""
    note_id = uuid4()
    vault_id = uuid4()

    # Create a mock note
    mock_note = MagicMock()
    mock_note.original_text = '---\ntitle: Test\nuser_notes: |\n  Old annotation\n---\nBody text.'
    mock_note.vault_id = vault_id
    mock_note.created_at = None
    mock_note.content_hash = 'old-hash'

    # Mock session
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_note)
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.commit = AsyncMock()

    # Mock metastore to return session
    mock_metastore = MagicMock()
    mock_metastore.session = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    # Mock extraction engine
    mock_extraction = AsyncMock()
    mock_extraction.extract_user_notes = AsyncMock(return_value=(['unit-1', 'unit-2'], set()))

    # Build a minimal MemexAPI mock
    api = MagicMock()
    api.metastore = mock_metastore
    api._extraction = mock_extraction
    api.queue_service = None

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    result = await api.update_user_notes(note_id, 'New annotation')

    assert result['note_id'] == str(note_id)
    assert result['units_deleted'] == 0  # no old units existed
    assert result['units_created'] == 2

    # Verify note was updated
    assert 'New annotation' in mock_note.original_text
    assert 'Old annotation' not in mock_note.original_text
    assert mock_note.content_hash != 'old-hash'


@pytest.mark.asyncio
async def test_update_user_notes_null_deletes_only():
    """AC-C07 (partial): Setting user_notes to None strips frontmatter and deletes units."""
    note_id = uuid4()
    vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.original_text = '---\ntitle: Test\nuser_notes: |\n  Old notes\n---\nBody.'
    mock_note.vault_id = vault_id
    mock_note.created_at = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_note)
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.commit = AsyncMock()

    mock_metastore = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    mock_extraction = AsyncMock()

    api = MagicMock()
    api.metastore = mock_metastore
    api._extraction = mock_extraction
    api.queue_service = None

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    result = await api.update_user_notes(note_id, None)

    assert result['units_deleted'] == 0
    assert result['units_created'] == 0
    # Extraction should NOT have been called
    mock_extraction.extract_user_notes.assert_not_called()
    # user_notes should be stripped from text
    assert 'user_notes' not in mock_note.original_text
    assert 'Old notes' not in mock_note.original_text


@pytest.mark.asyncio
async def test_update_user_notes_not_found():
    """update_user_notes raises ValueError for non-existent note."""
    note_id = uuid4()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    mock_metastore = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    api = MagicMock()
    api.metastore = mock_metastore

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    with pytest.raises(ValueError, match='not found'):
        await api.update_user_notes(note_id, 'test')


# ---------------------------------------------------------------------------
# Helper: build MemexAPI mock with old units pre-populated
# ---------------------------------------------------------------------------


def _build_api_with_old_units(
    note_id,
    vault_id,
    old_unit_ids,
    old_entity_ids,
    new_unit_ids=None,
    new_entity_ids=None,
):
    """Return (api, mock_session, mock_extraction, mock_enqueue) with old units populated."""
    mock_note = MagicMock()
    mock_note.original_text = '---\ntitle: Test\nuser_notes: |\n  Old annotation\n---\nBody text.'
    mock_note.vault_id = vault_id
    mock_note.created_at = None
    mock_note.content_hash = 'old-hash'

    # Build execute side_effect: first call returns old unit IDs,
    # second returns old entity IDs, third+fourth are deletes (no return needed)
    old_unit_rows = [(uid,) for uid in old_unit_ids]
    old_entity_rows = [(eid,) for eid in old_entity_ids]

    execute_results = []
    # Call 1: SELECT MemoryUnit.id WHERE note_id AND context='user_notes'
    r1 = MagicMock()
    r1.all.return_value = old_unit_rows
    execute_results.append(r1)
    # Call 2: SELECT UnitEntity.entity_id WHERE unit_id IN (...)
    if old_unit_ids:
        r2 = MagicMock()
        r2.all.return_value = old_entity_rows
        execute_results.append(r2)
    # Call 3+4: DELETE UnitEntity, DELETE MemoryUnit (if old units exist)
    if old_unit_ids:
        execute_results.append(MagicMock())  # DELETE UnitEntity
        execute_results.append(MagicMock())  # DELETE MemoryUnit

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_note)
    mock_session.execute = AsyncMock(side_effect=execute_results)
    mock_session.commit = AsyncMock()

    mock_metastore = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    mock_extraction = AsyncMock()
    mock_extraction.extract_user_notes = AsyncMock(
        return_value=(new_unit_ids or [], new_entity_ids or set())
    )

    mock_queue_service = AsyncMock()

    api = MagicMock()
    api.metastore = mock_metastore
    api._extraction = mock_extraction
    api.queue_service = mock_queue_service

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    return api, mock_session, mock_extraction, mock_queue_service


# ---------------------------------------------------------------------------
# AC-C02: Old MemoryUnits with context='user_notes' are deleted on update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_user_notes_deletes_old_units():
    """AC-C02: Old MemoryUnits with context='user_notes' are deleted on update."""
    note_id = uuid4()
    vault_id = uuid4()
    old_uid_1 = uuid4()
    old_uid_2 = uuid4()
    old_entity_id = uuid4()

    api, mock_session, _, _ = _build_api_with_old_units(
        note_id=note_id,
        vault_id=vault_id,
        old_unit_ids=[old_uid_1, old_uid_2],
        old_entity_ids=[old_entity_id],
        new_unit_ids=['new-1'],
        new_entity_ids=set(),
    )

    result = await api.update_user_notes(note_id, 'Updated annotation')

    assert result['units_deleted'] == 2

    # Verify session.execute was called at least 4 times:
    # 1) SELECT old unit IDs, 2) SELECT old entity IDs,
    # 3) DELETE UnitEntity, 4) DELETE MemoryUnit
    assert mock_session.execute.call_count >= 4

    # Inspect the DELETE calls (calls 3 and 4, 0-indexed as 2 and 3)
    delete_calls = mock_session.execute.call_args_list
    # Call index 2: DELETE FROM unit_entities WHERE unit_id IN (...)
    delete_unit_entity_stmt = delete_calls[2].args[0]
    # Call index 3: DELETE FROM memory_units WHERE id IN (...)
    delete_memory_unit_stmt = delete_calls[3].args[0]

    # Verify the DELETE statements target the right tables by checking
    # the compiled SQL string contains the expected table names
    from sqlalchemy.dialects import postgresql

    ue_sql = str(delete_unit_entity_stmt.compile(dialect=postgresql.dialect()))
    mu_sql = str(delete_memory_unit_stmt.compile(dialect=postgresql.dialect()))
    assert 'unitentity' in ue_sql.lower() or 'unit_entit' in ue_sql.lower()
    assert 'memoryunit' in mu_sql.lower() or 'memory_unit' in mu_sql.lower()


# ---------------------------------------------------------------------------
# AC-C03: extract_user_notes is called with the new user_notes text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_user_notes_calls_extract_with_correct_args():
    """AC-C03: extract_user_notes is called with the new text and correct note/vault IDs."""
    note_id = uuid4()
    vault_id = uuid4()
    old_uid = uuid4()
    new_uid_1 = uuid4()
    new_uid_2 = uuid4()

    api, mock_session, mock_extraction, _ = _build_api_with_old_units(
        note_id=note_id,
        vault_id=vault_id,
        old_unit_ids=[old_uid],
        old_entity_ids=[],
        new_unit_ids=[new_uid_1, new_uid_2],
        new_entity_ids={uuid4()},
    )

    result = await api.update_user_notes(note_id, 'Brand new annotation')

    assert result['units_created'] == 2

    # Verify extract_user_notes was called exactly once with the right arguments
    mock_extraction.extract_user_notes.assert_called_once()
    call_kwargs = mock_extraction.extract_user_notes.call_args
    # Positional or keyword: session, user_notes_text, note_id, vault_id
    assert call_kwargs.kwargs.get('user_notes_text') == 'Brand new annotation' or (
        len(call_kwargs.args) >= 2 and call_kwargs.args[1] == 'Brand new annotation'
    )
    assert call_kwargs.kwargs.get('note_id') == str(note_id) or (
        len(call_kwargs.args) >= 3 and call_kwargs.args[2] == str(note_id)
    )
    assert call_kwargs.kwargs.get('vault_id') == vault_id or (
        len(call_kwargs.args) >= 4 and call_kwargs.args[3] == vault_id
    )


# ---------------------------------------------------------------------------
# AC-C04: Affected entity IDs (old + new) are enqueued for reflection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_user_notes_enqueues_all_entities_for_reflection():
    """AC-C04: Entity IDs from both deleted and newly created units are enqueued."""
    note_id = uuid4()
    vault_id = uuid4()
    old_uid = uuid4()
    old_entity_1 = uuid4()
    old_entity_2 = uuid4()
    new_entity_1 = uuid4()

    api, mock_session, _, mock_queue_service = _build_api_with_old_units(
        note_id=note_id,
        vault_id=vault_id,
        old_unit_ids=[old_uid],
        old_entity_ids=[old_entity_1, old_entity_2],
        new_unit_ids=['new-1'],
        new_entity_ids={new_entity_1},
    )

    with patch(
        'memex_core.memory.extraction.pipeline.tracking.enqueue_for_reflection',
        new_callable=AsyncMock,
    ) as mock_enqueue:
        result = await api.update_user_notes(note_id, 'Updated annotation')

        assert result['units_deleted'] == 1

        # enqueue_for_reflection must be called with the union of old + new entity IDs
        mock_enqueue.assert_called_once()
        enqueue_args = mock_enqueue.call_args
        # args: (session, all_entity_ids, vault_id, queue_service)
        passed_entity_ids = (
            enqueue_args.args[1]
            if len(enqueue_args.args) > 1
            else enqueue_args.kwargs.get('entity_ids', set())
        )
        passed_vault_id = (
            enqueue_args.args[2]
            if len(enqueue_args.args) > 2
            else enqueue_args.kwargs.get('vault_id')
        )
        passed_queue_svc = (
            enqueue_args.args[3]
            if len(enqueue_args.args) > 3
            else enqueue_args.kwargs.get('queue_service')
        )

        expected_entities = {old_entity_1, old_entity_2, new_entity_1}
        assert passed_entity_ids == expected_entities
        assert passed_vault_id == vault_id
        assert passed_queue_svc is mock_queue_service
