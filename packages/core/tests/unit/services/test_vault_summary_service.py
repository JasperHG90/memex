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
    result.all.return_value = []
    session.execute.return_value = result
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    session.add = MagicMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_returns_existing_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(vault_id=vault_id, summary='Test summary')
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        result = await svc.get_summary(vault_id)
        assert result is existing
        assert result.summary == 'Test summary'

    @pytest.mark.asyncio
    async def test_returns_none_when_no_summary(self):
        svc = _make_service()
        ctx, session = _mock_session(None)
        svc.metastore.session.return_value = ctx

        result = await svc.get_summary(uuid4())
        assert result is None


class TestDeleteSummary:
    @pytest.mark.asyncio
    async def test_deletes_existing_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(vault_id=vault_id, summary='Test')
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        result = await svc.delete_summary(vault_id)
        assert result is True
        session.delete.assert_called_once_with(existing)
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_summary(self):
        svc = _make_service()
        ctx, session = _mock_session(None)
        svc.metastore.session.return_value = ctx

        result = await svc.delete_summary(uuid4())
        assert result is False
        session.delete.assert_not_called()


class TestPatchSummary:
    @pytest.mark.asyncio
    async def test_creates_new_summary_for_first_note(self):
        svc = _make_service()
        vault_id = uuid4()
        note_id = uuid4()
        ctx, session = _mock_session(None)
        svc.metastore.session.return_value = ctx

        # patch session.refresh to set created_at/updated_at on the summary
        async def fake_refresh(obj):
            obj.created_at = datetime.now(timezone.utc)
            obj.updated_at = datetime.now(timezone.utc)

        session.refresh.side_effect = fake_refresh

        result = await svc.patch_summary(vault_id, note_id, 'Test Note', 'A test description')
        assert result.vault_id == vault_id
        assert result.notes_incorporated == 1
        assert result.last_note_id == note_id
        assert result.summary == 'A test description'
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_patches_existing_summary_with_llm(self):
        svc = _make_service()
        vault_id = uuid4()
        note_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Existing summary.',
            topics=[{'name': 'AI', 'note_count': 1, 'description': 'AI topics'}],
            stats={'total_notes': 1},
            version=1,
            notes_incorporated=1,
            patch_log=[],
        )
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated summary with new note.'
        mock_prediction.updated_topics_json = json.dumps(
            [
                {'name': 'AI', 'note_count': 2, 'description': 'AI topics expanded'},
            ]
        )

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.patch_summary(vault_id, note_id, 'New Note', 'New description')

        assert result.summary == 'Updated summary with new note.'
        assert result.version == 2
        assert result.notes_incorporated == 2
        assert result.last_note_id == note_id
        assert len(result.patch_log) == 1
        assert result.patch_log[0]['action'] == 'patch'

    @pytest.mark.asyncio
    async def test_patch_log_bounded_to_max(self):
        """AC-A09: patch_log is bounded to max_patch_log entries."""
        config = VaultSummaryConfig(max_patch_log=20)
        svc = _make_service(config)
        vault_id = uuid4()

        # Start with 19 existing entries
        existing_log = [
            {
                'note_id': str(uuid4()),
                'action': 'patch',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
            for _ in range(19)
        ]
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Summary',
            topics=[],
            stats={'total_notes': 19},
            version=19,
            notes_incorporated=19,
            patch_log=existing_log,
        )
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated'
        mock_prediction.updated_topics_json = '[]'

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.patch_summary(vault_id, uuid4(), 'Note 20', 'Desc 20')

        assert len(result.patch_log) == 20  # 19 + 1 = 20, within limit

        # Now add one more — should trim to 20
        result.patch_log = list(result.patch_log)  # copy
        ctx2, session2 = _mock_session(result)
        svc.metastore.session.return_value = ctx2

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result2 = await svc.patch_summary(vault_id, uuid4(), 'Note 21', 'Desc 21')

        assert len(result2.patch_log) == 20  # Bounded at 20

    @pytest.mark.asyncio
    async def test_patch_25_times_stays_at_20(self):
        """AC-A09: Patch 25 times, verify patch_log length is 20."""
        config = VaultSummaryConfig(max_patch_log=20)
        svc = _make_service(config)
        vault_id = uuid4()

        # Simulate 25 patches building up a log
        existing_log = [
            {
                'note_id': str(uuid4()),
                'action': 'patch',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
            for _ in range(24)
        ]
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Summary',
            topics=[],
            stats={'total_notes': 24},
            version=24,
            notes_incorporated=24,
            patch_log=existing_log,
        )
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated'
        mock_prediction.updated_topics_json = '[]'

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.patch_summary(vault_id, uuid4(), 'Note 25', 'Desc 25')

        # 24 existing + 1 new = 25, should be trimmed to 20
        assert len(result.patch_log) == 20

    @pytest.mark.asyncio
    async def test_handles_invalid_topics_json(self):
        svc = _make_service()
        vault_id = uuid4()
        note_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            summary='Old summary',
            topics=[{'name': 'A', 'note_count': 1}],
            stats={'total_notes': 1},
            version=1,
            notes_incorporated=1,
            patch_log=[],
        )
        ctx, session = _mock_session(existing)
        svc.metastore.session.return_value = ctx

        mock_prediction = MagicMock()
        mock_prediction.updated_summary = 'Updated'
        mock_prediction.updated_topics_json = 'not valid json'

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.patch_summary(vault_id, note_id, 'Note', 'Desc')

        # Falls back to existing topics
        assert result.topics == [{'name': 'A', 'note_count': 1}]


class TestRegenerateSummary:
    @pytest.mark.asyncio
    async def test_empty_vault(self):
        svc = _make_service()
        vault_id = uuid4()

        # Two session calls: first for note query, second for summary create
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = []
        note_session.execute.return_value = note_result

        summary_ctx, summary_session = _mock_session(None)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        result = await svc.regenerate_summary(vault_id)
        assert result.summary == 'This vault is empty.'
        assert result.notes_incorporated == 0

    @pytest.mark.asyncio
    async def test_tier1_small_vault(self):
        """Tier 1: <= batch_size notes, single LLM call."""
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()
        note_ids = [uuid4() for _ in range(10)]

        # Mock note query
        notes = [
            MagicMock(id=nid, title=f'Note {i}', description=f'Desc {i}')
            for i, nid in enumerate(note_ids)
        ]
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = notes
        note_session.execute.return_value = note_result

        # Mock summary query (no existing summary)
        summary_ctx, summary_session = _mock_session(None)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Generated summary for 10 notes.'
        mock_prediction.topics_json = json.dumps(
            [
                {'name': 'General', 'note_count': 10, 'description': 'General topics'},
            ]
        )

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Generated summary for 10 notes.'
        assert result.notes_incorporated == 10
        assert result.last_note_id == note_ids[-1]
        assert result.patch_log == []  # Reset on regeneration
        # Verify single LLM call (tier 1)
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_tier2_medium_vault(self):
        """Tier 2: batch_size < n <= batch_size*10, two-pass."""
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        # 100 notes -> 2 batches
        notes = [
            MagicMock(id=uuid4(), title=f'Note {i}', description=f'Desc {i}') for i in range(100)
        ]
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = notes
        note_session.execute.return_value = note_result

        summary_ctx, summary_session = _mock_session(None)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        extract_prediction = MagicMock()
        extract_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 50, 'description': 'A topic'},
            ]
        )
        extract_prediction.batch_summary = 'Batch summary.'

        merge_prediction = MagicMock()
        merge_prediction.summary = 'Merged summary for 100 notes.'
        merge_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 100, 'description': 'Merged topic'},
            ]
        )

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            # 2 extract calls + 1 merge call = 3 total
            mock_run.side_effect = [extract_prediction, extract_prediction, merge_prediction]
            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Merged summary for 100 notes.'
        assert result.notes_incorporated == 100
        assert mock_run.call_count == 3

    @pytest.mark.asyncio
    async def test_tier3_large_vault(self):
        """AC-A08: Tier 3: >500 notes, hierarchical summarization."""
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        # 600 notes -> 12 batches
        notes = [
            MagicMock(id=uuid4(), title=f'Note {i}', description=f'Desc {i}') for i in range(600)
        ]
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = notes
        note_session.execute.return_value = note_result

        summary_ctx, summary_session = _mock_session(None)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        extract_prediction = MagicMock()
        extract_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 50, 'description': 'A topic'},
            ]
        )
        extract_prediction.batch_summary = 'Batch summary.'

        merge_prediction = MagicMock()
        merge_prediction.summary = 'Hierarchical summary for 600 notes.'
        merge_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 600, 'description': 'Merged topic'},
            ]
        )

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            # 12 extract calls + 1 merge call = 13 total
            mock_run.side_effect = [extract_prediction] * 12 + [merge_prediction]
            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Hierarchical summary for 600 notes.'
        assert result.notes_incorporated == 600
        assert mock_run.call_count == 13  # 12 batches + 1 merge

    @pytest.mark.asyncio
    async def test_batch_failure_is_skipped(self):
        """Error handling: if a batch fails, skip and note in summary."""
        config = VaultSummaryConfig(batch_size=50)
        svc = _make_service(config)
        vault_id = uuid4()

        notes = [
            MagicMock(id=uuid4(), title=f'Note {i}', description=f'Desc {i}') for i in range(100)
        ]
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = notes
        note_session.execute.return_value = note_result

        summary_ctx, summary_session = _mock_session(None)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        extract_prediction = MagicMock()
        extract_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 50, 'description': 'A topic'},
            ]
        )
        extract_prediction.batch_summary = 'Good batch.'

        merge_prediction = MagicMock()
        merge_prediction.summary = 'Summary despite batch failure.'
        merge_prediction.topics_json = json.dumps(
            [
                {'name': 'Topic', 'note_count': 50, 'description': 'A topic'},
            ]
        )

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            # First batch succeeds, second fails, merge succeeds
            mock_run.side_effect = [
                extract_prediction,
                RuntimeError('LLM failed'),
                merge_prediction,
            ]
            result = await svc.regenerate_summary(vault_id)

        assert result.summary == 'Summary despite batch failure.'
        assert mock_run.call_count == 3

    @pytest.mark.asyncio
    async def test_regeneration_resets_patch_log(self):
        """Regeneration should reset the patch log."""
        svc = _make_service()
        vault_id = uuid4()

        notes = [MagicMock(id=uuid4(), title='Note 1', description='Desc 1')]
        note_ctx, note_session = _mock_session(None)
        note_result = MagicMock()
        note_result.all.return_value = notes
        note_session.execute.return_value = note_result

        existing = VaultSummary(
            vault_id=vault_id,
            summary='Old',
            patch_log=[{'note_id': 'x', 'action': 'patch'}],
            version=5,
            notes_incorporated=5,
        )
        summary_ctx, summary_session = _mock_session(existing)

        call_count = 0

        def session_factory():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return note_ctx
            return summary_ctx

        svc.metastore.session = session_factory

        mock_prediction = MagicMock()
        mock_prediction.summary = 'Fresh summary.'
        mock_prediction.topics_json = '[]'

        with patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run:
            mock_run.return_value = mock_prediction
            result = await svc.regenerate_summary(vault_id)

        assert result.patch_log == []
        assert result.version == 6
