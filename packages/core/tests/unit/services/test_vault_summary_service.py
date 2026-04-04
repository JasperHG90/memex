"""Unit tests for VaultSummaryService."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.config import VaultSummaryConfig
from memex_core.memory.sql_models import VaultSummary
from memex_core.services.vault_summary import VaultSummaryService


def _make_service(config: VaultSummaryConfig | None = None) -> VaultSummaryService:
    """Create a VaultSummaryService with mock dependencies."""
    metastore = MagicMock()
    lm = MagicMock()
    return VaultSummaryService(
        metastore=metastore,
        lm=lm,
        config=config or VaultSummaryConfig(),
    )


def _mock_session(existing_summary: VaultSummary | None = None):
    """Create a mock async session context manager."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_summary
    session.execute.return_value = result
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


def _make_note_metadata(count: int = 5) -> list[dict]:
    """Build a list of rich note metadata dicts as returned by _fetch_note_metadata."""
    return [
        {
            'title': f'Note {i}',
            'publish_date': '2026-04-01',
            'tags': [f'tag-{i}'],
            'template': 'general_note',
            'author': 'test',
            'source_domain': 'example.com',
            'description': f'Description for note {i}',
            'summaries': [{'topic': f'Topic {i}', 'key_points': [f'Point {i}']}],
        }
        for i in range(count)
    ]


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_returns_existing_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, summary='Test summary')
        ctx, session = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        result = await svc.get_summary(vault_id)
        assert result == summary

    @pytest.mark.asyncio
    async def test_returns_none_when_no_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        ctx, session = _mock_session(None)
        svc.metastore.session = lambda: ctx
        result = await svc.get_summary(vault_id)
        assert result is None


class TestDeleteSummary:
    @pytest.mark.asyncio
    async def test_deletes_existing(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, summary='Test')
        ctx, session = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        result = await svc.delete_summary(vault_id)
        assert result is True
        session.delete.assert_called_once_with(summary)

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        svc = _make_service()
        vault_id = uuid4()
        ctx, session = _mock_session(None)
        svc.metastore.session = lambda: ctx
        result = await svc.delete_summary(vault_id)
        assert result is False


class TestIsStale:
    @pytest.mark.asyncio
    async def test_stale_when_no_summary_but_notes_exist(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        # First execute: summary query (None)
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = None
        # Second execute: count query (returns 5)
        count_result = MagicMock()
        count_result.scalar.return_value = 5
        session.execute = AsyncMock(side_effect=[summary_result, count_result])

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is True

    @pytest.mark.asyncio
    async def test_not_stale_when_no_summary_no_notes(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = None
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        session.execute = AsyncMock(side_effect=[summary_result, count_result])

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is False

    @pytest.mark.asyncio
    async def test_stale_when_new_notes_after_updated_at(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            summary='Old summary',
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        session = AsyncMock()
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = summary
        count_result = MagicMock()
        count_result.scalar.return_value = 3  # 3 new notes
        session.execute = AsyncMock(side_effect=[summary_result, count_result])

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is True

    @pytest.mark.asyncio
    async def test_not_stale_when_no_new_notes(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            summary='Current summary',
            updated_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
        )

        session = AsyncMock()
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = summary
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        session.execute = AsyncMock(side_effect=[summary_result, count_result])

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is False


class TestUpdateSummary:
    @pytest.mark.asyncio
    async def test_falls_back_to_regenerate_when_no_summary(self):
        svc = _make_service()
        vault_id = uuid4()

        # First session: summary lookup (None)
        ctx1, session1 = _mock_session(None)

        svc.metastore.session = lambda: ctx1
        svc.regenerate_summary = AsyncMock(
            return_value=VaultSummary(vault_id=vault_id, summary='Regenerated')
        )

        result = await svc.update_summary(vault_id)
        assert result.summary == 'Regenerated'
        svc.regenerate_summary.assert_called_once_with(vault_id)

    @pytest.mark.asyncio
    async def test_returns_existing_when_no_new_notes(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Existing summary',
            updated_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
        )

        # First session: summary lookup
        ctx1, _ = _mock_session(existing)
        # Second session: _fetch_note_metadata returns empty
        ctx2, session2 = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = []
        session2.execute.return_value = note_result

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            return ctx1 if call_count == 1 else ctx2

        svc.metastore.session = session_factory

        result = await svc.update_summary(vault_id)
        assert result.summary == 'Existing summary'

    @pytest.mark.asyncio
    async def test_updates_with_delta_notes(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Old overview',
            topics=[{'name': 'AI', 'note_count': 5, 'description': 'AI topics'}],
            stats={'total_notes': 10},
            version=3,
            notes_incorporated=10,
            patch_log=[],
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        delta_notes = _make_note_metadata(3)

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated overview with 3 new notes.'
        mock_prediction.updated_topics_json = json.dumps(
            [
                {'name': 'AI', 'note_count': 8, 'description': 'AI topics expanded'},
            ]
        )

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = delta_notes
            mock_run.return_value = mock_prediction

            # Mock sessions: summary lookup, total count, persist
            session_results = []

            # Session 1: summary lookup
            ctx1, s1 = _mock_session(existing)
            session_results.append(ctx1)

            # Session 2: _fetch_note_metadata (handled by mock)
            ctx2, s2 = _mock_session(None)
            session_results.append(ctx2)

            # Session 3: total count
            ctx3, s3 = _mock_session(None)
            count_result = MagicMock()
            count_result.scalar.return_value = 13
            s3.execute.return_value = count_result
            session_results.append(ctx3)

            # Session 4: persist
            ctx4, s4 = _mock_session(existing)
            session_results.append(ctx4)

            idx = 0

            def session_factory():
                nonlocal idx
                ctx = session_results[idx]
                idx += 1
                return ctx

            svc.metastore.session = session_factory

            result = await svc.update_summary(vault_id)

        assert result.summary == 'Updated overview with 3 new notes.'
        assert result.version == 4
        assert len(result.patch_log) == 1
        assert result.patch_log[0]['action'] == 'update'
        assert result.patch_log[0]['notes_added'] == 3

    @pytest.mark.asyncio
    async def test_patch_log_bounded_to_max(self):
        svc = _make_service(VaultSummaryConfig(max_patch_log=3))
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Summary',
            topics=[],
            stats={'total_notes': 10},
            version=5,
            notes_incorporated=10,
            patch_log=[
                {'action': 'update', 'notes_added': 1, 'timestamp': '2026-04-01T00:00:00'},
                {'action': 'update', 'notes_added': 2, 'timestamp': '2026-04-02T00:00:00'},
                {'action': 'update', 'notes_added': 3, 'timestamp': '2026-04-03T00:00:00'},
            ],
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated'
        mock_prediction.updated_topics_json = '[]'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = _make_note_metadata(1)
            mock_run.return_value = mock_prediction

            ctx1, _ = _mock_session(existing)
            ctx2, s2 = _mock_session(None)
            ctx3, s3 = _mock_session(None)
            count_result = MagicMock()
            count_result.scalar.return_value = 11
            s3.execute.return_value = count_result
            ctx4, _ = _mock_session(existing)

            results = [ctx1, ctx2, ctx3, ctx4]
            idx = 0

            def sf():
                nonlocal idx
                c = results[idx]
                idx += 1
                return c

            svc.metastore.session = sf
            result = await svc.update_summary(vault_id)

        assert len(result.patch_log) == 3  # bounded to max_patch_log=3


class TestRegenerateSummary:
    @pytest.mark.asyncio
    async def test_empty_vault(self):
        svc = _make_service()
        vault_id = uuid4()

        with patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = []

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)
        assert result.summary == 'This vault is empty.'
        assert result.notes_incorporated == 0

    @pytest.mark.asyncio
    async def test_tier1_small_vault(self):
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data = _make_note_metadata(10)

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Summary of 10 notes.'
        mock_prediction.topics_json = json.dumps(
            [{'name': 'General', 'note_count': 10, 'description': 'General topics'}]
        )

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = notes_data
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Summary of 10 notes.'
        assert result.notes_incorporated == 10
        assert result.patch_log == []
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_tier2_medium_vault(self):
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data = _make_note_metadata(100)

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Summary of 100 notes.'
        mock_prediction.topics_json = json.dumps(
            [{'name': 'T', 'note_count': 100, 'description': 'T'}]
        )
        mock_prediction.batch_summary = 'Batch summary'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = notes_data
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Summary of 100 notes.'
        # 2 extract calls + 1 merge = 3
        assert mock_run.call_count == 3

    @pytest.mark.asyncio
    async def test_tier3_large_vault(self):
        """AC-A08: >500 notes uses hierarchical summarization."""
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data = _make_note_metadata(600)

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Hierarchical summary of 600 notes.'
        mock_prediction.topics_json = json.dumps(
            [{'name': 'T', 'note_count': 600, 'description': 'T'}]
        )
        mock_prediction.batch_summary = 'Batch summary'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = notes_data
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Hierarchical summary of 600 notes.'
        # 12 extract + recursive merge calls
        assert mock_run.call_count >= 13

    @pytest.mark.asyncio
    async def test_batch_failure_is_skipped(self):
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data = _make_note_metadata(100)

        call_count = 0

        async def mock_run_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError('Batch 0 failed')
            prediction = MagicMock()
            prediction.summary = 'Partial summary.'
            prediction.topics_json = json.dumps(
                [{'name': 'T', 'note_count': 50, 'description': 'T'}]
            )
            prediction.batch_summary = 'Batch summary'
            return prediction

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = notes_data
            mock_run.side_effect = mock_run_side_effect

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Partial summary.'

    @pytest.mark.asyncio
    async def test_regeneration_resets_patch_log(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Old',
            patch_log=[{'action': 'update'}],
            version=5,
        )

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Fresh summary.'
        mock_prediction.topics_json = '[]'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = _make_note_metadata(5)
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(existing)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.patch_log == []
        assert result.version == 6


class TestFetchNoteMetadata:
    """Direct tests for _fetch_note_metadata to verify query construction and metadata assembly."""

    @pytest.mark.asyncio
    async def test_returns_rich_metadata(self):
        svc = _make_service()
        session = AsyncMock()

        # Mock note query result
        note_id = uuid4()
        note = MagicMock()
        note.id = note_id
        note.title = 'Test Note'
        note.description = 'A test description'
        note.publish_date = datetime(2026, 4, 1, tzinfo=timezone.utc)
        note.doc_metadata = {
            'tags': ['ai', 'ml'],
            'author': 'Jasper',
            'source_uri': 'https://example.com/article',
            'template': 'technical_brief',
        }

        note_result = MagicMock()
        note_result.all.return_value = [note]

        # Mock chunk query result
        chunk = MagicMock()
        chunk.note_id = note_id
        chunk.summary = {
            'topic': 'Machine Learning',
            'key_points': ['Gradient descent', 'Backprop'],
        }

        chunk_result = MagicMock()
        chunk_result.all.return_value = [chunk]

        session.execute = AsyncMock(side_effect=[note_result, chunk_result])

        result = await svc._fetch_note_metadata(session, uuid4())

        assert len(result) == 1
        meta = result[0]
        assert meta['title'] == 'Test Note'
        assert meta['description'] == 'A test description'
        assert meta['publish_date'] == '2026-04-01T00:00:00+00:00'
        assert meta['tags'] == ['ai', 'ml']
        assert meta['author'] == 'Jasper'
        assert meta['source_domain'] == 'example.com'
        assert meta['template'] == 'technical_brief'
        assert len(meta['summaries']) == 1
        assert meta['summaries'][0]['topic'] == 'Machine Learning'
        assert meta['summaries'][0]['key_points'] == ['Gradient descent', 'Backprop']

    @pytest.mark.asyncio
    async def test_filters_since_timestamp(self):
        """When since is provided, only notes after that timestamp are returned."""
        svc = _make_service()
        session = AsyncMock()

        note_result = MagicMock()
        note_result.all.return_value = []
        session.execute = AsyncMock(return_value=note_result)

        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        result = await svc._fetch_note_metadata(session, uuid4(), since=since)

        assert result == []
        # Verify execute was called (the since filter is in the SQL)
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_notes_with_no_content(self):
        """Notes with title='Untitled', no description, and no summaries are skipped."""
        svc = _make_service()
        session = AsyncMock()

        note = MagicMock()
        note.id = uuid4()
        note.title = None  # becomes 'Untitled'
        note.description = None
        note.publish_date = None
        note.doc_metadata = {}

        note_result = MagicMock()
        note_result.all.return_value = [note]

        chunk_result = MagicMock()
        chunk_result.all.return_value = []  # no chunk summaries

        session.execute = AsyncMock(side_effect=[note_result, chunk_result])

        result = await svc._fetch_note_metadata(session, uuid4())
        assert result == []  # skipped

    @pytest.mark.asyncio
    async def test_handles_missing_doc_metadata_fields(self):
        """Missing doc_metadata fields default to empty values."""
        svc = _make_service()
        session = AsyncMock()

        note = MagicMock()
        note.id = uuid4()
        note.title = 'Note With Minimal Metadata'
        note.description = None
        note.publish_date = None
        note.doc_metadata = None  # completely missing

        note_result = MagicMock()
        note_result.all.return_value = [note]

        chunk_result = MagicMock()
        chunk_result.all.return_value = []

        session.execute = AsyncMock(side_effect=[note_result, chunk_result])

        result = await svc._fetch_note_metadata(session, uuid4())
        assert len(result) == 1
        meta = result[0]
        assert meta['tags'] == []
        assert meta['author'] == ''
        assert meta['source_domain'] == ''
        assert meta['template'] == ''

    @pytest.mark.asyncio
    async def test_multiple_chunks_per_note(self):
        """Multiple chunk summaries are collected per note."""
        svc = _make_service()
        session = AsyncMock()

        note_id = uuid4()
        note = MagicMock()
        note.id = note_id
        note.title = 'Multi-Chunk Note'
        note.description = 'Has multiple chunks'
        note.publish_date = None
        note.doc_metadata = {}

        note_result = MagicMock()
        note_result.all.return_value = [note]

        chunk1 = MagicMock()
        chunk1.note_id = note_id
        chunk1.summary = {'topic': 'Topic A', 'key_points': ['Point A1']}
        chunk2 = MagicMock()
        chunk2.note_id = note_id
        chunk2.summary = {'topic': 'Topic B', 'key_points': ['Point B1']}

        chunk_result = MagicMock()
        chunk_result.all.return_value = [chunk1, chunk2]

        session.execute = AsyncMock(side_effect=[note_result, chunk_result])

        result = await svc._fetch_note_metadata(session, uuid4())
        assert len(result) == 1
        assert len(result[0]['summaries']) == 2
        assert result[0]['summaries'][0]['topic'] == 'Topic A'
        assert result[0]['summaries'][1]['topic'] == 'Topic B'


class TestPeriodicVaultSummaryTask:
    """Test the scheduler task function."""

    @pytest.mark.asyncio
    async def test_updates_stale_vaults(self):
        from memex_core.scheduler import periodic_vault_summary_task

        api = AsyncMock()
        vault1 = MagicMock()
        vault1.id = uuid4()
        vault1.name = 'vault1'
        vault2 = MagicMock()
        vault2.id = uuid4()
        vault2.name = 'vault2'

        api.list_vaults = AsyncMock(return_value=[vault1, vault2])
        api.vault_summary.is_stale = AsyncMock(side_effect=[True, False])
        api.vault_summary.update_summary = AsyncMock()

        with patch('memex_core.scheduler.background_session') as mock_bg:
            mock_bg.return_value.__aenter__ = AsyncMock()
            mock_bg.return_value.__aexit__ = AsyncMock(return_value=False)
            await periodic_vault_summary_task(api)

        # vault1 was stale → updated, vault2 was not → skipped
        api.vault_summary.update_summary.assert_called_once_with(vault1.id)

    @pytest.mark.asyncio
    async def test_handles_errors_gracefully(self):
        from memex_core.scheduler import periodic_vault_summary_task

        api = AsyncMock()
        api.list_vaults = AsyncMock(side_effect=RuntimeError('DB down'))

        with patch('memex_core.scheduler.background_session') as mock_bg:
            mock_bg.return_value.__aenter__ = AsyncMock()
            mock_bg.return_value.__aexit__ = AsyncMock(return_value=False)
            # Should not raise
            await periodic_vault_summary_task(api)
