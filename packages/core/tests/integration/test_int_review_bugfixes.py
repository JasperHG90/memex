"""Integration tests for post-review bugfixes (BUG-1, BUG-4, BUG-5).

Verifies that the fixes work against a real Postgres database:
- BUG-4: update_user_notes atomicity (delete old + persist new in one txn)
- BUG-5: update_summary session consolidation and FOR UPDATE locking
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select, text

from memex_core.memory.sql_models import (
    MemoryUnit,
    Note,
    UnitEntity,
    Entity,
    Vault,
    VaultSummary,
)
from memex_common.types import FactTypes
from memex_core.services.vault_summary_signatures import LLMTheme


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_vault(session, vault_id=None, name=None):
    vault_id = vault_id or uuid4()
    name = name or f'test_vault_{vault_id.hex[:8]}'
    await session.execute(pg_insert(Vault.__table__).values(id=vault_id, name=name))
    await session.commit()
    return vault_id


async def _create_note(session, vault_id, note_id=None, text_content='Test note', **kwargs):
    note_id = note_id or uuid4()
    await session.execute(
        pg_insert(Note.__table__).values(
            id=note_id,
            original_text=text_content,
            content_hash=f'hash-{note_id.hex[:8]}',
            vault_id=vault_id,
            **kwargs,
        )
    )
    await session.commit()
    return note_id


async def _create_memory_unit(session, note_id, vault_id, unit_text='Extracted fact', context=None):
    unit_id = uuid4()
    await session.execute(
        pg_insert(MemoryUnit.__table__).values(
            id=unit_id,
            note_id=note_id,
            text=unit_text,
            fact_type=FactTypes.WORLD,
            vault_id=vault_id,
            embedding=[0.1] * 384,
            event_date=datetime.now(timezone.utc),
            context=context,
        )
    )
    await session.commit()
    return unit_id


async def _create_entity(session, name='TestEntity'):
    entity_id = uuid4()
    now = datetime.now(timezone.utc)
    await session.execute(
        pg_insert(Entity.__table__).values(
            id=entity_id,
            canonical_name=name,
            first_seen=now,
            last_seen=now,
        )
    )
    await session.commit()
    return entity_id


async def _link_unit_entity(session, unit_id, entity_id):
    await session.execute(
        pg_insert(UnitEntity.__table__).values(
            unit_id=unit_id,
            entity_id=entity_id,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# BUG-4: update_user_notes atomicity
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_user_notes_atomic_delete_and_persist(metastore, session):
    """BUG-4: Old units are deleted and new units created in a single transaction.

    If persist_user_notes fails, the old units should NOT be deleted (rollback).
    """
    vault_id = await _create_vault(session)
    note_id = await _create_note(
        session,
        vault_id,
        text_content='---\ntitle: Test\nuser_notes: |\n  Old annotation\n---\nBody.',
    )

    # Create old user_notes memory units
    old_unit_1 = await _create_memory_unit(
        session, note_id, vault_id, 'Old fact 1', context='user_notes'
    )
    await _create_memory_unit(session, note_id, vault_id, 'Old fact 2', context='user_notes')
    old_entity = await _create_entity(session, 'OldEntity')
    await _link_unit_entity(session, old_unit_1, old_entity)

    # Verify old units exist
    result = await session.execute(
        select(MemoryUnit).where(
            col(MemoryUnit.note_id) == note_id,
            col(MemoryUnit.context) == 'user_notes',
        )
    )
    assert len(result.all()) == 2

    # Build MemexAPI with real metastore but mocked extraction
    from memex_core.api import MemexAPI

    mock_extraction = AsyncMock()
    # prepare_user_notes returns some processed facts
    mock_extraction.prepare_user_notes = AsyncMock(return_value=['fake-fact'])
    # persist_user_notes raises to simulate failure
    mock_extraction.persist_user_notes = AsyncMock(
        side_effect=RuntimeError('LLM extraction failed')
    )

    api = MagicMock(spec=MemexAPI)
    api.metastore = metastore
    api._extraction = mock_extraction
    api.queue_service = None
    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    with pytest.raises(RuntimeError, match='LLM extraction failed'):
        await api.update_user_notes(note_id, 'New annotation')

    # Old units should still exist because the transaction rolled back
    result = await session.execute(
        select(MemoryUnit).where(
            col(MemoryUnit.note_id) == note_id,
            col(MemoryUnit.context) == 'user_notes',
        )
    )
    remaining = result.all()
    assert len(remaining) == 2, f'Expected 2 old units to survive rollback, got {len(remaining)}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_user_notes_success_path(metastore, session):
    """BUG-4: On success, old units are deleted and new units are persisted atomically."""
    vault_id = await _create_vault(session)
    note_id = await _create_note(
        session,
        vault_id,
        text_content='---\ntitle: Test\nuser_notes: |\n  Old annotation\n---\nBody.',
    )

    # Create old user_notes memory units
    old_unit = await _create_memory_unit(
        session, note_id, vault_id, 'Old fact', context='user_notes'
    )

    from memex_core.api import MemexAPI

    new_unit_id = str(uuid4())
    mock_extraction = AsyncMock()
    mock_extraction.prepare_user_notes = AsyncMock(return_value=['fake-fact'])
    mock_extraction.persist_user_notes = AsyncMock(return_value=([new_unit_id], set()))

    api = MagicMock(spec=MemexAPI)
    api.metastore = metastore
    api._extraction = mock_extraction
    api.queue_service = None
    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    result = await api.update_user_notes(note_id, 'New annotation')

    assert result['units_deleted'] == 1
    assert result['units_created'] == 1

    # Old unit should be gone
    old = await session.execute(select(MemoryUnit).where(col(MemoryUnit.id) == old_unit))
    assert old.first() is None

    # Note text should be updated
    note = await session.get(Note, note_id)
    assert 'New annotation' in note.original_text
    assert 'Old annotation' not in note.original_text


# ---------------------------------------------------------------------------
# BUG-5: update_summary session consolidation + FOR UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_summary_consolidates_sessions(metastore, session):
    """BUG-5: update_summary reads summary + delta + count, then persists with FOR UPDATE."""
    from memex_common.config import VaultSummaryConfig
    from memex_core.services.vault_summary import VaultSummaryService

    vault_id = await _create_vault(session)

    # Create initial summary with an older updated_at
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summary = VaultSummary(vault_id=vault_id, narrative='Old summary', version=1)
    session.add(summary)
    await session.commit()
    # Force updated_at to the past so new notes appear as delta
    await session.execute(
        text('UPDATE vault_summaries SET updated_at = :ts WHERE vault_id = :vid'),
        {'ts': old_ts, 'vid': str(vault_id)},
    )
    await session.commit()

    # Create a note newer than the summary (with title so _fetch_note_metadata includes it)
    await _create_note(session, vault_id, text_content='New research note', title='Research Note')

    # Mock LLM
    mock_prediction = MagicMock()
    mock_prediction.updated_narrative = 'Updated summary with new research'
    mock_prediction.updated_themes = [
        LLMTheme(name='Research', description='Research topics', note_count=1, trend='growing')
    ]

    mock_lm = MagicMock(spec=['__call__'])
    svc = VaultSummaryService(
        metastore=metastore,
        lm=mock_lm,
        config=VaultSummaryConfig(),
    )

    with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
        mock_run.return_value = mock_prediction
        result = await svc.update_summary(vault_id)

    assert result.narrative == 'Updated summary with new research'
    assert result.version == 2
    assert len(result.patch_log) == 1
    assert result.patch_log[0]['action'] == 'update'

    # Verify persisted to DB
    async with metastore.session() as s:
        db_summary = (
            await s.execute(select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id))
        ).scalar_one()
        assert db_summary.narrative == 'Updated summary with new research'
        assert db_summary.version == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_summary_version_conflict_skips_write(metastore, session):
    """BUG-5: If version changes during LLM call, the write is skipped."""
    from memex_common.config import VaultSummaryConfig
    from memex_core.services.vault_summary import VaultSummaryService

    vault_id = await _create_vault(session)

    # Create initial summary at version 3 with old timestamp
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summary = VaultSummary(
        vault_id=vault_id, narrative='Version 3 summary', version=3, notes_incorporated=10
    )
    session.add(summary)
    await session.commit()
    await session.execute(
        text('UPDATE vault_summaries SET updated_at = :ts WHERE vault_id = :vid'),
        {'ts': old_ts, 'vid': str(vault_id)},
    )
    await session.commit()

    # Create a note newer than the summary (with title so _fetch_note_metadata includes it)
    await _create_note(session, vault_id, text_content='Trigger note', title='Trigger Note')

    mock_lm = MagicMock(spec=['__call__'])
    svc = VaultSummaryService(
        metastore=metastore,
        lm=mock_lm,
        config=VaultSummaryConfig(),
    )

    mock_prediction = MagicMock()
    mock_prediction.updated_narrative = 'Should NOT be persisted'
    mock_prediction.updated_themes = []

    async def _bump_version_during_llm(**kwargs):
        """Simulate a concurrent update bumping the version during the LLM call."""
        async with metastore.session() as s:
            row = (
                await s.execute(select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id))
            ).scalar_one()
            row.narrative = 'Concurrently updated'
            row.version = 5
            s.add(row)
            await s.commit()
        return mock_prediction

    with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
        mock_run.side_effect = _bump_version_during_llm
        result = await svc.update_summary(vault_id)

    # Should return the concurrently updated version, not our LLM result
    assert result.narrative == 'Concurrently updated'
    assert result.version == 5

    # DB should also reflect the concurrent version
    async with metastore.session() as s:
        db_summary = (
            await s.execute(select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id))
        ).scalar_one()
        assert db_summary.narrative == 'Concurrently updated'
        assert db_summary.version == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_summary_no_delta_returns_existing(metastore, session):
    """update_summary returns the existing summary when no new notes exist."""
    from memex_common.config import VaultSummaryConfig
    from memex_core.services.vault_summary import VaultSummaryService

    vault_id = await _create_vault(session)

    summary = VaultSummary(vault_id=vault_id, narrative='Current summary', version=2)
    session.add(summary)
    await session.commit()

    mock_lm = MagicMock(spec=['__call__'])
    svc = VaultSummaryService(
        metastore=metastore,
        lm=mock_lm,
        config=VaultSummaryConfig(),
    )

    # No new notes → should return existing without calling LLM
    with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
        result = await svc.update_summary(vault_id)
        mock_run.assert_not_called()

    assert result.narrative == 'Current summary'
    assert result.version == 2
