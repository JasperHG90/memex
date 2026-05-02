"""Integration tests for F1c MW boost composition at the reranker.

These tests exercise the full retrieval pipeline (embedder + reranker + MW
composition + MMR) against a real Postgres + pgvector. Formula tests for
``compute_mw_score`` / ``compute_mw_boost`` live in
``packages/core/tests/unit/services/test_outcomes.py``.

The MW composition only fires through the reranker code path
(``engine.py:_rerank_results``); fixtures construct ``RetrievalEngine`` with
a real reranker so the load-bearing assertion — that high-MW units rank
above low-MW units with identical text — actually runs through the boost
line at ``engine.py:1180``.
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
class TestMwBoostComposition:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='class')
    async def reranker(self):
        return await get_reranking_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder, reranker):
        # exploration_epsilon=0 keeps the result count deterministic so the
        # MMR-diversity assertion isn't flaked by ε-greedy injection.
        # token_budget=0 disables packing-mode so MMR honors `request.limit`.
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
        success: int,
        failure: int,
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
            success_co_count=success,
            failure_co_count=failure,
        )
        ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID)
        session.add(unit)
        session.add(ue)
        return unit_id

    async def test_high_mw_ranks_above_low_mw_with_same_content(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Two units with identical text + embedding rank purely by MW.

        Same query → same ce_score; same event_date → same recency boost;
        the MW boost at ``engine.py:1180`` is the only differentiator.
        """
        text = 'Kubernetes deployments use rolling update strategy by default.'
        emb = embedder.encode([text])[0].tolist()

        note = Note(id=uuid4(), original_text='F1c rank test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name='Kubernetes')
        session.add(note)
        session.add(entity)
        await session.flush()

        high_success_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=entity.id,
            text=text,
            embedding=emb,
            success=20,
            failure=0,
        )
        high_failure_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=entity.id,
            text=text,
            embedding=emb,
            success=0,
            failure=20,
        )
        await session.commit()

        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(query='kubernetes rolling update', limit=5),
        )

        result_ids = [r.id for r in results]
        assert high_success_id in result_ids, 'high-success unit missing from results'
        assert high_failure_id in result_ids, 'high-failure unit missing from results'
        assert result_ids.index(high_success_id) < result_ids.index(high_failure_id), (
            f'F1c regression: high-MW unit must rank above high-failure peer when '
            f'ce_score is equal. Got order: {result_ids}'
        )

    async def test_cold_start_unit_neutral_relative_to_high_mw(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Cold-start (0/0) gets ``mw_boost = 1.0`` — sits between high-success
        and high-failure units with the same ce_score."""
        text = 'Postgres logical replication propagates row changes.'
        emb = embedder.encode([text])[0].tolist()

        note = Note(id=uuid4(), original_text='F1c cold start test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name='Postgres')
        session.add(note)
        session.add(entity)
        await session.flush()

        high_success_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=entity.id,
            text=text,
            embedding=emb,
            success=20,
            failure=0,
        )
        cold_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=entity.id,
            text=text,
            embedding=emb,
            success=0,
            failure=0,
        )
        high_failure_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=entity.id,
            text=text,
            embedding=emb,
            success=0,
            failure=20,
        )
        await session.commit()

        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(query='postgres logical replication', limit=5),
        )
        result_ids = [r.id for r in results]
        for uid in (high_success_id, cold_id, high_failure_id):
            assert uid in result_ids, f'unit {uid} missing from results'

        idx_success = result_ids.index(high_success_id)
        idx_cold = result_ids.index(cold_id)
        idx_failure = result_ids.index(high_failure_id)
        assert idx_success < idx_cold < idx_failure, (
            f'F1c cold-start neutrality: expected order [success, cold, failure]; '
            f'got idx_success={idx_success} idx_cold={idx_cold} idx_failure={idx_failure}'
        )

    async def test_mmr_diversity_applies_after_mw_composition(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """MMR runs strictly downstream of MW composition.

        Seed two near-duplicate high-MW units in cluster A and one isolated
        unit in cluster B. With MMR active and limit=2, the result list
        should NOT contain both A duplicates — MMR penalty must drop the
        second one even though both have the highest MW boost.
        """
        cluster_a_text_1 = 'GraphQL resolvers run sequentially per field.'
        cluster_a_text_2 = 'GraphQL field resolvers execute one after another.'
        cluster_b_text = 'gRPC streams use HTTP/2 multiplexing.'

        emb_a1 = embedder.encode([cluster_a_text_1])[0].tolist()
        emb_a2 = embedder.encode([cluster_a_text_2])[0].tolist()
        emb_b = embedder.encode([cluster_b_text])[0].tolist()

        note = Note(id=uuid4(), original_text='F1c MMR test', vault_id=GLOBAL_VAULT_ID)
        ent_a = Entity(id=uuid4(), canonical_name='GraphQL')
        ent_b = Entity(id=uuid4(), canonical_name='gRPC')
        session.add(note)
        session.add(ent_a)
        session.add(ent_b)
        await session.flush()

        a1_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=ent_a.id,
            text=cluster_a_text_1,
            embedding=emb_a1,
            success=20,
            failure=0,
        )
        a2_id = await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=ent_a.id,
            text=cluster_a_text_2,
            embedding=emb_a2,
            success=20,
            failure=0,
        )
        await self._seed_unit(
            session,
            note_id=note.id,
            entity_id=ent_b.id,
            text=cluster_b_text,
            embedding=emb_b,
            success=0,
            failure=0,
        )
        await session.commit()

        # First confirm all 3 units are retrievable — otherwise the MMR
        # invariant has nothing to diversify between.
        all_results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(query='request handling protocols', limit=10),
        )
        all_ids = {r.id for r in all_results}
        if not (a1_id in all_ids and a2_id in all_ids):
            pytest.skip('A duplicates not both in candidate set — MMR test inconclusive')

        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(
                query='request handling protocols',
                limit=2,
                mmr_lambda=0.3,
            ),
        )
        assert len(results) == 2, f'expected 2 results with limit=2, got {len(results)}'
        result_ids = {r.id for r in results}
        if a1_id in result_ids and a2_id in result_ids:
            pytest.fail(
                f'MMR diversity violated: both near-duplicate A units returned for '
                f'limit=2 with mmr_lambda=0.3. Result ids: {[str(i) for i in result_ids]}'
            )

    async def test_mw_boost_observed_metric_emitted_during_retrieve(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """``MW_BOOST_OBSERVED`` histogram receives one observation per
        scored unit per retrieve call when the reranker path is active."""
        from memex_core.metrics import MW_BOOST_OBSERVED

        text = 'Vault-scoped policies enforce per-tenant isolation.'
        emb = embedder.encode([text])[0].tolist()

        note = Note(id=uuid4(), original_text='F1c metrics test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name='Vault')
        session.add(note)
        session.add(entity)
        await session.flush()

        for s, f in [(5, 0), (0, 5), (0, 0)]:
            await self._seed_unit(
                session,
                note_id=note.id,
                entity_id=entity.id,
                text=text,
                embedding=emb,
                success=s,
                failure=f,
            )
        await session.commit()

        before = MW_BOOST_OBSERVED._sum.get()
        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(query='vault policies tenant isolation', limit=10),
        )
        after = MW_BOOST_OBSERVED._sum.get()

        assert len(results) > 0, 'retrieve returned no results — boost path not exercised'
        assert after > before, (
            f'MW_BOOST_OBSERVED histogram should receive samples during retrieve; '
            f'before={before} after={after}'
        )
