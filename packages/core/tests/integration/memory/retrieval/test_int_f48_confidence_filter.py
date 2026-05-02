"""F48 — pre-reranker confidence filter integration tests.

The pinning unit tests in
``packages/core/tests/unit/memory/retrieval/test_f48_sql_builder.py`` lock
down the SQL-builder shape; this file proves the predicate fires
end-to-end and that:

* Cold-start units (``confidence = 1.0``, the schema default) survive.
* Boundary units (``confidence == 0.2``) survive — strict ``<`` semantics.
* Strongly-contradicted units (``confidence < 0.2``) are pruned when
  ``apply_pre_filter=True`` (the default).
* The same units re-appear when ``apply_pre_filter=False`` (the F40 single
  bypass also covers the F48 branch).
* Vault scoping holds: vault A's search never sees vault B's units,
  regardless of pre-filter state.
* F44 — the F33 exploration bypass surfaces a low-confidence unit pruned
  by F48 via the separate hydration path (the self-correction property
  must hold for the F48 branch too — without F44's bypass, low-confidence
  units would never re-validate).
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
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity, Vault


# Pre-merge sequencing gate — F40 must be on the base branch.
assert 'apply_pre_filter' in RetrievalRequest.model_fields, (
    'F48 requires F40 to be merged on the base branch — '
    "RetrievalRequest must expose the 'apply_pre_filter' field."
)


@pytest.mark.integration
class TestF48ConfidenceFilter:
    """End-to-end coverage of the F48 confidence branch — five confidence
    bands cover the round-3 review's critical cases:

    * **1.0** (cold-start, schema default) — kept (1.0 > 0.2).
    * **0.5** (mid) — kept.
    * **0.2** (boundary) — kept (strict ``<``).
    * **0.15** (filtered) — pruned.
    * **0.0** (filtered) — pruned.
    """

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_no_rerank(self, embedder):
        return RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

    async def _seed_band(
        self,
        session: AsyncSession,
        embedder,
        *,
        note_id: UUID,
        entity_id: UUID,
        vault_id: UUID,
        text: str,
        confidence: float,
    ) -> UUID:
        unit_id = uuid4()
        embedding = embedder.encode([text])[0].tolist()
        session.add(
            MemoryUnit(
                id=unit_id,
                note_id=note_id,
                text=text,
                fact_type=FactTypes.WORLD,
                embedding=embedding,
                vault_id=vault_id,
                event_date=datetime.now(timezone.utc),
                confidence=confidence,
            )
        )
        session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id))
        return unit_id

    @pytest_asyncio.fixture
    async def seeded_bands(self, session: AsyncSession, embedder):
        """Seed five confidence bands sharing one topic."""
        topic = 'Postgres replication lag tuning'
        note = Note(id=uuid4(), original_text='F48 test corpus', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        cold_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
            text='Postgres replication lag tuning fact one cold-start unit.',
            confidence=1.0,
        )
        mid_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
            text='Postgres replication lag tuning fact two mid confidence unit.',
            confidence=0.5,
        )
        boundary_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
            text='Postgres replication lag tuning fact three boundary unit.',
            confidence=0.2,
        )
        pruned_low_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
            text='Postgres replication lag tuning fact four contradicted unit.',
            confidence=0.15,
        )
        pruned_zero_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
            text='Postgres replication lag tuning fact five refuted unit.',
            confidence=0.0,
        )
        await session.commit()

        return {
            'cold': cold_id,
            'mid': mid_id,
            'boundary': boundary_id,
            'pruned_low': pruned_low_id,
            'pruned_zero': pruned_zero_id,
        }

    async def test_pre_filter_prunes_low_confidence_units(
        self, session: AsyncSession, engine_no_rerank, seeded_bands
    ):
        """``apply_pre_filter=True`` (default) prunes confidence < 0.2 and
        keeps cold-start + mid + boundary units.

        ``include_superseded=True`` is set on every request so the existing
        post-hydration ``superseded_threshold=0.3`` filter does not mask
        F48's effect. F48 lives upstream at the hydration layer; isolating
        it from the downstream superseded filter is the right test
        boundary."""
        request = RetrievalRequest(
            query='Postgres replication lag tuning',
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=True,
            include_superseded=True,
        )
        results, _ = await engine_no_rerank.retrieve(session, request)
        result_ids = {r.id for r in results}

        assert seeded_bands['cold'] in result_ids, (
            'F48 cold-start safeguard regressed — confidence=1.0 unit must '
            'NEVER be filtered (1.0 > 0.2 by an order of magnitude).'
        )
        assert seeded_bands['mid'] in result_ids, (
            'F48 over-prune: confidence=0.5 unit must survive (0.5 > 0.2).'
        )
        assert seeded_bands['boundary'] in result_ids, (
            'F48 strict-< invariant broken: confidence=0.2 (boundary) must '
            'survive — the spec settled on strict less-than (not <=).'
        )
        assert seeded_bands['pruned_low'] not in result_ids, (
            'F48 regression: confidence=0.15 unit must be pruned (< 0.2).'
        )
        assert seeded_bands['pruned_zero'] not in result_ids, (
            'F48 regression: confidence=0.0 unit must be pruned (< 0.2).'
        )

    async def test_apply_pre_filter_false_returns_pruned_units(
        self, session: AsyncSession, engine_no_rerank, seeded_bands
    ):
        """The audit / lineage / historical-routing escape hatch — F40's
        single-bypass flag also drops the F48 branch, so previously-pruned
        low-confidence units re-appear."""
        request = RetrievalRequest(
            query='Postgres replication lag tuning',
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=False,
            include_superseded=True,
        )
        results, _ = await engine_no_rerank.retrieve(session, request)
        result_ids = {r.id for r in results}

        for key in (
            'cold',
            'mid',
            'boundary',
            'pruned_low',
            'pruned_zero',
        ):
            assert seeded_bands[key] in result_ids, (
                f'apply_pre_filter=False must surface every band, including '
                f'{key!r}; missing from {result_ids}.'
            )


@pytest.mark.integration
class TestF48VaultScoping:
    """The F48 confidence filter must not leak between vaults. The
    predicate sits at the hydration layer downstream of the strategy
    layer's vault scoping — this test pins the composition."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_confidence_filter_respects_vault_scope(self, session: AsyncSession, embedder):
        """Two vaults; same low-confidence unit pattern in each. Searching
        one vault must not see the other's units, regardless of
        pre-filter state."""
        v_a = Vault(id=uuid4(), name='vault-a-f48')
        v_b = Vault(id=uuid4(), name='vault-b-f48')
        session.add(v_a)
        session.add(v_b)
        await session.flush()

        async def _seed_pair(vault_id: UUID, label: str) -> tuple[UUID, UUID]:
            note = Note(id=uuid4(), original_text=f'F48 vault {label}', vault_id=vault_id)
            entity = Entity(id=uuid4(), canonical_name=f'F48-Topic-{label}')
            session.add(note)
            session.add(entity)
            await session.flush()

            high_text = f'Vault {label} high-confidence well-validated guidance.'
            high_id = uuid4()
            session.add(
                MemoryUnit(
                    id=high_id,
                    note_id=note.id,
                    text=high_text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([high_text])[0].tolist(),
                    vault_id=vault_id,
                    event_date=datetime.now(timezone.utc),
                    confidence=1.0,
                )
            )
            session.add(UnitEntity(unit_id=high_id, entity_id=entity.id, vault_id=vault_id))

            low_text = f'Vault {label} low-confidence contradicted guidance.'
            low_id = uuid4()
            session.add(
                MemoryUnit(
                    id=low_id,
                    note_id=note.id,
                    text=low_text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([low_text])[0].tolist(),
                    vault_id=vault_id,
                    event_date=datetime.now(timezone.utc),
                    confidence=0.05,
                )
            )
            session.add(UnitEntity(unit_id=low_id, entity_id=entity.id, vault_id=vault_id))
            return high_id, low_id

        a_high, a_low = await _seed_pair(v_a.id, 'A')
        b_high, b_low = await _seed_pair(v_b.id, 'B')
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

        for apply_pre_filter in (True, False):
            results_a, _ = await engine.retrieve(
                session,
                RetrievalRequest(
                    query='vault contradicted guidance',
                    limit=20,
                    vault_ids=[v_a.id],
                    apply_pre_filter=apply_pre_filter,
                    include_superseded=True,
                ),
            )
            ids_a = {r.id for r in results_a}
            assert b_high not in ids_a and b_low not in ids_a, (
                f'vault-scoping violation under apply_pre_filter={apply_pre_filter}: '
                f"vault A search returned vault B's units."
            )


@pytest.mark.integration
class TestF48ExplorationBypass:
    """F44 — F33 exploration bypass must keep low-confidence units
    re-discoverable. Without this, F48 would make confidence monotonic
    once a unit dipped below 0.2 (no path back to re-validation)."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_low_confidence_unit_re_surfaces_via_exploration(
        self, session: AsyncSession, embedder
    ):
        """A unit with ``confidence < 0.2`` AND ``< 5 outcomes`` is the
        F48 ∩ F33 region: F48 prunes it from the main path, F33's
        exploration eligibility window covers it, and F44's separate
        hydration path bypasses F40+F48 — so the unit re-surfaces. Without
        the F44 bypass, F48 would silently shrink F33's candidate pool."""
        topic = 'Kafka consumer rebalance protocol'

        note = Note(id=uuid4(), original_text='F48 self-correction', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        # Filler high-confidence units to keep the result set crowded.
        for i in range(3):
            uid = uuid4()
            text = f'{topic} canonical pattern {i} (well-validated).'
            session.add(
                MemoryUnit(
                    id=uid,
                    note_id=note.id,
                    text=text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([text])[0].tolist(),
                    vault_id=GLOBAL_VAULT_ID,
                    event_date=datetime.now(timezone.utc),
                    success_co_count=20,
                    failure_co_count=0,
                    confidence=1.0,
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))

        # The F48 ∩ F33 target: low confidence (pruned by F48 main path),
        # zero outcomes (eligible for F33 exploration injection — F33's
        # threshold is total outcomes < 5).
        bypass_target_id = uuid4()
        text_bypass = f'{topic} ancient observation that contradiction-engine downgraded.'
        session.add(
            MemoryUnit(
                id=bypass_target_id,
                note_id=note.id,
                text=text_bypass,
                fact_type=FactTypes.WORLD,
                embedding=embedder.encode([text_bypass])[0].tolist(),
                vault_id=GLOBAL_VAULT_ID,
                event_date=datetime.now(timezone.utc),
                success_co_count=0,
                failure_co_count=0,
                confidence=0.05,
            )
        )
        session.add(
            UnitEntity(unit_id=bypass_target_id, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID)
        )
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=1.0,
                exploration_max_injections=2,
                exploration_low_mw_threshold=5,
                token_budget=0,
            ),
        )

        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(
                query=f'{topic} rebalance behaviour',
                limit=2,
                vault_ids=[GLOBAL_VAULT_ID],
                apply_pre_filter=True,
                include_superseded=True,
            ),
        )

        result_ids = {r.id for r in results}
        assert bypass_target_id in result_ids, (
            'F48 + F44 regression: a low-confidence unit pruned by F48 must '
            'still re-surface via the F33 exploration bypass when '
            'apply_pre_filter=True. Without this, F48 makes confidence '
            'monotonic — the unit can never re-validate via F33 self-correction.'
        )
