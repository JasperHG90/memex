"""Unit tests for extraction pipeline fact_processing module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_core.memory.extraction.models import ExtractedFact, ProcessedFact
from memex_core.memory.extraction.pipeline.fact_processing import (
    add_temporal_offsets,
    process_embeddings,
)
from memex_common.types import FactTypes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(
    content_index: int = 0,
    occurred_start: datetime | None = None,
    occurred_end: datetime | None = None,
    mentioned_at: datetime | None = None,
) -> ExtractedFact:
    return ExtractedFact(
        fact_text='test fact',
        fact_type=FactTypes.WORLD,
        content_index=content_index,
        chunk_index=0,
        occurred_start=occurred_start,
        occurred_end=occurred_end,
        mentioned_at=mentioned_at or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# ===========================================================================
# add_temporal_offsets tests
# ===========================================================================


class TestAddTemporalOffsets:
    """Tests for the add_temporal_offsets function."""

    def test_no_facts(self) -> None:
        """Empty list is a no-op."""
        add_temporal_offsets([])

    def test_single_fact_no_offset(self) -> None:
        """A single fact gets zero offset."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        fact = _make_fact(occurred_start=base, mentioned_at=base)
        add_temporal_offsets([fact])
        assert fact.occurred_start == base
        assert fact.mentioned_at == base

    def test_multiple_facts_get_incremental_offsets(self) -> None:
        """Facts in the same content get increasing offsets."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        facts = [
            _make_fact(occurred_start=base, mentioned_at=base),
            _make_fact(occurred_start=base, mentioned_at=base),
            _make_fact(occurred_start=base, mentioned_at=base),
        ]
        add_temporal_offsets(facts, seconds_per_fact=10)

        assert facts[0].occurred_start == base
        assert facts[1].occurred_start == base + timedelta(seconds=10)
        assert facts[2].occurred_start == base + timedelta(seconds=20)

    def test_offsets_reset_on_content_index_change(self) -> None:
        """Offsets reset when content_index changes."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        facts = [
            _make_fact(content_index=0, occurred_start=base, mentioned_at=base),
            _make_fact(content_index=0, occurred_start=base, mentioned_at=base),
            _make_fact(content_index=1, occurred_start=base, mentioned_at=base),
            _make_fact(content_index=1, occurred_start=base, mentioned_at=base),
        ]
        add_temporal_offsets(facts, seconds_per_fact=5)

        # Content 0: positions 0 and 1
        assert facts[0].occurred_start == base
        assert facts[1].occurred_start == base + timedelta(seconds=5)
        # Content 1: positions reset to 0 and 1
        assert facts[2].occurred_start == base
        assert facts[3].occurred_start == base + timedelta(seconds=5)

    def test_custom_seconds_per_fact(self) -> None:
        """Custom seconds_per_fact is respected."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        facts = [
            _make_fact(occurred_start=base),
            _make_fact(occurred_start=base),
        ]
        add_temporal_offsets(facts, seconds_per_fact=60)

        assert facts[0].occurred_start == base
        assert facts[1].occurred_start == base + timedelta(seconds=60)

    def test_none_timestamps_skipped(self) -> None:
        """None timestamps are not modified."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        fact = _make_fact(occurred_start=None, occurred_end=None, mentioned_at=base)
        add_temporal_offsets([fact, fact])
        assert fact.occurred_start is None
        assert fact.occurred_end is None

    def test_occurred_end_also_offset(self) -> None:
        """occurred_end gets the same offset as occurred_start."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        facts = [
            _make_fact(occurred_start=base, occurred_end=end),
            _make_fact(occurred_start=base, occurred_end=end),
        ]
        add_temporal_offsets(facts, seconds_per_fact=10)

        assert facts[1].occurred_start == base + timedelta(seconds=10)
        assert facts[1].occurred_end == end + timedelta(seconds=10)

    def test_mentioned_at_also_offset(self) -> None:
        """mentioned_at timestamp gets offset too."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        facts = [
            _make_fact(mentioned_at=base),
            _make_fact(mentioned_at=base),
        ]
        add_temporal_offsets(facts, seconds_per_fact=10)

        assert facts[0].mentioned_at == base
        assert facts[1].mentioned_at == base + timedelta(seconds=10)


# ===========================================================================
# process_embeddings tests
# ===========================================================================


class TestProcessEmbeddings:
    """Tests for the process_embeddings function."""

    @pytest.mark.asyncio
    async def test_returns_processed_facts(self) -> None:
        """Each extracted fact produces one processed fact."""
        model = MagicMock()
        model.encode = MagicMock(return_value=[[0.1] * 384, [0.2] * 384])

        fact1 = _make_fact()
        fact2 = _make_fact()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.format_facts_for_embedding',
                lambda facts: [f.fact_text for f in facts],
            )
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.generate_embeddings_batch',
                AsyncMock(return_value=[[0.1] * 384, [0.2] * 384]),
            )

            result = await process_embeddings(model, [fact1, fact2])

        assert len(result) == 2
        assert all(isinstance(pf, ProcessedFact) for pf in result)
        assert result[0].embedding == [0.1] * 384
        assert result[1].embedding == [0.2] * 384

    @pytest.mark.asyncio
    async def test_preserves_vault_id(self) -> None:
        """vault_id from extracted fact is preserved on processed fact."""
        from uuid import uuid4

        vault_id = uuid4()
        fact = _make_fact()
        fact.vault_id = vault_id

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.format_facts_for_embedding',
                lambda facts: [f.fact_text for f in facts],
            )
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.generate_embeddings_batch',
                AsyncMock(return_value=[[0.5] * 384]),
            )

            result = await process_embeddings(MagicMock(), [fact])

        assert result[0].vault_id == vault_id

    @pytest.mark.asyncio
    async def test_empty_facts(self) -> None:
        """Empty input returns empty output."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.format_facts_for_embedding',
                lambda facts: [],
            )
            mp.setattr(
                'memex_core.memory.extraction.pipeline.fact_processing.'
                'embedding_processor.generate_embeddings_batch',
                AsyncMock(return_value=[]),
            )

            result = await process_embeddings(MagicMock(), [])

        assert result == []
