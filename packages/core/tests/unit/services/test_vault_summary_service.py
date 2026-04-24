"""Unit tests for VaultSummaryService."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.config import VaultSummaryConfig
from memex_core.memory.sql_models import VaultSummary
from memex_core.services.vault_summary import VaultSummaryService, _estimate_tokens
from memex_core.services.vault_summary_signatures import LLMTheme


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


def _make_note_metadata(count: int = 5) -> tuple[list[dict], list, list]:
    """Build note metadata + IDs as returned by _fetch_note_metadata."""
    ids = [uuid4() for _ in range(count)]
    data = [
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
    return data, ids, ids


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_returns_existing_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, narrative='Test summary')
        ctx, session = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_apply_freshness_overlay', AsyncMock(side_effect=lambda s: s)):
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


class TestFreshnessOverlay:
    """Overlay recomputes time-sensitive fields on read and demotes stale trends."""

    @staticmethod
    def _fresh(**overrides):
        base = {
            'recent_activity': {'7d': 0, '30d': 0},
            'last_activity_at': None,
            'days_since_last_note': None,
            'date_range_latest': None,
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_overlays_fresh_recent_activity(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Test',
            inventory={
                'total_notes': 100,
                'recent_activity': {'7d': 277, '30d': 405},
                'last_activity_at': '2026-03-01T00:00:00+00:00',
                'days_since_last_note': 0,
                'date_range': {'earliest': '2025-01-01', 'latest': '2026-03-01'},
            },
            themes=[],
        )
        fresh = self._fresh(
            recent_activity={'7d': 0, '30d': 0},
            last_activity_at='2026-03-01T00:00:00+00:00',
            days_since_last_note=54,
            date_range_latest='2026-03-01',
        )
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert result.inventory['recent_activity'] == {'7d': 0, '30d': 0}
        assert result.inventory['last_activity_at'] == '2026-03-01T00:00:00+00:00'
        assert result.inventory['days_since_last_note'] == 54
        # Non-time-sensitive fields preserved from the persisted row
        assert result.inventory['total_notes'] == 100

    @pytest.mark.asyncio
    async def test_demotes_theme_trends_when_vault_cold(self):
        svc = _make_service(VaultSummaryConfig(dormant_threshold_days=30))
        vault_id = uuid4()
        persisted_themes = [
            {'name': 'A', 'note_count': 5, 'description': 'x', 'trend': 'growing'},
            {'name': 'B', 'note_count': 3, 'description': 'y', 'trend': 'stable'},
            {'name': 'C', 'note_count': 1, 'description': 'z', 'trend': 'dormant'},
        ]
        summary = VaultSummary(
            vault_id=vault_id, narrative='Test', inventory={}, themes=persisted_themes
        )
        fresh = self._fresh(last_activity_at='2026-03-01T00:00:00+00:00', days_since_last_note=50)
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert [t['trend'] for t in result.themes] == ['dormant', 'dormant', 'dormant']
        # Other theme fields preserved
        assert result.themes[0]['name'] == 'A'
        assert result.themes[1]['note_count'] == 3

    @pytest.mark.asyncio
    async def test_keeps_trends_when_vault_active(self):
        svc = _make_service(VaultSummaryConfig(dormant_threshold_days=30))
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Test',
            inventory={},
            themes=[
                {'name': 'A', 'note_count': 5, 'description': 'x', 'trend': 'growing'},
                {'name': 'B', 'note_count': 3, 'description': 'y', 'trend': 'stable'},
            ],
        )
        fresh = self._fresh(last_activity_at='2026-04-22T00:00:00+00:00', days_since_last_note=2)
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert [t['trend'] for t in result.themes] == ['growing', 'stable']

    @pytest.mark.asyncio
    async def test_threshold_boundary_does_not_demote(self):
        """days_since == threshold must NOT demote (strict > semantics)."""
        svc = _make_service(VaultSummaryConfig(dormant_threshold_days=30))
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Test',
            inventory={},
            themes=[
                {'name': 'A', 'note_count': 5, 'description': 'x', 'trend': 'growing'},
            ],
        )
        fresh = self._fresh(last_activity_at='2026-03-25T00:00:00+00:00', days_since_last_note=30)
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert result.themes[0]['trend'] == 'growing'

    @pytest.mark.asyncio
    async def test_overlay_preserves_template_and_tag_counts(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Test',
            inventory={
                'by_template': {'daily': 10, 'meeting': 5},
                'top_tags': {'work': 8, 'personal': 3},
            },
            themes=[],
        )
        fresh = self._fresh()
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert result.inventory['by_template'] == {'daily': 10, 'meeting': 5}
        assert result.inventory['top_tags'] == {'work': 8, 'personal': 3}

    @pytest.mark.asyncio
    async def test_handles_empty_vault(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='',
            inventory={},
            themes=[
                {'name': 'A', 'note_count': 0, 'description': 'x', 'trend': 'stable'},
            ],
        )
        fresh = self._fresh()
        ctx, _ = _mock_session(summary)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock(return_value=fresh)):
            result = await svc.get_summary(vault_id)
        assert result is not None
        assert result.inventory['last_activity_at'] is None
        assert result.inventory['days_since_last_note'] is None
        # Trend demotion should not run when last_activity is None
        assert result.themes[0]['trend'] == 'stable'

    @pytest.mark.asyncio
    async def test_none_summary_skips_overlay(self):
        svc = _make_service()
        vault_id = uuid4()
        ctx, _ = _mock_session(None)
        svc.metastore.session = lambda: ctx
        with patch.object(svc, '_compute_fresh_fields', AsyncMock()) as mock_fresh:
            result = await svc.get_summary(vault_id)
        assert result is None
        mock_fresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_compute_fresh_fields_returns_expected_shape(self):
        """_compute_fresh_fields returns the overlay keys with injected _now."""
        svc = _make_service()
        vault_id = uuid4()
        frozen_now = datetime(2026, 4, 24, tzinfo=timezone.utc)
        last_activity = datetime(2026, 4, 15, tzinfo=timezone.utc)
        publish_latest = datetime(2026, 4, 20, tzinfo=timezone.utc)

        session = AsyncMock()
        r7_res = MagicMock()
        r7_res.scalar = MagicMock(return_value=2)
        r30_res = MagicMock()
        r30_res.scalar = MagicMock(return_value=5)
        max_row = MagicMock()
        max_row.__getitem__ = lambda _self, idx: [last_activity, publish_latest][idx]
        max_res = MagicMock()
        max_res.one_or_none = MagicMock(return_value=max_row)
        session.execute = AsyncMock(side_effect=[r7_res, r30_res, max_res])
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        fresh = await svc._compute_fresh_fields(vault_id, _now=frozen_now)

        assert fresh['recent_activity'] == {'7d': 2, '30d': 5}
        assert fresh['last_activity_at'] == last_activity.isoformat()
        assert fresh['days_since_last_note'] == 9
        assert fresh['date_range_latest'] == publish_latest.isoformat()

    @pytest.mark.asyncio
    async def test_compute_fresh_fields_empty_vault(self):
        """_compute_fresh_fields handles empty vault (nulls)."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        r7_res = MagicMock()
        r7_res.scalar = MagicMock(return_value=0)
        r30_res = MagicMock()
        r30_res.scalar = MagicMock(return_value=0)
        max_res = MagicMock()
        max_res.one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(side_effect=[r7_res, r30_res, max_res])
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        fresh = await svc._compute_fresh_fields(vault_id)

        assert fresh['last_activity_at'] is None
        assert fresh['days_since_last_note'] is None
        assert fresh['date_range_latest'] is None
        assert fresh['recent_activity'] == {'7d': 0, '30d': 0}

    @pytest.mark.asyncio
    async def test_compute_inventory_accepts_injected_now(self):
        """_compute_inventory uses injected _now without patching datetime globally."""
        svc = _make_service()
        vault_id = uuid4()
        last_activity = datetime(2026, 4, 15, tzinfo=timezone.utc)
        frozen_now = datetime(2026, 4, 24, tzinfo=timezone.utc)

        session = AsyncMock()
        total_res = MagicMock()
        total_res.scalar = MagicMock(return_value=10)
        ent_res = MagicMock()
        ent_res.scalar = MagicMock(return_value=5)
        date_res = MagicMock()
        date_res.one_or_none = MagicMock(return_value=None)
        meta_res = MagicMock()
        meta_res.all = MagicMock(return_value=[])
        r7_res = MagicMock()
        r7_res.scalar = MagicMock(return_value=2)
        r30_res = MagicMock()
        r30_res.scalar = MagicMock(return_value=5)
        la_res = MagicMock()
        la_res.scalar = MagicMock(return_value=last_activity)
        session.execute = AsyncMock(
            side_effect=[total_res, ent_res, date_res, meta_res, r7_res, r30_res, la_res]
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        inv = await svc._compute_inventory(vault_id, _now=frozen_now)

        assert inv['last_activity_at'] == last_activity.isoformat()
        assert inv['days_since_last_note'] == 9
        assert inv['recent_activity'] == {'7d': 2, '30d': 5}

    @pytest.mark.asyncio
    async def test_compute_inventory_empty_vault_nulls(self):
        """_compute_inventory handles max(created_at) == None (empty vault)."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        total_res = MagicMock()
        total_res.scalar = MagicMock(return_value=0)
        ent_res = MagicMock()
        ent_res.scalar = MagicMock(return_value=0)
        date_res = MagicMock()
        date_res.one_or_none = MagicMock(return_value=None)
        meta_res = MagicMock()
        meta_res.all = MagicMock(return_value=[])
        r7_res = MagicMock()
        r7_res.scalar = MagicMock(return_value=0)
        r30_res = MagicMock()
        r30_res.scalar = MagicMock(return_value=0)
        la_res = MagicMock()
        la_res.scalar = MagicMock(return_value=None)
        session.execute = AsyncMock(
            side_effect=[total_res, ent_res, date_res, meta_res, r7_res, r30_res, la_res]
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        inv = await svc._compute_inventory(vault_id)

        assert inv['last_activity_at'] is None
        assert inv['days_since_last_note'] is None


class TestDeleteSummary:
    @pytest.mark.asyncio
    async def test_deletes_existing(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, narrative='Test')
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
    async def test_stale_when_unincorporated_notes_exist(self):
        """Stale when notes have summary_version_incorporated < summary.version."""
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Old summary',
            version=3,
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        session = AsyncMock()
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = summary
        count_result = MagicMock()
        count_result.scalar.return_value = 3  # 3 unincorporated notes
        session.execute = AsyncMock(side_effect=[summary_result, count_result])

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is True

    @pytest.mark.asyncio
    async def test_not_stale_when_all_notes_incorporated(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='Current summary',
            version=5,
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
            return_value=VaultSummary(vault_id=vault_id, narrative='Regenerated')
        )

        result = await svc.update_summary(vault_id)
        assert result.narrative == 'Regenerated'
        svc.regenerate_summary.assert_called_once_with(vault_id)

    @pytest.mark.asyncio
    async def test_returns_existing_when_no_new_notes(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Existing summary',
            version=3,
            updated_at=datetime(2026, 4, 4, tzinfo=timezone.utc),
        )

        with patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([], [], [])

            ctx1, s1 = _mock_session(existing)
            summary_result = MagicMock()
            summary_result.scalar_one_or_none.return_value = existing
            s1.execute = AsyncMock(return_value=summary_result)

            svc.metastore.session = lambda: ctx1

            result = await svc.update_summary(vault_id)
            assert result.narrative == 'Existing summary'

    @pytest.mark.asyncio
    async def test_updates_with_delta_notes(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Old overview',
            themes=[{'name': 'AI', 'note_count': 5, 'description': 'AI topics'}],
            inventory={'total_notes': 10},
            version=3,
            notes_incorporated=10,
            patch_log=[],
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        delta_notes, delta_ids, _ = _make_note_metadata(3)

        mock_prediction = MagicMock()
        mock_prediction.updated_narrative = 'Updated overview with 3 new notes.'
        mock_prediction.updated_themes = [
            LLMTheme(name='AI', description='AI topics expanded', note_count=3, trend='growing'),
        ]

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = (delta_notes, delta_ids, delta_ids)
            mock_inv.return_value = {'total_notes': 13, 'total_entities': 5}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            # Session 1: summary lookup + _fetch_note_metadata + total count
            ctx1, s1 = _mock_session(existing)
            summary_result = MagicMock()
            summary_result.scalar_one_or_none.return_value = existing
            count_result = MagicMock()
            count_result.scalar.return_value = 13
            s1.execute = AsyncMock(side_effect=[summary_result, count_result])

            # Session 2: persist with FOR UPDATE
            ctx2, s2 = _mock_session(existing)

            session_results = [ctx1, ctx2]
            idx = 0

            def session_factory():
                nonlocal idx
                ctx = session_results[idx]
                idx += 1
                return ctx

            svc.metastore.session = session_factory

            result = await svc.update_summary(vault_id)

        assert result.narrative == 'Updated overview with 3 new notes.'
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
            narrative='Summary',
            themes=[],
            inventory={'total_notes': 10},
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
        mock_prediction.updated_narrative = 'Updated'
        mock_prediction.updated_themes = []

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = _make_note_metadata(1)
            mock_inv.return_value = {'total_notes': 11}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            # Session 1: summary lookup + _fetch_note_metadata + total count
            ctx1, s1 = _mock_session(existing)
            summary_result = MagicMock()
            summary_result.scalar_one_or_none.return_value = existing
            count_result = MagicMock()
            count_result.scalar.return_value = 11
            s1.execute = AsyncMock(side_effect=[summary_result, count_result])

            # Session 2: persist with FOR UPDATE
            ctx2, _ = _mock_session(existing)

            results = [ctx1, ctx2]
            idx = 0

            def sf():
                nonlocal idx
                c = results[idx]
                idx += 1
                return c

            svc.metastore.session = sf
            result = await svc.update_summary(vault_id)

        assert len(result.patch_log) == 3  # bounded to max_patch_log=3

    @pytest.mark.asyncio
    async def test_skips_write_on_version_conflict(self):
        """If another update bumped the version during LLM call, skip the write."""
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Old overview',
            themes=[],
            inventory={'total_notes': 10},
            version=3,
            notes_incorporated=10,
            patch_log=[],
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        mock_prediction = MagicMock()
        mock_prediction.updated_narrative = 'Should not be persisted'
        mock_prediction.updated_themes = []

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = _make_note_metadata(1)
            mock_inv.return_value = {'total_notes': 11}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            # Session 1: return version=3
            ctx1, s1 = _mock_session(existing)
            summary_result = MagicMock()
            summary_result.scalar_one_or_none.return_value = existing
            count_result = MagicMock()
            count_result.scalar.return_value = 11
            s1.execute = AsyncMock(side_effect=[summary_result, count_result])

            # Session 2 (persist): return version=5 (bumped by concurrent update)
            concurrent_summary = VaultSummary(
                vault_id=vault_id,
                narrative='Concurrently updated',
                themes=[],
                inventory={'total_notes': 12},
                version=5,
                notes_incorporated=12,
                patch_log=[],
                updated_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
            )
            ctx2, _ = _mock_session(concurrent_summary)

            results = [ctx1, ctx2]
            idx = 0

            def sf():
                nonlocal idx
                c = results[idx]
                idx += 1
                return c

            svc.metastore.session = sf
            result = await svc.update_summary(vault_id)

        # Should return the concurrent version, NOT apply our LLM prediction
        assert result.narrative == 'Concurrently updated'
        assert result.version == 5


class TestRegenerateSummary:
    @pytest.mark.asyncio
    async def test_empty_vault(self):
        svc = _make_service()
        vault_id = uuid4()

        with patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([], [], [])

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)
        assert result.narrative == 'This vault is empty.'
        assert result.notes_incorporated == 0

    @pytest.mark.asyncio
    async def test_tier1_small_payload(self):
        """Small payload fits within max_batch_tokens → single LLM call."""
        config = VaultSummaryConfig(max_batch_tokens=50000)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data, note_ids, _ = _make_note_metadata(10)

        mock_prediction = MagicMock()
        mock_prediction.narrative = 'Summary of 10 notes.'
        mock_prediction.themes_json = json.dumps(
            [{'name': 'General', 'note_count': 10, 'description': 'General topics'}]
        )

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = (notes_data, note_ids, note_ids)
            mock_inv.return_value = {'total_notes': 10}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.narrative == 'Summary of 10 notes.'
        assert result.notes_incorporated == 10
        assert result.patch_log == []
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_tier2_medium_payload(self):
        """Medium payload uses two-pass topic extraction + merge."""
        # Use a small token budget so 100 notes exceed it but stay under 10x
        config = VaultSummaryConfig(max_batch_tokens=1000, batch_size=200)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data, note_ids, _ = _make_note_metadata(100)

        mock_prediction = MagicMock()
        mock_prediction.narrative = 'Summary of 100 notes.'
        mock_prediction.themes_json = json.dumps(
            [{'name': 'T', 'note_count': 100, 'description': 'T'}]
        )
        mock_prediction.batch_summary = 'Batch summary'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = (notes_data, note_ids, note_ids)
            mock_inv.return_value = {'total_notes': 100}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.narrative == 'Summary of 100 notes.'
        # Multiple extract calls + 1 merge
        assert mock_run.call_count >= 2

    @pytest.mark.asyncio
    async def test_tier3_large_payload(self):
        """Very large payload uses hierarchical summarization."""
        # Tiny token budget forces tier 3
        config = VaultSummaryConfig(max_batch_tokens=1000, batch_size=200)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data, note_ids, _ = _make_note_metadata(600)

        mock_prediction = MagicMock()
        mock_prediction.narrative = 'Hierarchical summary of 600 notes.'
        mock_prediction.themes_json = json.dumps(
            [{'name': 'T', 'note_count': 600, 'description': 'T'}]
        )
        mock_prediction.batch_summary = 'Batch summary'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = (notes_data, note_ids, note_ids)
            mock_inv.return_value = {'total_notes': 600}
            mock_ke.return_value = []
            mock_run.return_value = mock_prediction

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.narrative == 'Hierarchical summary of 600 notes.'
        # Many extract + recursive merge calls
        assert mock_run.call_count >= 5

    @pytest.mark.asyncio
    async def test_batch_failure_is_skipped(self):
        config = VaultSummaryConfig(max_batch_tokens=1000, batch_size=200)
        svc = _make_service(config)
        vault_id = uuid4()

        notes_data, note_ids, _ = _make_note_metadata(100)

        call_count = 0

        async def mock_run_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError('Batch 0 failed')
            prediction = MagicMock()
            prediction.narrative = 'Partial summary.'
            prediction.themes_json = json.dumps(
                [{'name': 'T', 'note_count': 50, 'description': 'T'}]
            )
            prediction.batch_summary = 'Batch summary'
            return prediction

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = (notes_data, note_ids, note_ids)
            mock_inv.return_value = {'total_notes': 100}
            mock_ke.return_value = []
            mock_run.side_effect = mock_run_side_effect

            ctx, session = _mock_session(None)
            svc.metastore.session = lambda: ctx

            result = await svc.regenerate_summary(vault_id)

        assert result.narrative == 'Partial summary.'

    @pytest.mark.asyncio
    async def test_regeneration_resets_patch_log(self):
        svc = _make_service()
        vault_id = uuid4()
        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Old',
            patch_log=[{'action': 'update'}],
            version=5,
        )

        mock_prediction = MagicMock()
        mock_prediction.narrative = 'Fresh summary.'
        mock_prediction.themes_json = '[]'

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch('memex_core.services.vault_summary.run_dspy_operation') as mock_run,
        ):
            mock_fetch.return_value = _make_note_metadata(5)
            mock_inv.return_value = {'total_notes': 5}
            mock_ke.return_value = []
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

        data, ids, _all = await svc._fetch_note_metadata(session, uuid4())

        assert len(data) == 1
        assert len(ids) == 1
        assert ids[0] == note_id
        meta = data[0]
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
    async def test_filters_by_summary_version(self):
        """When summary_version is provided, only unincorporated notes are returned."""
        svc = _make_service()
        session = AsyncMock()

        note_result = MagicMock()
        note_result.all.return_value = []
        session.execute = AsyncMock(return_value=note_result)

        data, ids, _all = await svc._fetch_note_metadata(session, uuid4(), summary_version=3)

        assert data == []
        assert ids == []
        # Verify execute was called (the version filter is in the SQL)
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

        data, ids, _all = await svc._fetch_note_metadata(session, uuid4())
        assert data == []
        assert ids == []

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

        data, ids, _all = await svc._fetch_note_metadata(session, uuid4())
        assert len(data) == 1
        meta = data[0]
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

        data, ids, _all = await svc._fetch_note_metadata(session, uuid4())
        assert len(data) == 1
        assert ids[0] == note_id
        assert len(data[0]['summaries']) == 2
        assert data[0]['summaries'][0]['topic'] == 'Topic A'
        assert data[0]['summaries'][1]['topic'] == 'Topic B'


class TestTokenBatching:
    """Tests for _split_into_token_batches and _estimate_tokens."""

    def test_estimate_tokens(self):
        assert _estimate_tokens('abcd') == 1
        assert _estimate_tokens('a' * 100) == 25

    def test_single_batch_when_within_budget(self):
        from memex_core.services.vault_summary import _split_into_token_batches

        notes = [{'title': f'Note {i}'} for i in range(3)]
        batches = _split_into_token_batches(notes, max_tokens=10000, max_notes=100)
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_splits_on_token_budget(self):
        from memex_core.services.vault_summary import _split_into_token_batches

        # Each note is ~50 chars → ~12 tokens
        notes = [{'title': f'Note {i}', 'description': 'x' * 40} for i in range(10)]
        batches = _split_into_token_batches(notes, max_tokens=30, max_notes=100)
        assert len(batches) > 1
        for batch in batches:
            assert len(batch) >= 1

    def test_splits_on_note_count_cap(self):
        from memex_core.services.vault_summary import _split_into_token_batches

        notes = [{'title': f'Note {i}'} for i in range(10)]
        batches = _split_into_token_batches(notes, max_tokens=100000, max_notes=3)
        assert len(batches) >= 4  # 10 notes / 3 per batch

    def test_oversized_single_note(self):
        """A single note exceeding max_tokens is placed alone in a batch."""
        from memex_core.services.vault_summary import _split_into_token_batches

        notes = [
            {'title': 'Small'},
            {'title': 'Huge', 'description': 'x' * 10000},
            {'title': 'Small2'},
        ]
        batches = _split_into_token_batches(notes, max_tokens=100, max_notes=100)
        # The huge note should be in its own batch
        assert any(len(b) == 1 and b[0]['title'] == 'Huge' for b in batches)


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
        # get_summary returns summaries without needs_regeneration flag
        summary1 = MagicMock()
        summary1.needs_regeneration = False
        summary2 = MagicMock()
        summary2.needs_regeneration = False
        api.vault_summary.get_summary = AsyncMock(side_effect=[summary1, summary2])
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
