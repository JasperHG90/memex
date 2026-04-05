"""Integration tests for version-based vault summary incorporation tracking.

Verifies that summary_version_incorporated column, version-based filtering,
note marking after update/regen, and flag resets all work against real Postgres.
"""

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import update as sa_update
from sqlmodel import col, select, text

from memex_common.config import GLOBAL_VAULT_ID, VaultSummaryConfig
from memex_core.memory.sql_models import Chunk, ContentStatus, Note, VaultSummary
from memex_core.services.vault_summary import VaultSummaryService


async def _insert_note(
    session,
    vault_id=GLOBAL_VAULT_ID,
    title='Test Note',
    summary_version_incorporated=None,
    status='active',
) -> Note:
    """Insert a note directly via SQL for test setup."""
    note = Note(
        id=uuid4(),
        vault_id=vault_id,
        title=title,
        original_text=f'Content of {title} {uuid4()}',
        content_hash=str(uuid4()),
        status=status,
        summary_version_incorporated=summary_version_incorporated,
    )
    session.add(note)
    await session.flush()
    return note


async def _insert_chunk_with_summary(session, note_id, topic='Topic', key_points=None):
    """Insert a chunk with a summary for the given note."""
    chunk = Chunk(
        id=uuid4(),
        note_id=note_id,
        vault_id=GLOBAL_VAULT_ID,
        text=f'Chunk content {uuid4()}',
        content_hash=str(uuid4()),
        chunk_index=0,
        status=ContentStatus.ACTIVE,
        summary={'topic': topic, 'key_points': key_points or ['point 1']},
        embedding=[0.1] * 384,
    )
    session.add(chunk)
    await session.flush()
    return chunk


@pytest.mark.integration
class TestSummaryVersionColumn:
    """Test that the summary_version_incorporated column works correctly."""

    @pytest.mark.asyncio
    async def test_column_exists_and_defaults_to_null(self, session):
        """New notes have summary_version_incorporated = NULL."""
        note = await _insert_note(session)
        await session.commit()

        result = await session.execute(
            select(Note.summary_version_incorporated).where(col(Note.id) == note.id)
        )
        value = result.scalar_one()
        assert value is None

    @pytest.mark.asyncio
    async def test_column_can_be_set_and_read(self, session):
        """summary_version_incorporated can be set to an integer."""
        note = await _insert_note(session, summary_version_incorporated=5)
        await session.commit()

        result = await session.execute(
            select(Note.summary_version_incorporated).where(col(Note.id) == note.id)
        )
        assert result.scalar_one() == 5

    @pytest.mark.asyncio
    async def test_composite_index_exists(self, session):
        """idx_notes_summary_version index exists on (vault_id, summary_version_incorporated)."""
        result = await session.execute(
            text(
                'SELECT indexname FROM pg_indexes '
                "WHERE tablename = 'notes' AND indexname = 'idx_notes_summary_version'"
            )
        )
        row = result.first()
        assert row is not None, 'Composite index idx_notes_summary_version does not exist'


@pytest.mark.integration
class TestFetchNoteMetadataVersionFilter:
    """Test that _fetch_note_metadata correctly filters by summary version."""

    @pytest.mark.asyncio
    async def test_returns_unincorporated_notes(self, metastore):
        """Notes with NULL or old version are returned; current-version notes are not."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Unincorporated')
            await _insert_chunk_with_summary(session, n1.id, topic='Topic A')
            n2 = await _insert_note(session, title='Old version', summary_version_incorporated=2)
            await _insert_chunk_with_summary(session, n2.id, topic='Topic B')
            n3 = await _insert_note(
                session, title='Current version', summary_version_incorporated=5
            )
            await _insert_chunk_with_summary(session, n3.id, topic='Topic C')
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        async with metastore.session() as session:
            data, ids, _all = await svc._fetch_note_metadata(
                session, GLOBAL_VAULT_ID, summary_version=5
            )

        titles = [d['title'] for d in data]
        assert 'Unincorporated' in titles
        assert 'Old version' in titles
        assert 'Current version' not in titles
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_returns_all_active_without_version_filter(self, metastore):
        """Without summary_version, all active notes are returned (regen mode)."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Note A', summary_version_incorporated=5)
            await _insert_chunk_with_summary(session, n1.id)
            n2 = await _insert_note(session, title='Note B')
            await _insert_chunk_with_summary(session, n2.id)
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        async with metastore.session() as session:
            data, ids, _all = await svc._fetch_note_metadata(session, GLOBAL_VAULT_ID)

        assert len(data) == 2
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_excludes_non_active_notes(self, metastore):
        """Archived/superseded notes are never returned regardless of version."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Active note')
            await _insert_chunk_with_summary(session, n1.id)
            n2 = await _insert_note(session, title='Archived note', status='archived')
            await _insert_chunk_with_summary(session, n2.id)
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        async with metastore.session() as session:
            data, ids, _all = await svc._fetch_note_metadata(session, GLOBAL_VAULT_ID)

        assert len(data) == 1
        assert data[0]['title'] == 'Active note'


@pytest.mark.integration
class TestIsStaleVersionBased:
    """Test is_stale() uses version-based comparison."""

    @pytest.mark.asyncio
    async def test_stale_when_unincorporated_notes_exist(self, metastore):
        async with metastore.session() as session:
            await _insert_note(session, title='New note')  # NULL version
            summary = VaultSummary(vault_id=GLOBAL_VAULT_ID, summary='Old', version=3)
            session.add(summary)
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        assert await svc.is_stale(GLOBAL_VAULT_ID) is True

    @pytest.mark.asyncio
    async def test_not_stale_when_all_incorporated(self, metastore):
        async with metastore.session() as session:
            await _insert_note(session, title='Inc note', summary_version_incorporated=3)
            summary = VaultSummary(vault_id=GLOBAL_VAULT_ID, summary='Current', version=3)
            session.add(summary)
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        assert await svc.is_stale(GLOBAL_VAULT_ID) is False

    @pytest.mark.asyncio
    async def test_stale_when_old_version_notes_exist(self, metastore):
        async with metastore.session() as session:
            await _insert_note(session, title='Old note', summary_version_incorporated=2)
            summary = VaultSummary(vault_id=GLOBAL_VAULT_ID, summary='Current', version=5)
            session.add(summary)
            await session.commit()

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())
        assert await svc.is_stale(GLOBAL_VAULT_ID) is True


@pytest.mark.integration
class TestNoteMarkingAfterUpdate:
    """Test that notes are correctly marked after update/regen against real Postgres."""

    @pytest.mark.asyncio
    async def test_update_marks_processed_notes(self, metastore):
        """After update_summary, processed notes get summary_version_incorporated set."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Note to process')
            n1_id = n1.id
            await _insert_chunk_with_summary(session, n1.id, topic='ML basics')
            summary = VaultSummary(
                vault_id=GLOBAL_VAULT_ID, summary='Existing', version=3, topics=[]
            )
            session.add(summary)
            await session.commit()

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated summary with ML basics.'
        mock_prediction.updated_topics_json = json.dumps([{'name': 'ML', 'note_count': 1}])

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.update_summary(GLOBAL_VAULT_ID)

        assert result.version == 4

        # Verify the note was marked
        async with metastore.session() as session:
            note = await session.get(Note, n1_id)
            assert note.summary_version_incorporated == 4

    @pytest.mark.asyncio
    async def test_regenerate_marks_all_notes(self, metastore):
        """After regenerate_summary, all active notes get marked with new version."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Note A')
            n1_id = n1.id
            await _insert_chunk_with_summary(session, n1.id, topic='Topic A')
            n2 = await _insert_note(session, title='Note B', summary_version_incorporated=1)
            n2_id = n2.id
            await _insert_chunk_with_summary(session, n2.id, topic='Topic B')
            # Archived note should NOT be marked
            n3 = await _insert_note(session, title='Archived', status='archived')
            n3_id = n3.id
            await _insert_chunk_with_summary(session, n3.id, topic='Topic C')
            await session.commit()

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Full summary.'
        mock_prediction.topics_json = json.dumps([])

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.regenerate_summary(GLOBAL_VAULT_ID)

        new_version = result.version

        async with metastore.session() as session:
            note_a = await session.get(Note, n1_id)
            note_b = await session.get(Note, n2_id)
            note_c = await session.get(Note, n3_id)
            assert note_a.summary_version_incorporated == new_version
            assert note_b.summary_version_incorporated == new_version
            # Archived note was not fetched, so stays unmarked
            assert note_c.summary_version_incorporated is None

    @pytest.mark.asyncio
    async def test_already_incorporated_notes_not_refetched(self, metastore):
        """Notes already at current version are not returned by update_summary."""
        async with metastore.session() as session:
            # Note already incorporated at version 3
            n1 = await _insert_note(session, title='Already done', summary_version_incorporated=3)
            await _insert_chunk_with_summary(session, n1.id)
            # New note, not yet incorporated
            n2 = await _insert_note(session, title='New note')
            n2_id = n2.id
            await _insert_chunk_with_summary(session, n2.id, topic='New topic')

            summary = VaultSummary(
                vault_id=GLOBAL_VAULT_ID, summary='Current', version=3, topics=[]
            )
            session.add(summary)
            await session.commit()

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated with only new note.'
        mock_prediction.updated_topics_json = '[]'

        svc = VaultSummaryService(metastore=metastore, lm=MagicMock(), config=VaultSummaryConfig())

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.update_summary(GLOBAL_VAULT_ID)

        # Only 1 note in the delta (the new one)
        assert result.stats['new_since_last'] == 1

        async with metastore.session() as session:
            note = await session.get(Note, n2_id)
            assert note.summary_version_incorporated == 4


@pytest.mark.integration
class TestFlagResets:
    """Test that summary_version_incorporated resets appropriately."""

    @pytest.mark.asyncio
    async def test_upsert_resets_flag(self, metastore):
        """Content upsert (ON CONFLICT) resets summary_version_incorporated to NULL."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        note_id = uuid4()

        async with metastore.session() as session:
            # Initial insert with version set
            await session.execute(
                pg_insert(Note.__table__).values(  # type: ignore[attr-defined]
                    id=note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    title='Upsert test',
                    original_text='Original content',
                    content_hash='hash1',
                    summary_version_incorporated=5,
                )
            )
            await session.commit()

        async with metastore.session() as session:
            # Upsert with same ID — simulates content update
            from sqlalchemy import func

            insert_stmt = pg_insert(Note.__table__).values(  # type: ignore[attr-defined]
                id=note_id,
                vault_id=GLOBAL_VAULT_ID,
                title='Upsert test updated',
                original_text='Updated content',
                content_hash='hash2',
            )
            set_clause = {
                'title': insert_stmt.excluded.title,
                'original_text': insert_stmt.excluded.original_text,
                'content_hash': insert_stmt.excluded.content_hash,
                'updated_at': func.now(),
                'summary_version_incorporated': None,
            }
            await session.execute(
                insert_stmt.on_conflict_do_update(index_elements=['id'], set_=set_clause)
            )
            await session.commit()

        async with metastore.session() as session:
            note = await session.get(Note, note_id)
            assert note.summary_version_incorporated is None
            assert note.title == 'Upsert test updated'

    @pytest.mark.asyncio
    async def test_status_to_active_resets_flag(self, metastore):
        """Transitioning a note back to active resets summary_version_incorporated."""
        async with metastore.session() as session:
            note = await _insert_note(
                session,
                title='Archive then restore',
                status='archived',
                summary_version_incorporated=5,
            )
            note_id = note.id
            await session.commit()

        # Transition to active via the same pattern as NoteService.set_note_status
        async with metastore.session() as session:
            doc = await session.get(Note, note_id)
            doc.status = 'active'
            doc.superseded_by = None
            doc.appended_to = None
            doc.summary_version_incorporated = None
            session.add(doc)
            await session.commit()

        async with metastore.session() as session:
            note = await session.get(Note, note_id)
            assert note.status == 'active'
            assert note.summary_version_incorporated is None


@pytest.mark.integration
class TestSaUpdateWithSessionAdd:
    """Verify that sa_update() + session.add() in the same transaction works."""

    @pytest.mark.asyncio
    async def test_mixed_orm_core_in_same_transaction(self, metastore):
        """session.add(summary) + session.execute(sa_update(Note)) commit atomically."""
        async with metastore.session() as session:
            n1 = await _insert_note(session, title='Mixed test')
            n1_id = n1.id
            await session.commit()

        async with metastore.session() as session:
            # ORM-style add
            summary = VaultSummary(
                vault_id=GLOBAL_VAULT_ID, summary='Mixed test summary', version=1
            )
            session.add(summary)

            # Core-style update in same session
            mark_stmt = (
                sa_update(Note).where(col(Note.id) == n1_id).values(summary_version_incorporated=1)
            )
            await session.execute(mark_stmt)
            await session.commit()

        # Both should have persisted
        async with metastore.session() as session:
            note = await session.get(Note, n1_id)
            assert note.summary_version_incorporated == 1

            result = await session.execute(
                select(VaultSummary).where(col(VaultSummary.vault_id) == GLOBAL_VAULT_ID)
            )
            vs = result.scalar_one()
            assert vs.summary == 'Mixed test summary'
