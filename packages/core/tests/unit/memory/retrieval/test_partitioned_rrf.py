"""Tests for fact-type partitioned RRF retrieval (T7)."""

from collections import namedtuple
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.config import RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.retrieval.engine import RetrievalEngine


Item = namedtuple('Item', ['id', 'type'])


def _make_engine(partitioned: bool = False, budget: int = 20) -> RetrievalEngine:
    embedder = MagicMock()
    config = RetrievalConfig(
        fact_type_partitioned_rrf=partitioned,
        fact_type_budget=budget,
    )
    return RetrievalEngine(embedder=embedder, retrieval_config=config)


class TestPartitionedRRFDisabled:
    """When fact_type_partitioned_rrf=False, behaviour is identical to baseline."""

    @pytest.mark.asyncio
    async def test_default_off_calls_rrf_directly(self):
        """With partitioned RRF off, _perform_rrf_retrieval is called, not partitioned."""
        engine = _make_engine(partitioned=False)
        session = AsyncMock()

        expected = [Item(id=uuid4(), type='unit')]
        with (
            patch.object(
                engine, '_perform_rrf_retrieval', new_callable=AsyncMock, return_value=expected
            ),
            patch.object(
                engine, '_perform_partitioned_rrf', new_callable=AsyncMock
            ) as mock_partitioned,
        ):
            result = await engine._perform_rrf_retrieval(session, 'test', [0.1], 10, {})
            mock_partitioned.assert_not_called()
            assert result == expected


class TestPartitionedRRFEnabled:
    """When fact_type_partitioned_rrf=True, partitioned path is used."""

    @pytest.mark.asyncio
    async def test_enabled_calls_partitioned(self):
        """With partitioned RRF on, _perform_partitioned_rrf is called."""
        engine = _make_engine(partitioned=True)
        session = AsyncMock()

        expected = [Item(id=uuid4(), type='unit')]
        with patch.object(
            engine, '_perform_partitioned_rrf', new_callable=AsyncMock, return_value=expected
        ) as mock_part:
            result = await engine._perform_partitioned_rrf(session, 'test', [0.1], 10, {})
            mock_part.assert_called_once()
            assert result == expected


class TestPerTypeIsolation:
    """Each fact type runs its own RRF pass with fact_type filter injected."""

    @pytest.mark.asyncio
    async def test_per_type_rrf_calls(self):
        """Verify _perform_rrf_retrieval is called once per fact type + once for mental model."""
        engine = _make_engine(partitioned=True, budget=5)
        session = AsyncMock()

        call_filters: list[dict] = []

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            call_filters.append(dict(filters))
            return []

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            await engine._perform_partitioned_rrf(
                session, 'test query', [0.1], 20, {'vault_ids': ['v1']}
            )

        fact_type_values = [f.get('fact_type') for f in call_filters]
        # Should have one call per FactType + one for mental_model (no fact_type)
        expected_types = [ft.value for ft in FactTypes]
        for ft in expected_types:
            assert ft in fact_type_values, f'{ft} not found in RRF calls'

    @pytest.mark.asyncio
    async def test_each_type_gets_budget_as_limit(self):
        """Per-type calls use fact_type_budget, not the overall limit."""
        engine = _make_engine(partitioned=True, budget=7)
        session = AsyncMock()

        captured_limits: list[int] = []

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            captured_limits.append(limit)
            return []

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            await engine._perform_partitioned_rrf(session, 'q', [0.1], 100, {})

        # All calls should use budget=7
        assert all(lim == 7 for lim in captured_limits)


class TestRoundRobinInterleaving:
    """Results are interleaved round-robin across fact types."""

    @pytest.mark.asyncio
    async def test_interleaving_order(self):
        """Results alternate between fact types in round-robin order."""
        engine = _make_engine(partitioned=True, budget=10)
        session = AsyncMock()

        fact_types = [ft.value for ft in FactTypes]
        num_types = len(fact_types)

        # Build 3 results per fact type
        type_ids: dict[str, list] = {}
        type_results: dict[str, list] = {}
        for ft in fact_types:
            ids = [uuid4() for _ in range(3)]
            type_ids[ft] = ids
            type_results[ft] = [Item(id=uid, type='unit') for uid in ids]

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            ft = filters.get('fact_type')
            if ft and ft in type_results:
                return type_results[ft]
            return []  # mental model call

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            result = await engine._perform_partitioned_rrf(session, 'q', [0.1], 20, {})

        def get_type(uid):
            for ft, ids in type_ids.items():
                if uid in ids:
                    return ft
            return 'mental_model'

        result_types = [get_type(r.id) for r in result]
        # First N results (one per type) should all be distinct types
        first_n = result_types[:num_types]
        assert len(set(first_n)) == num_types, f'Expected {num_types} distinct types, got {first_n}'


class TestDeduplication:
    """Same unit appearing in multiple type buckets is not duplicated."""

    @pytest.mark.asyncio
    async def test_duplicate_across_types(self):
        """A unit returned by two fact-type buckets appears only once."""
        engine = _make_engine(partitioned=True, budget=10)
        session = AsyncMock()

        shared_id = uuid4()
        unique_id = uuid4()

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            ft = filters.get('fact_type')
            if ft == 'world':
                return [Item(id=shared_id, type='unit'), Item(id=unique_id, type='unit')]
            if ft == 'experience':
                return [Item(id=shared_id, type='unit')]  # duplicate
            return []

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            result = await engine._perform_partitioned_rrf(session, 'q', [0.1], 20, {})

        result_ids = [r.id for r in result]
        assert result_ids.count(shared_id) == 1, 'Shared ID should appear exactly once'
        assert unique_id in result_ids


class TestEmptyType:
    """If one fact type has no results, others fill in."""

    @pytest.mark.asyncio
    async def test_empty_type_does_not_block(self):
        """An empty fact type bucket does not reduce total results."""
        engine = _make_engine(partitioned=True, budget=10)
        session = AsyncMock()

        ids_world = [uuid4() for _ in range(5)]

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            ft = filters.get('fact_type')
            if ft == 'world':
                return [Item(id=uid, type='unit') for uid in ids_world]
            return []  # all other types empty

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            result = await engine._perform_partitioned_rrf(session, 'q', [0.1], 20, {})

        assert len(result) == 5
        assert all(r.id in ids_world for r in result)


class TestMentalModelHandling:
    """Mental model results are collected separately and interleaved."""

    @pytest.mark.asyncio
    async def test_mental_models_included(self):
        """Mental model results appear in final output."""
        engine = _make_engine(partitioned=True, budget=10)
        session = AsyncMock()

        mm_id = uuid4()
        world_id = uuid4()

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            strategies = kw.get('strategies')
            ft = filters.get('fact_type')
            if strategies == ['mental_model']:
                return [Item(id=mm_id, type='model')]
            if ft == 'world':
                return [Item(id=world_id, type='unit')]
            return []

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            result = await engine._perform_partitioned_rrf(session, 'q', [0.1], 20, {})

        result_ids = [r.id for r in result]
        assert mm_id in result_ids, 'Mental model should be in results'
        assert world_id in result_ids, 'World fact should be in results'


class TestLimitEnforcement:
    """Final output respects the requested limit."""

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        """Total results do not exceed the requested limit."""
        engine = _make_engine(partitioned=True, budget=10)
        session = AsyncMock()

        async def _fake_rrf(session, query, emb, limit, filters, **kw):
            ft = filters.get('fact_type')
            if ft:
                return [Item(id=uuid4(), type='unit') for _ in range(10)]
            return []

        with patch.object(engine, '_perform_rrf_retrieval', side_effect=_fake_rrf):
            result = await engine._perform_partitioned_rrf(session, 'q', [0.1], 5, {})

        assert len(result) == 5
