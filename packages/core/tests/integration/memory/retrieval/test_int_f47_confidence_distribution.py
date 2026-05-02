"""F47: integration tests for the calibration histogram emitted at hydration.

Seeds memory units across confidence levels (1.0, 0.8, 0.6, 0.2), runs a real
retrieval through the full engine, and asserts ``CONFIDENCE_SCORE_DISTRIBUTION``
accumulates samples — proving the metric fires regardless of
``confidence_alpha`` (it ships at 0.0 by default).
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
class TestF47ConfidenceDistribution:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='class')
    async def reranker(self):
        return await get_reranking_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder, reranker):
        return RetrievalEngine(
            embedder=embedder,
            reranker=reranker,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

    async def _seed_unit(
        self,
        session: AsyncSession,
        *,
        note_id: UUID,
        entity_id: UUID,
        text: str,
        embedding: list[float],
        confidence: float,
    ) -> UUID:
        unit_id = uuid4()
        unit = MemoryUnit(
            id=unit_id,
            note_id=note_id,
            text=text,
            fact_type=FactTypes.WORLD,
            embedding=embedding,
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
            confidence=confidence,
        )
        ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID)
        session.add(unit)
        session.add(ue)
        return unit_id

    async def test_confidence_score_distribution_emits_at_hydration(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Hydration emits one CONFIDENCE_SCORE_DISTRIBUTION sample per unit
        regardless of ``confidence_alpha`` — accumulates calibration data
        before the boost is flipped on."""
        from memex_core.metrics import CONFIDENCE_SCORE_DISTRIBUTION

        text = 'Service mesh sidecars terminate TLS at the pod level.'
        emb = embedder.encode([text])[0].tolist()

        note = Note(id=uuid4(), original_text='F47 hydration metric', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name='ServiceMesh')
        session.add(note)
        session.add(entity)
        await session.flush()

        seeded_ids: list[UUID] = []
        for conf in (1.0, 0.8, 0.6, 0.2):
            uid = await self._seed_unit(
                session,
                note_id=note.id,
                entity_id=entity.id,
                text=text,
                embedding=emb,
                confidence=conf,
            )
            seeded_ids.append(uid)
        await session.commit()

        cumulative_sum_before = CONFIDENCE_SCORE_DISTRIBUTION._sum.get()

        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(query='service mesh sidecar tls', limit=10),
        )

        cumulative_sum_after = CONFIDENCE_SCORE_DISTRIBUTION._sum.get()

        assert len(results) > 0, 'retrieve returned no results — hydration not exercised'
        assert cumulative_sum_after > cumulative_sum_before, (
            'CONFIDENCE_SCORE_DISTRIBUTION must observe each hydrated unit; '
            f'before={cumulative_sum_before} after={cumulative_sum_after}'
        )
        # Sanity: at least one of our seeded units was returned.
        result_ids = {r.id for r in results}
        assert result_ids & set(seeded_ids), (
            'expected one of the seeded confidence units to surface in results'
        )

    async def test_alpha_zero_default_emits_neutral_boost(
        self, session: AsyncSession, embedder, reranker
    ):
        """At ship default ``confidence_alpha=0.0`` every observed boost is 1.0
        regardless of the unit's confidence value — proves the F47 composition
        is a no-op until the calibration flag is flipped.

        We seed a low-confidence unit, run a real retrieval through the engine
        with default config (``confidence_alpha=0.0``), then assert that the
        ``CONFIDENCE_BOOST_OBSERVED`` histogram's bucket count for boost=1.0
        increases — i.e., the boost factor recorded for the contradicted unit
        was the neutral 1.0, not a penalty.
        """
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED

        text = 'Service mesh sidecars terminate TLS at the pod level for zero trust.'
        emb = embedder.encode([text])[0].tolist()

        note = Note(
            id=uuid4(),
            original_text='F47 alpha-zero invariance',
            vault_id=GLOBAL_VAULT_ID,
        )
        entity = Entity(id=uuid4(), canonical_name='ServiceMesh')
        session.add(note)
        session.add(entity)
        await session.flush()

        # Seed multiple units so the rerank path is exercised with > 1 candidate.
        # If F47's boost were silently active at alpha=0, the rerank ordering
        # would shift; with alpha=0 every confidence_boost = 1.0.
        for conf in (1.0, 0.8, 0.5, 0.2):
            await self._seed_unit(
                session,
                note_id=note.id,
                entity_id=entity.id,
                text=text,
                embedding=emb,
                confidence=conf,
            )
        await session.commit()

        engine_default = RetrievalEngine(
            embedder=embedder,
            reranker=reranker,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )
        assert engine_default.retrieval_config.confidence_alpha == 0.0

        def _hist_count(h) -> int:
            for metric in h.collect():
                for sample in metric.samples:
                    if sample.name.endswith('_count'):
                        return int(sample.value)
            return 0

        sum_before = CONFIDENCE_BOOST_OBSERVED._sum.get()
        count_before = _hist_count(CONFIDENCE_BOOST_OBSERVED)

        results, _ = await engine_default.retrieve(
            session,
            RetrievalRequest(query='service mesh sidecar tls', limit=10),
        )

        sum_after = CONFIDENCE_BOOST_OBSERVED._sum.get()
        count_after = _hist_count(CONFIDENCE_BOOST_OBSERVED)

        assert len(results) > 0, 'retrieve returned no results — boost path not exercised'
        # Every observation at alpha=0 must equal 1.0 → sum increment == count increment.
        delta_sum = sum_after - sum_before
        delta_count = count_after - count_before
        assert delta_count > 0, 'CONFIDENCE_BOOST_OBSERVED received no samples'
        assert delta_sum == pytest.approx(delta_count, abs=1e-9), (
            f'At confidence_alpha=0.0, every observation must be 1.0 (neutral); '
            f'got delta_sum={delta_sum} delta_count={delta_count} '
            f'(mean={delta_sum / delta_count})'
        )
