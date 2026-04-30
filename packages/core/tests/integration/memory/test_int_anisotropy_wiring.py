"""F2 wiring tests — verify the shared anisotropy corrector observes
similarity values produced by extraction-dedup and contradiction candidate
selection.

The retrieval-side wiring is exercised by
``packages/core/tests/integration/memory/retrieval/test_int_anisotropy.py``.
These tests cover the two non-retrieval consumers added in F2's "Code adapted"
spec: ``find_similar_facts`` / ``check_duplicates_in_window`` (extraction),
and ``_get_semantic_candidates`` (contradiction).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.contradiction.candidates import _get_semantic_candidates
from memex_core.memory.extraction.storage import (
    check_duplicates_in_window,
    find_similar_facts,
)
from memex_core.memory.models.anisotropy import (
    get_shared_corrector,
    reset_shared_corrector_for_testing,
)
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity


@pytest.mark.integration
class TestAnisotropyWiring:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest.fixture(autouse=True)
    def _reset_corrector(self):
        """Each test starts from a fresh shared corrector so observation
        counts attribute cleanly to the call under test."""
        reset_shared_corrector_for_testing()
        yield
        reset_shared_corrector_for_testing()

    async def _seed_units(
        self,
        session: AsyncSession,
        embedder,
        texts: list[str],
    ) -> list[UUID]:
        note = Note(id=uuid4(), original_text='F2 wiring corpus', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name='Topic')
        session.add(note)
        session.add(entity)
        await session.flush()

        ids: list[UUID] = []
        for t in texts:
            uid = uuid4()
            session.add(
                MemoryUnit(
                    id=uid,
                    note_id=note.id,
                    text=t,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([t])[0].tolist(),
                    vault_id=GLOBAL_VAULT_ID,
                    event_date=datetime.now(timezone.utc),
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
            ids.append(uid)
        await session.commit()
        return ids

    async def test_find_similar_facts_routes_through_corrector(
        self, session: AsyncSession, embedder
    ):
        """``find_similar_facts`` must feed cosine similarities into the
        shared corrector and return normalized scores."""
        await self._seed_units(
            session,
            embedder,
            [
                'Postgres uses MVCC for concurrent transactions.',
                'MVCC isolates reads via row versioning.',
                'GraphQL resolvers run sequentially.',
                'gRPC uses HTTP/2 multiplexing.',
            ],
        )

        query_emb = embedder.encode(['Postgres concurrency control'])[0].tolist()

        before = get_shared_corrector().count
        results = await find_similar_facts(
            session,
            embedding=query_emb,
            limit=5,
            threshold=0.0,  # accept everything; we want to observe the wiring, not filter
            vault_ids=[GLOBAL_VAULT_ID],
        )
        after = get_shared_corrector().count

        assert results, 'find_similar_facts returned no results — wiring path not exercised'
        assert after > before, (
            f'shared corrector should accumulate observations during '
            f'find_similar_facts; before={before} after={after}'
        )

    async def test_check_duplicates_in_window_routes_through_corrector(
        self, session: AsyncSession, embedder
    ):
        """``check_duplicates_in_window`` must feed candidate similarities
        into the shared corrector before the threshold check."""
        await self._seed_units(
            session,
            embedder,
            [
                'Kafka partitions are append-only.',
                'Kafka topics partition data across brokers.',
                'Postgres MVCC handles concurrent writes.',
            ],
        )

        new_texts = ['Kafka partitions append messages in order.']
        new_embs = [embedder.encode([new_texts[0]])[0].tolist()]

        before = get_shared_corrector().count
        await check_duplicates_in_window(
            session,
            texts=new_texts,
            embeddings=new_embs,
            target_date=datetime.now(timezone.utc),
            window_hours=48,
            similarity_threshold=0.5,
            vault_ids=[GLOBAL_VAULT_ID],
        )
        after = get_shared_corrector().count

        assert after > before, (
            f'shared corrector should accumulate observations during '
            f'check_duplicates_in_window; before={before} after={after}'
        )

    async def test_contradiction_candidates_route_through_corrector(
        self, session: AsyncSession, embedder
    ):
        """``_get_semantic_candidates`` (contradiction-side) must feed
        similarities into the shared corrector."""
        ids = await self._seed_units(
            session,
            embedder,
            [
                'Service mesh sidecars handle telemetry.',
                'Sidecar containers offload observability concerns.',
                'Kubernetes pods run containerized workloads.',
            ],
        )

        # Pick the first seeded unit as the probe
        probe = await session.get(MemoryUnit, ids[0])
        assert probe is not None

        before = get_shared_corrector().count
        candidates = await _get_semantic_candidates(
            session,
            unit=probe,
            vault_id=GLOBAL_VAULT_ID,
            threshold=0.0,
        )
        after = get_shared_corrector().count

        # Fresh corrector + reasonable threshold → at least one candidate.
        # The wiring proof is the corrector count delta, not the candidate
        # set itself.
        assert after > before, (
            f'shared corrector should accumulate observations during '
            f'_get_semantic_candidates; before={before} after={after} '
            f'candidates={len(candidates)}'
        )
