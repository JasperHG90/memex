"""Regression tests for vault summary restructuring.

Covers:
1. Version-tracking fix (infinite loop prevention)
2. Inventory computation edge cases
3. Key entities computation
4. Structured vault summary fields (themes, narrative, inventory, key_entities)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.config import VaultSummaryConfig
from memex_core.memory.sql_models import VaultSummary
from memex_core.services.vault_summary import VaultSummaryService
from memex_core.services.vault_summary_signatures import LLMTheme


def _make_service(config: VaultSummaryConfig | None = None) -> VaultSummaryService:
    metastore = MagicMock()
    lm = MagicMock()
    return VaultSummaryService(
        metastore=metastore,
        lm=lm,
        config=config or VaultSummaryConfig(),
    )


# ─── 1. Version-tracking regression: no infinite loop ───


class TestVersionTrackingNoInfiniteLoop:
    """Regression: update_summary must mark ALL active notes with the new version,
    not just the delta notes.  Previously, only delta notes were marked, causing
    all other notes to become stale after each version bump — an infinite loop.
    """

    @pytest.mark.asyncio
    async def test_update_marks_all_active_notes_not_just_delta(self):
        """After update_summary, ALL active notes must have
        summary_version_incorporated == new_version, so is_stale returns False."""
        svc = _make_service()
        vault_id = uuid4()

        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Existing',
            themes=[],
            inventory={},
            key_entities=[],
            version=5,
            notes_incorporated=20,
            patch_log=[],
        )

        delta_ids = [uuid4()]

        # Phase 1 session: read summary + fetch delta + count
        session1 = AsyncMock()
        # First call: select VaultSummary → existing
        # Second call: _fetch_note_metadata (notes query)
        # Third call: _fetch_note_metadata (chunks query)
        # Fourth call: total count
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = existing

        notes_result = MagicMock()
        note_row = MagicMock()
        note_row.id = delta_ids[0]
        note_row.title = 'New Note'
        note_row.description = ''
        note_row.publish_date = None
        note_row.doc_metadata = {}
        notes_result.all.return_value = [note_row]

        chunks_result = MagicMock()
        chunks_result.all.return_value = []

        total_result = MagicMock()
        total_result.scalar.return_value = 21

        session1.execute = AsyncMock(
            side_effect=[summary_result, notes_result, chunks_result, total_result]
        )

        ctx1 = AsyncMock()
        ctx1.__aenter__ = AsyncMock(return_value=session1)
        ctx1.__aexit__ = AsyncMock(return_value=False)

        # Phase 2: compute_inventory session
        inv_session = AsyncMock()
        inv_results = [MagicMock() for _ in range(6)]
        inv_results[0].scalar.return_value = 21  # total_notes
        inv_results[1].scalar.return_value = 5  # total_entities
        inv_results[2].one_or_none.return_value = None  # date_range
        inv_results[3].all.return_value = []  # doc_metadata
        inv_results[4].scalar.return_value = 2  # recent_7d
        inv_results[5].scalar.return_value = 10  # recent_30d
        inv_session.execute = AsyncMock(side_effect=inv_results)
        inv_ctx = AsyncMock()
        inv_ctx.__aenter__ = AsyncMock(return_value=inv_session)
        inv_ctx.__aexit__ = AsyncMock(return_value=False)

        # Phase 2b: compute_key_entities session
        ke_session = AsyncMock()
        ke_result = MagicMock()
        ke_result.all.return_value = []
        ke_session.execute = AsyncMock(return_value=ke_result)
        ke_ctx = AsyncMock()
        ke_ctx.__aenter__ = AsyncMock(return_value=ke_session)
        ke_ctx.__aexit__ = AsyncMock(return_value=False)

        # Phase 4: persist session (SELECT FOR UPDATE + mark all)
        persist_session = AsyncMock()
        persist_summary_result = MagicMock()
        persist_summary_result.scalar_one_or_none.return_value = existing
        persist_session.execute = AsyncMock(return_value=persist_summary_result)
        persist_session.refresh = AsyncMock()
        persist_ctx = AsyncMock()
        persist_ctx.__aenter__ = AsyncMock(return_value=persist_session)
        persist_ctx.__aexit__ = AsyncMock(return_value=False)

        # Wire up sessions in order
        session_calls = [ctx1, inv_ctx, ke_ctx, persist_ctx]
        call_idx = {'i': 0}

        def next_session():
            ctx = session_calls[call_idx['i']]
            call_idx['i'] += 1
            return ctx

        svc.metastore.session = next_session

        # Mock LLM
        mock_prediction = MagicMock()
        mock_prediction.updated_narrative = 'Updated with new note'
        mock_prediction.updated_themes = []

        with patch(
            'memex_core.services.vault_summary.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=mock_prediction,
        ):
            await svc.update_summary(vault_id)

        # THE KEY ASSERTION: The persist session must have executed an UPDATE
        # that marks ALL active notes (WHERE vault_id = X AND status = 'active'),
        # NOT just the delta note IDs. Check the sa_update call.
        execute_calls = persist_session.execute.call_args_list

        # There should be multiple execute calls:
        # 1. SELECT FOR UPDATE
        # 2. UPDATE notes SET summary_version_incorporated = ...
        assert len(execute_calls) >= 2, (
            f'Expected at least 2 execute calls in persist phase, got {len(execute_calls)}'
        )

        # The UPDATE call is the second one. Verify it's a bulk update
        # (not an IN clause with specific IDs).
        update_call = execute_calls[1]
        update_stmt = update_call[0][0]
        # The compiled SQL should reference vault_id and status, not specific note IDs
        compiled = str(update_stmt.compile(compile_kwargs={'literal_binds': False}))
        assert 'vault_id' in compiled, 'UPDATE must filter by vault_id'
        assert 'status' in compiled, 'UPDATE must filter by status'
        # It should NOT contain an IN (...) clause (which would mean only delta IDs)
        # Check for " IN (" pattern — a SQL IN clause — not just substring "IN"
        assert ' IN (' not in compiled.upper(), (
            'UPDATE must not use IN clause — must mark ALL active notes, not just delta'
        )

    @pytest.mark.asyncio
    async def test_consecutive_updates_on_unchanged_vault_find_nothing(self):
        """After a successful update, is_stale() should return False if no
        new notes were added. This catches the infinite loop where version
        bumps cause previously-incorporated notes to become stale."""
        svc = _make_service()
        vault_id = uuid4()

        # Summary at version 6 — all notes incorporated
        summary = VaultSummary(
            vault_id=vault_id,
            narrative='All good',
            themes=[],
            inventory={'total_notes': 10},
            key_entities=[],
            version=6,
            notes_incorporated=10,
            patch_log=[],
        )

        session = AsyncMock()
        summary_result = MagicMock()
        summary_result.scalar_one_or_none.return_value = summary

        # is_stale query: count of notes where version_incorporated < 6
        count_result = MagicMock()
        count_result.scalar.return_value = 0  # No stale notes

        session.execute = AsyncMock(side_effect=[summary_result, count_result])
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        assert await svc.is_stale(vault_id) is False


# ─── 2. Inventory computation ───


class TestComputeInventory:
    """Tests for _compute_inventory — pure SQL aggregates, no LLM."""

    @pytest.mark.asyncio
    async def test_empty_vault_returns_zero_counts(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        results = [MagicMock() for _ in range(6)]
        results[0].scalar.return_value = 0  # total_notes
        results[1].scalar.return_value = 0  # total_entities
        results[2].one_or_none.return_value = (None, None)  # date_range
        results[3].all.return_value = []  # doc_metadata
        results[4].scalar.return_value = 0  # recent_7d
        results[5].scalar.return_value = 0  # recent_30d
        session.execute = AsyncMock(side_effect=results)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        inv = await svc._compute_inventory(vault_id)

        assert inv['total_notes'] == 0
        assert inv['total_entities'] == 0
        assert inv['date_range'] == {'earliest': None, 'latest': None}
        assert inv['by_template'] == {}
        assert inv['by_source_domain'] == {}
        assert inv['top_tags'] == {}
        assert inv['recent_activity'] == {'7d': 0, '30d': 0}

    @pytest.mark.asyncio
    async def test_aggregates_doc_metadata_correctly(self):
        """Templates, source domains, and tags are counted from doc_metadata JSONB."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        results = [MagicMock() for _ in range(6)]
        results[0].scalar.return_value = 3

        results[1].scalar.return_value = 2

        date_row = MagicMock()
        date_row.__getitem__ = lambda self, idx: [
            datetime(2024, 1, 15, tzinfo=timezone.utc),
            datetime(2026, 4, 6, tzinfo=timezone.utc),
        ][idx]
        results[2].one_or_none.return_value = date_row

        # doc_metadata rows
        results[3].all.return_value = [
            (
                {
                    'template': 'article',
                    'source_uri': 'https://arxiv.org/paper1',
                    'tags': ['ai', 'ml'],
                },
            ),
            ({'template': 'article', 'source_uri': 'https://arxiv.org/paper2', 'tags': ['ai']},),
            ({'template': 'bookmark', 'source_uri': 'https://github.com/repo', 'tags': ['code']},),
        ]

        results[4].scalar.return_value = 1
        results[5].scalar.return_value = 3
        session.execute = AsyncMock(side_effect=results)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        inv = await svc._compute_inventory(vault_id)

        assert inv['by_template'] == {'article': 2, 'bookmark': 1}
        assert inv['by_source_domain'] == {'arxiv.org': 2, 'github.com': 1}
        assert inv['top_tags'] == {'ai': 2, 'ml': 1, 'code': 1}
        assert inv['recent_activity'] == {'7d': 1, '30d': 3}

    @pytest.mark.asyncio
    async def test_null_doc_metadata_is_skipped(self):
        """Notes with null or non-dict doc_metadata don't break aggregation."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        results = [MagicMock() for _ in range(6)]
        results[0].scalar.return_value = 2
        results[1].scalar.return_value = 0
        results[2].one_or_none.return_value = (None, None)

        # Mixed: one valid, one None, one non-dict
        results[3].all.return_value = [
            ({'template': 'article', 'tags': ['ai']},),
            (None,),
            ('not-a-dict',),
        ]

        results[4].scalar.return_value = 0
        results[5].scalar.return_value = 0
        session.execute = AsyncMock(side_effect=results)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        inv = await svc._compute_inventory(vault_id)

        assert inv['by_template'] == {'article': 1}
        assert inv['top_tags'] == {'ai': 1}

    @pytest.mark.asyncio
    async def test_malformed_source_uri_is_skipped(self):
        """Notes with unparseable source_uri don't crash inventory."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        results = [MagicMock() for _ in range(6)]
        results[0].scalar.return_value = 1
        results[1].scalar.return_value = 0
        results[2].one_or_none.return_value = (None, None)

        results[3].all.return_value = [
            ({'source_uri': 'not://a[valid/url', 'tags': []},),
        ]

        results[4].scalar.return_value = 0
        results[5].scalar.return_value = 0
        session.execute = AsyncMock(side_effect=results)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        # Should not raise
        inv = await svc._compute_inventory(vault_id)
        assert isinstance(inv, dict)


# ─── 3. Key entities computation ───


class TestComputeKeyEntities:
    @pytest.mark.asyncio
    async def test_returns_sorted_by_mention_count(self):
        svc = _make_service()
        vault_id = uuid4()

        row1 = MagicMock()
        row1.canonical_name = 'Claude'
        row1.entity_type = 'product'
        row1.vault_mentions = 50

        row2 = MagicMock()
        row2.canonical_name = 'RAG'
        row2.entity_type = 'concept'
        row2.vault_mentions = 30

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [row1, row2]
        session.execute = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        entities = await svc._compute_key_entities(vault_id)

        assert len(entities) == 2
        assert entities[0] == {'name': 'Claude', 'type': 'product', 'mention_count': 50}
        assert entities[1] == {'name': 'RAG', 'type': 'concept', 'mention_count': 30}

    @pytest.mark.asyncio
    async def test_empty_vault_returns_empty_list(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        entities = await svc._compute_key_entities(vault_id)
        assert entities == []

    @pytest.mark.asyncio
    async def test_null_entity_type_becomes_unknown(self):
        svc = _make_service()
        vault_id = uuid4()

        row = MagicMock()
        row.canonical_name = 'Mystery'
        row.entity_type = None
        row.mention_count = 10

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [row]
        session.execute = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        entities = await svc._compute_key_entities(vault_id)
        assert entities[0]['type'] == 'unknown'

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        await svc._compute_key_entities(vault_id, limit=5)

        # Verify the SQL statement includes a LIMIT
        execute_call = session.execute.call_args[0][0]
        compiled = str(execute_call.compile(compile_kwargs={'literal_binds': False}))
        assert 'LIMIT' in compiled.upper()


# ─── 4. Structured fields ───


class TestStructuredFields:
    """Verify that update_summary and regenerate_summary populate all new fields."""

    @pytest.mark.asyncio
    async def test_update_populates_inventory_and_key_entities(self):
        """update_summary must call _compute_inventory and _compute_key_entities
        and write their results to the summary."""
        svc = _make_service()
        vault_id = uuid4()

        fake_inventory = {
            'total_notes': 10,
            'total_entities': 5,
            'date_range': {'earliest': '2024-01-01', 'latest': '2026-04-07'},
            'by_template': {'article': 8, 'bookmark': 2},
            'by_source_domain': {'arxiv.org': 5},
            'top_tags': {'ai': 7},
            'recent_activity': {'7d': 2, '30d': 5},
        }
        fake_entities = [{'name': 'Claude', 'type': 'product', 'mention_count': 20}]

        existing = VaultSummary(
            vault_id=vault_id,
            narrative='Existing narrative',
            themes=[],
            inventory={},
            key_entities=[],
            version=3,
            notes_incorporated=9,
            patch_log=[],
        )

        with (
            patch.object(svc, '_fetch_note_metadata', new_callable=AsyncMock) as mock_fetch,
            patch.object(svc, '_compute_inventory', new_callable=AsyncMock) as mock_inv,
            patch.object(svc, '_compute_key_entities', new_callable=AsyncMock) as mock_ke,
            patch(
                'memex_core.services.vault_summary.run_dspy_operation',
                new_callable=AsyncMock,
            ) as mock_llm,
        ):
            mock_fetch.return_value = (
                [
                    {
                        'title': 'New Note',
                        'publish_date': None,
                        'tags': [],
                        'template': '',
                        'author': '',
                        'source_domain': '',
                        'description': '',
                        'summaries': [],
                    }
                ],
                [uuid4()],
                [uuid4()],
            )
            mock_inv.return_value = fake_inventory
            mock_ke.return_value = fake_entities

            prediction = MagicMock()
            prediction.updated_narrative = 'Updated narrative'
            prediction.updated_themes = [
                LLMTheme(name='AI', description='AI stuff', note_count=1, trend='growing'),
            ]
            mock_llm.return_value = prediction

            # Phase 1: read session
            session1 = AsyncMock()
            s1_result = MagicMock()
            s1_result.scalar_one_or_none.return_value = existing
            total_result = MagicMock()
            total_result.scalar.return_value = 10
            session1.execute = AsyncMock(side_effect=[s1_result, total_result])
            ctx1 = AsyncMock()
            ctx1.__aenter__ = AsyncMock(return_value=session1)
            ctx1.__aexit__ = AsyncMock(return_value=False)

            # Phase 4: persist session
            persist_session = AsyncMock()
            p_result = MagicMock()
            p_result.scalar_one_or_none.return_value = existing
            persist_session.execute = AsyncMock(return_value=p_result)
            persist_session.refresh = AsyncMock()
            persist_ctx = AsyncMock()
            persist_ctx.__aenter__ = AsyncMock(return_value=persist_session)
            persist_ctx.__aexit__ = AsyncMock(return_value=False)

            svc.metastore.session = MagicMock(side_effect=[ctx1, persist_ctx])

            result = await svc.update_summary(vault_id)

            # Verify inventory and key_entities were computed and written
            mock_inv.assert_called_once_with(vault_id)
            mock_ke.assert_called_once_with(vault_id)
            assert result.inventory == fake_inventory
            assert result.key_entities == fake_entities
            assert result.narrative == 'Updated narrative'
            assert result.version == 4  # was 3, incremented

    @pytest.mark.asyncio
    async def test_empty_summary_has_all_structured_fields(self):
        """_create_empty_summary should populate all new fields with defaults."""
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.refresh = AsyncMock()

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session = lambda: ctx

        summary = await svc._create_empty_summary(vault_id)

        assert summary.narrative == 'This vault is empty.'
        assert summary.themes == []
        assert summary.inventory == {'total_notes': 0, 'total_entities': 0}
        assert summary.key_entities == []
        assert summary.notes_incorporated == 0
        assert summary.patch_log == []


# ─── 5. Config rename ───


class TestConfigRename:
    """Verify max_summary_tokens was renamed to max_narrative_tokens."""

    def test_config_has_max_narrative_tokens(self):
        config = VaultSummaryConfig()
        assert hasattr(config, 'max_narrative_tokens')
        assert config.max_narrative_tokens == 200

    def test_config_does_not_have_max_summary_tokens(self):
        config = VaultSummaryConfig()
        assert not hasattr(config, 'max_summary_tokens')
