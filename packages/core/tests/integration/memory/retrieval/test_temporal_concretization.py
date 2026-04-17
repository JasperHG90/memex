"""Integration tests for temporal query concretization (POC #4).

Tests the full pipeline: reference_date resolution, regex extraction,
LLM fallback, and config gating — all against a real Postgres database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import MemoryUnit, Note


@pytest.mark.integration
class TestTemporalConcretization:
    """Prove temporal concretization works end-to-end."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def _seed_memory(
        self,
        session: AsyncSession,
        embedder,
        text: str,
        event_date: datetime,
    ) -> MemoryUnit:
        """Insert a note + memory unit at a specific event_date."""
        emb = embedder.encode([text])[0].tolist()
        note = Note(id=uuid4(), original_text=f'Test note {uuid4()}')
        session.add(note)
        unit = MemoryUnit(
            id=uuid4(),
            text=text,
            embedding=emb,
            fact_type=FactTypes.WORLD,
            event_date=event_date,
            note_id=note.id,
        )
        session.add(unit)
        await session.flush()
        await session.commit()
        return unit

    # -----------------------------------------------------------------
    # Test 1: reference_date makes relative dates deterministic
    # -----------------------------------------------------------------
    async def test_relative_date_with_reference_date(self, session: AsyncSession, embedder) -> None:
        """'last week' resolves relative to reference_date, not now()."""
        # Seed a memory unit dated 2024-06-10 (within "last week" of 2024-06-15)
        unit_in_range = await self._seed_memory(
            session,
            embedder,
            f'Meeting notes about project kickoff {uuid4()}',
            event_date=datetime(2024, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
        )
        # Seed a memory unit dated 2024-01-01 (well outside the range)
        unit_out_of_range = await self._seed_memory(
            session,
            embedder,
            f'Old architecture decision from January {uuid4()}',
            event_date=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )

        engine = RetrievalEngine(embedder=embedder)
        request = RetrievalRequest(
            query='what happened last week',
            reference_date=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            limit=10,
        )
        results, debug_info = await engine.retrieve(session, request)

        # The in-range unit should appear; the out-of-range unit should be filtered
        result_ids = {u.id for u in results}
        assert unit_in_range.id in result_ids, (
            'Memory from 2024-06-10 should be within "last week" of 2024-06-15'
        )
        assert unit_out_of_range.id not in result_ids, (
            'Memory from 2024-01-01 should be outside "last week" of 2024-06-15'
        )

    # -----------------------------------------------------------------
    # Test 2: existing regex patterns still work
    # -----------------------------------------------------------------
    async def test_regex_extraction_still_works(self, session: AsyncSession, embedder) -> None:
        """Existing regex patterns continue to work unchanged."""
        unit_march = await self._seed_memory(
            session,
            embedder,
            f'March project update and review {uuid4()}',
            event_date=datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        unit_july = await self._seed_memory(
            session,
            embedder,
            f'July status report {uuid4()}',
            event_date=datetime(2024, 7, 15, 10, 0, 0, tzinfo=timezone.utc),
        )

        engine = RetrievalEngine(embedder=embedder)
        request = RetrievalRequest(
            query='what happened in March 2024',
            reference_date=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            limit=10,
        )
        results, _ = await engine.retrieve(session, request)

        result_ids = {u.id for u in results}
        assert unit_march.id in result_ids, (
            'Memory from March 2024 should be found by regex extraction'
        )
        assert unit_july.id not in result_ids, (
            'Memory from July 2024 should be outside "in March 2024" filter'
        )

    # -----------------------------------------------------------------
    # Test 3: LLM fallback fires on ambiguous temporal queries
    # -----------------------------------------------------------------
    async def test_llm_fallback_on_ambiguous_temporal(
        self, session: AsyncSession, embedder
    ) -> None:
        """LLM fallback fires when regex fails on temporal-sounding query."""
        # Seed a unit dated in Feb 2024 (the range the mock LLM will return)
        unit_feb = await self._seed_memory(
            session,
            embedder,
            f'Onboarding documentation and setup {uuid4()}',
            event_date=datetime(2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        unit_dec = await self._seed_memory(
            session,
            embedder,
            f'December holiday planning notes {uuid4()}',
            event_date=datetime(2024, 12, 15, 10, 0, 0, tzinfo=timezone.utc),
        )

        # Mock the LLM to return Feb 2024 date range
        mock_run = AsyncMock(
            return_value=SimpleNamespace(
                start_date='2024-01-15T00:00:00+00:00',
                end_date='2024-02-28T23:59:59+00:00',
            )
        )

        config = RetrievalConfig(temporal_concretization_enabled=True)

        with patch(
            'memex_core.memory.retrieval.temporal_concretizer.run_dspy_operation',
            side_effect=mock_run,
        ):
            # Create engine with a dummy LM so concretizer is instantiated
            from unittest.mock import MagicMock

            engine = RetrievalEngine(
                embedder=embedder,
                lm=MagicMock(),
                retrieval_config=config,
            )
            request = RetrievalRequest(
                query='what did we discuss during the onboarding',
                reference_date=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
                limit=10,
            )
            results, _ = await engine.retrieve(session, request)

        # LLM was called (regex can't parse "during the onboarding")
        assert mock_run.call_count == 1

        result_ids = {u.id for u in results}
        assert unit_feb.id in result_ids, (
            'Memory from Feb 2024 should be found via LLM concretization'
        )
        assert unit_dec.id not in result_ids, (
            'Memory from Dec 2024 should be outside LLM-concretized range (Jan-Feb 2024)'
        )

    # -----------------------------------------------------------------
    # Test 4: non-temporal queries skip all temporal filtering
    # -----------------------------------------------------------------
    async def test_no_temporal_expression_skips_filter(
        self, session: AsyncSession, embedder
    ) -> None:
        """Non-temporal queries don't trigger any temporal filtering."""
        unit = await self._seed_memory(
            session,
            embedder,
            f'Project architecture overview and design {uuid4()}',
            event_date=datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
        )

        engine = RetrievalEngine(embedder=embedder)
        request = RetrievalRequest(
            query='what is the project architecture',
            limit=10,
        )
        results, _ = await engine.retrieve(session, request)

        result_ids = {u.id for u in results}
        assert unit.id in result_ids, (
            'Without temporal filtering, the unit should appear in results'
        )

    # -----------------------------------------------------------------
    # Test 5: config disables LLM concretization
    # -----------------------------------------------------------------
    async def test_concretization_disabled_skips_llm(self, session: AsyncSession, embedder) -> None:
        """When config disables concretization, LLM fallback is skipped."""
        await self._seed_memory(
            session,
            embedder,
            f'Sprint planning notes {uuid4()}',
            event_date=datetime(2024, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
        )

        mock_run = AsyncMock()

        config = RetrievalConfig(temporal_concretization_enabled=False)

        with patch(
            'memex_core.memory.retrieval.temporal_concretizer.run_dspy_operation',
            side_effect=mock_run,
        ):
            from unittest.mock import MagicMock

            engine = RetrievalEngine(
                embedder=embedder,
                lm=MagicMock(),
                retrieval_config=config,
            )
            request = RetrievalRequest(
                query='what did we discuss during the onboarding',
                reference_date=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
                limit=10,
            )
            results, _ = await engine.retrieve(session, request)

        # LLM should NOT have been called
        assert mock_run.call_count == 0, 'LLM concretizer should not be called when disabled'
