"""Integration tests for F33 ε-greedy exploration injection through the engine.

Verifies that exploration units actually surface in ``engine.retrieve()``
results when ``exploration_epsilon > 0`` and that no exploration units are
injected when disabled. Pure-function tests for ``inject_exploration_units``
live alongside ``packages/core/tests/unit/memory/retrieval/`` (formula-level
behavior) — these tests run through the full pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity


@pytest.mark.integration
class TestExplorationInjection:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='class')
    async def reranker(self):
        return await get_reranking_model()

    async def _seed_units(
        self,
        session: AsyncSession,
        embedder,
        *,
        warm_count: int = 1,
        cold_count: int = 5,
        topic: str = 'Python',
    ) -> dict[str, list[UUID]]:
        """Seed warm (high-MW) and cold (zero-MW) units sharing a topic."""
        note = Note(id=uuid4(), original_text='F33 corpus', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        warm_ids: list[UUID] = []
        cold_ids: list[UUID] = []

        for i in range(warm_count):
            text = f'Well-established fact {i} about {topic} runtime behavior.'
            uid = uuid4()
            session.add(
                MemoryUnit(
                    id=uid,
                    note_id=note.id,
                    text=text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([text])[0].tolist(),
                    vault_id=GLOBAL_VAULT_ID,
                    event_date=datetime.now(timezone.utc),
                    success_co_count=10,
                    failure_co_count=2,
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
            warm_ids.append(uid)

        for i in range(cold_count):
            text = f'Newly observed detail {i} about {topic} edge cases.'
            uid = uuid4()
            session.add(
                MemoryUnit(
                    id=uid,
                    note_id=note.id,
                    text=text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([text])[0].tolist(),
                    vault_id=GLOBAL_VAULT_ID,
                    event_date=datetime.now(timezone.utc),
                    success_co_count=0,
                    failure_co_count=0,
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
            cold_ids.append(uid)

        await session.commit()
        return {'warm': warm_ids, 'cold': cold_ids}

    async def test_exploration_injects_cold_start_unit_when_epsilon_one(
        self, session: AsyncSession, embedder, reranker
    ):
        """Positive control: with ε=1.0 and cold-start candidates outside the
        MMR-limited result set, at least one result must carry the
        ``exploration: true`` metadata flag.

        MMR (``mmr_lambda``) enforces ``request.limit``, leaving the rest of
        the hydrated candidate pool eligible for exploration injection.
        """
        # token_budget=0 disables packing-mode so MMR honors `request.limit`,
        # leaving cold-start candidates outside the result set for ε-greedy
        # injection to surface.
        config = RetrievalConfig(
            exploration_epsilon=1.0,
            exploration_max_injections=2,
            exploration_low_mw_threshold=5,
            token_budget=0,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)

        seeded = await self._seed_units(
            session, embedder, warm_count=1, cold_count=5, topic='Python'
        )

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='Python runtime details', limit=2, mmr_lambda=0.5),
        )

        # Positive control — without retrieval results, the injection point
        # is short-circuited, making any "no exploration" assertion vacuous.
        assert len(results) > 0, 'retrieve returned no results — exploration cannot fire'

        exploration_units = [u for u in results if u.unit_metadata.get('exploration', False)]
        assert len(exploration_units) >= 1, (
            f'F33 regression: ε=1.0 with {len(seeded["cold"])} cold-start candidates '
            f'must inject at least one exploration unit. results={[str(r.id) for r in results]}'
        )
        for u in exploration_units:
            total = u.success_co_count + u.failure_co_count
            assert total < 5, f'exploration unit must have low MW total; got {total} for {u.id}'

    async def test_exploration_disabled_when_epsilon_zero(
        self, session: AsyncSession, embedder, reranker
    ):
        """ε=0 → no injection. Positive control on ``len(results) > 0``
        prevents the assertion from passing vacuously."""
        config = RetrievalConfig(exploration_epsilon=0.0)
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)

        await self._seed_units(session, embedder, warm_count=1, cold_count=5, topic='Postgres')

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='Postgres edge cases', limit=5),
        )

        assert len(results) > 0, 'retrieve returned no results — assertion would be vacuous'
        exploration_units = [u for u in results if u.unit_metadata.get('exploration', False)]
        assert exploration_units == [], (
            f'ε=0 must inject no exploration units; got {len(exploration_units)}'
        )

    async def test_exploration_max_injections_caps_count(
        self, session: AsyncSession, embedder, reranker
    ):
        """``max_injections=1`` must cap exploration units at 1 even when ε=1
        and many cold-start candidates are eligible."""
        config = RetrievalConfig(
            exploration_epsilon=1.0,
            exploration_max_injections=1,
            exploration_low_mw_threshold=5,
            token_budget=0,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)

        await self._seed_units(session, embedder, warm_count=1, cold_count=5, topic='Redis')

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='Redis details', limit=2, mmr_lambda=0.5),
        )

        assert len(results) > 0
        exploration_units = [u for u in results if u.unit_metadata.get('exploration', False)]
        assert len(exploration_units) <= 1, (
            f'max_injections=1 violated; got {len(exploration_units)} exploration units'
        )

    async def test_exploration_skips_high_mw_units(self, session: AsyncSession, embedder, reranker):
        """Eligibility is gated by ``low_mw_threshold`` — high-MW units must
        never carry the ``exploration`` flag even with ε=1.0."""
        config = RetrievalConfig(
            exploration_epsilon=1.0,
            exploration_max_injections=3,
            exploration_low_mw_threshold=5,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)

        seeded = await self._seed_units(
            session, embedder, warm_count=3, cold_count=2, topic='Kafka'
        )

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='Kafka behavior', limit=10),
        )

        assert len(results) > 0
        warm_set = set(seeded['warm'])
        for u in results:
            if u.id in warm_set and u.unit_metadata.get('exploration', False):
                pytest.fail(
                    f'high-MW unit {u.id} (success+failure >= 5) wrongly flagged as exploration'
                )

    async def test_int_thompson_end_to_end(self, session: AsyncSession, embedder, reranker):
        """End-to-end Thompson mode: at least one result carries
        ``exploration=True`` and ``exploration_mode='thompson'``; the metric
        counter increments under the ``thompson`` label.
        """
        from memex_core.metrics import EXPLORATION_INJECTED_TOTAL

        config = RetrievalConfig(
            exploration_mode='thompson',
            exploration_max_injections=2,
            token_budget=0,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)
        await self._seed_units(session, embedder, warm_count=1, cold_count=5, topic='Thompson')

        before = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()
        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='Thompson runtime details', limit=2, mmr_lambda=0.5),
        )
        after = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()

        assert len(results) > 0
        thompson_injected = [
            u for u in results if u.unit_metadata.get('exploration_mode') == 'thompson'
        ]
        assert thompson_injected, 'Thompson mode produced no annotated injections'
        assert after == before + len(thompson_injected), (
            f'EXPLORATION_INJECTED_TOTAL{{mode="thompson"}} delta {after - before} does not match '
            f'injected count {len(thompson_injected)} — metric and annotation are out of sync'
        )

    async def test_int_thompson_bypass_pre_filter(self, session: AsyncSession, embedder, reranker):
        """Thompson sees units the pre-filter would have removed — parity with ε-greedy bypass."""
        config = RetrievalConfig(
            exploration_mode='thompson',
            exploration_max_injections=2,
            fsfm_branch_enabled=True,
            token_budget=0,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)
        seeded = await self._seed_units(
            session, embedder, warm_count=1, cold_count=5, topic='BypassThompson'
        )

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(
                query='BypassThompson edge cases',
                limit=2,
                mmr_lambda=0.5,
                apply_pre_filter=True,
            ),
        )

        assert len(results) > 0
        injected_ids = {
            u.id for u in results if u.unit_metadata.get('exploration_mode') == 'thompson'
        }
        cold_set = set(seeded['cold'])
        assert injected_ids & cold_set, (
            'Thompson bypass-pool did not surface any cold-start unit; pre-filter parity broken'
        )

    async def test_int_off_mode_no_injection(self, session: AsyncSession, embedder, reranker):
        """``exploration_mode='off'`` short-circuits the dispatch; neither
        label of ``EXPLORATION_INJECTED_TOTAL`` increments and no result
        carries an exploration annotation, even when ε is forced to 1.0.
        """
        from memex_core.metrics import EXPLORATION_INJECTED_TOTAL

        config = RetrievalConfig(
            exploration_mode='off',
            exploration_epsilon=1.0,
            exploration_max_injections=2,
            token_budget=0,
        )
        engine = RetrievalEngine(embedder=embedder, reranker=reranker, retrieval_config=config)
        await self._seed_units(session, embedder, warm_count=1, cold_count=5, topic='OffMode')

        eg_before = EXPLORATION_INJECTED_TOTAL.labels(mode='epsilon_greedy')._value.get()
        ts_before = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()
        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(query='OffMode details', limit=2, mmr_lambda=0.5),
        )
        eg_after = EXPLORATION_INJECTED_TOTAL.labels(mode='epsilon_greedy')._value.get()
        ts_after = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()

        assert len(results) > 0, (
            'retrieve returned no results — off-mode assertion would be vacuous'
        )
        assert eg_after == eg_before, "'off' mode must not increment the ε-greedy counter"
        assert ts_after == ts_before, "'off' mode must not increment the Thompson counter"
        assert all(not u.unit_metadata.get('exploration') for u in results), (
            "'off' mode must not annotate any result with exploration=True"
        )

    async def test_int_exploration_mode_label_emitted(
        self, session: AsyncSession, embedder, reranker
    ):
        """Both modes increment ``EXPLORATION_INJECTED_TOTAL`` under their respective labels."""
        from memex_core.metrics import EXPLORATION_INJECTED_TOTAL

        # ε-greedy half
        eg_config = RetrievalConfig(
            exploration_mode='epsilon_greedy',
            exploration_epsilon=1.0,
            exploration_max_injections=1,
            exploration_low_mw_threshold=5,
            token_budget=0,
        )
        eg_engine = RetrievalEngine(
            embedder=embedder, reranker=reranker, retrieval_config=eg_config
        )
        await self._seed_units(session, embedder, warm_count=1, cold_count=3, topic='LabelEpsilon')
        eg_before = EXPLORATION_INJECTED_TOTAL.labels(mode='epsilon_greedy')._value.get()
        await eg_engine.retrieve(
            session,
            RetrievalRequest(query='LabelEpsilon details', limit=2, mmr_lambda=0.5),
        )
        eg_after = EXPLORATION_INJECTED_TOTAL.labels(mode='epsilon_greedy')._value.get()

        # Thompson half
        ts_config = RetrievalConfig(
            exploration_mode='thompson',
            exploration_max_injections=1,
            token_budget=0,
        )
        ts_engine = RetrievalEngine(
            embedder=embedder, reranker=reranker, retrieval_config=ts_config
        )
        await self._seed_units(session, embedder, warm_count=1, cold_count=3, topic='LabelThompson')
        ts_before = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()
        await ts_engine.retrieve(
            session,
            RetrievalRequest(query='LabelThompson details', limit=2, mmr_lambda=0.5),
        )
        ts_after = EXPLORATION_INJECTED_TOTAL.labels(mode='thompson')._value.get()

        assert eg_after > eg_before, (
            'EXPLORATION_INJECTED_TOTAL{mode="epsilon_greedy"} did not increment'
        )
        assert ts_after > ts_before, 'EXPLORATION_INJECTED_TOTAL{mode="thompson"} did not increment'
