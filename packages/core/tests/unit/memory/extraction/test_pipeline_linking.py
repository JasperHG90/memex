"""Unit tests for extraction pipeline linking module."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.memory.extraction.pipeline.linking import (
    create_cross_doc_links,
    create_links,
)
from memex_common.types import FactTypes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(
    text: str = 'test fact',
    occurred: datetime | None = None,
    mentioned: datetime | None = None,
    doc_id: str = 'doc1',
    embedding: list[float] | None = None,
) -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type=FactTypes.WORLD,
        embedding=embedding or [0.1] * 384,
        occurred_start=occurred,
        mentioned_at=mentioned or datetime.now(timezone.utc),
        note_id=doc_id,
    )


# ===========================================================================
# create_links tests
# ===========================================================================


class TestCreateLinks:
    """Tests for the create_links pipeline function."""

    @pytest.mark.asyncio
    async def test_creates_temporal_links_for_same_document(self) -> None:
        """Temporal links are created between facts in the same document."""
        session = AsyncMock()
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        f1 = _make_fact(occurred=base, doc_id='doc1')
        f2 = _make_fact(occurred=base + timedelta(hours=1), doc_id='doc1')
        unit_ids = [str(uuid4()), str(uuid4())]

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg,
        ):
            mock_storage.find_similar_facts = AsyncMock(return_value=[])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)
            mock_pg.return_value.values.return_value.on_conflict_do_nothing.return_value = (
                MagicMock()
            )

            await create_links(session, unit_ids, [f1, f2])

            mock_pg.assert_called()
            values = mock_pg.return_value.values.call_args[0][0]
            temporal = [v for v in values if v['link_type'] == 'temporal']
            assert len(temporal) == 1
            assert temporal[0]['from_unit_id'] == unit_ids[0]
            assert temporal[0]['to_unit_id'] == unit_ids[1]

    @pytest.mark.asyncio
    async def test_no_temporal_links_across_documents(self) -> None:
        """Temporal links are NOT created between facts in different documents."""
        session = AsyncMock()
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        f1 = _make_fact(occurred=base, doc_id='doc1')
        f2 = _make_fact(occurred=base + timedelta(hours=1), doc_id='doc2')
        unit_ids = [str(uuid4()), str(uuid4())]

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg,
        ):
            mock_storage.find_similar_facts = AsyncMock(return_value=[])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)
            mock_pg.return_value.values.return_value.on_conflict_do_nothing.return_value = (
                MagicMock()
            )

            await create_links(session, unit_ids, [f1, f2])

            # No intra-doc temporal links should be created
            if mock_pg.return_value.values.called:
                values = mock_pg.return_value.values.call_args[0][0]
                temporal = [v for v in values if v['link_type'] == 'temporal']
                assert len(temporal) == 0

    @pytest.mark.asyncio
    async def test_creates_semantic_links(self) -> None:
        """Semantic links are created from similarity search results."""
        session = AsyncMock()
        f1 = _make_fact(text='Fact about AI')
        unit_ids = [str(uuid4())]
        target_uuid = uuid4()

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg,
        ):
            mock_storage.find_similar_facts = AsyncMock(return_value=[(target_uuid, 0.85)])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)
            mock_pg.return_value.values.return_value.on_conflict_do_nothing.return_value = (
                MagicMock()
            )

            await create_links(session, unit_ids, [f1])

            mock_storage.find_similar_facts.assert_called_once()
            values = mock_pg.return_value.values.call_args[0][0]
            semantic = [v for v in values if v['link_type'] == 'semantic']
            assert len(semantic) == 1
            assert semantic[0]['weight'] == 0.85
            assert semantic[0]['to_unit_id'] == str(target_uuid)

    @pytest.mark.asyncio
    async def test_skips_nan_similarity_scores(self) -> None:
        """NaN similarity scores are filtered out."""
        session = AsyncMock()
        f1 = _make_fact()
        unit_ids = [str(uuid4())]

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert'),
        ):
            mock_storage.find_similar_facts = AsyncMock(return_value=[(uuid4(), float('nan'))])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)

            # Should not raise
            await create_links(session, unit_ids, [f1])

    @pytest.mark.asyncio
    async def test_no_links_when_empty(self) -> None:
        """No DB insert when there are no links to create."""
        session = AsyncMock()

        with patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage:
            mock_storage.find_similar_facts = AsyncMock(return_value=[])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)

            await create_links(session, [], [])

            session.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_vault_id(self) -> None:
        """Verify default vault_id is GLOBAL_VAULT_ID."""
        session = AsyncMock()
        f1 = _make_fact()
        unit_ids = [str(uuid4())]
        target_uuid = uuid4()

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg,
        ):
            mock_storage.find_similar_facts = AsyncMock(return_value=[(target_uuid, 0.9)])
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)
            mock_pg.return_value.values.return_value.on_conflict_do_nothing.return_value = (
                MagicMock()
            )

            await create_links(session, unit_ids, [f1])

            values = mock_pg.return_value.values.call_args[0][0]
            for link in values:
                assert link['vault_id'] == GLOBAL_VAULT_ID


# ===========================================================================
# create_cross_doc_links tests
# ===========================================================================


class TestCreateCrossDocLinks:
    """Tests for the create_cross_doc_links pipeline function."""

    @pytest.mark.asyncio
    async def test_creates_predecessor_and_successor_links(self) -> None:
        """Links are created to both predecessor and successor."""
        session = AsyncMock()
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        f1 = _make_fact(occurred=base)
        unit_ids = [str(uuid4())]
        pred_uuid = uuid4()
        succ_uuid = uuid4()

        with (
            patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
            patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg,
        ):
            mock_storage.find_temporal_neighbor = AsyncMock(side_effect=[pred_uuid, succ_uuid])
            mock_pg.return_value.values.return_value.on_conflict_do_nothing.return_value = (
                MagicMock()
            )

            await create_cross_doc_links(session, unit_ids, [f1])

            values = mock_pg.return_value.values.call_args[0][0]
            assert len(values) == 2
            assert values[0]['from_unit_id'] == str(pred_uuid)
            assert values[1]['to_unit_id'] == str(succ_uuid)

    @pytest.mark.asyncio
    async def test_noop_on_empty_facts(self) -> None:
        """No-op when facts list is empty."""
        session = AsyncMock()

        await create_cross_doc_links(session, [], [])

        session.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_links_when_no_neighbors(self) -> None:
        """No links created when no temporal neighbors exist."""
        session = AsyncMock()
        f1 = _make_fact(occurred=datetime(2024, 6, 1, tzinfo=timezone.utc))
        unit_ids = [str(uuid4())]

        with patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage:
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)

            await create_cross_doc_links(session, unit_ids, [f1])

            session.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_earliest_and_latest_timestamps(self) -> None:
        """Verify predecessor search uses earliest, successor uses latest."""
        session = AsyncMock()
        early = datetime(2024, 1, 1, tzinfo=timezone.utc)
        late = datetime(2024, 12, 31, tzinfo=timezone.utc)
        f_early = _make_fact(occurred=early)
        f_late = _make_fact(occurred=late)
        unit_ids = [str(uuid4()), str(uuid4())]

        with patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage:
            mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)

            await create_cross_doc_links(session, unit_ids, [f_early, f_late])

            calls = mock_storage.find_temporal_neighbor.call_args_list
            assert len(calls) == 2
            # First call: predecessor (before earliest)
            assert calls[0][0][1] == early
            assert calls[0][1]['direction'] == 'before'
            # Second call: successor (after latest)
            assert calls[1][0][1] == late
            assert calls[1][1]['direction'] == 'after'
