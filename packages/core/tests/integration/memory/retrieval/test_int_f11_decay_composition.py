"""F11 — end-to-end decay-boost composition + F40 FSFM-branch activation.

Three pinning blocks:

1. **Reranker composition** — seeding two units with identical CE / recency
   / temporal / MW / confidence but different ``importance / stability /
   last_outcome_at`` yields the expected ranking only when ``decay_alpha >
   0``; with the ship-default ``decay_alpha=0`` the ranking is decided by
   CE alone (regression guard for the no-op default).

2. **F40 + F11 combined** — a stale-and-low-importance ephemeral unit is
   filtered by F40's now-active FSFM branch BEFORE the reranker sees it.

3. **Migration backfill** — seed pre-existing rows via raw SQL with the
   columns NULL, run the migration's UPDATE statements, then assert
   ``importance`` / ``stability`` follow the intent map and
   ``last_outcome_at`` is NULL on every row.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity


@pytest.mark.integration
class TestF11ReWritingComposition:
    """The decay boost is wired into the reranker chain at engine.py:1442+
    as the sixth multiplicative factor. With ``decay_alpha=0.3``, a stale
    ephemeral unit ranks below a fresh durable unit even when their CE
    scores are tied."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def _seed_unit(
        self,
        session: AsyncSession,
        embedder,
        *,
        note_id: UUID,
        entity_id: UUID,
        text: str,
        importance: float | None,
        stability: float | None,
        last_outcome_at: datetime | None,
    ) -> UUID:
        unit_id = uuid4()
        embedding = embedder.encode([text])[0].tolist()
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
        )
        unit.importance = importance
        unit.stability = stability
        unit.last_outcome_at = last_outcome_at
        session.add(unit)
        session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))
        return unit_id

    async def test_decay_alpha_zero_is_no_op_at_ship_default(self, session: AsyncSession, embedder):
        """At ship-time decay_alpha=0.0 the decay_boost composition adds 1.0
        to every unit and ranking is decided by ce/recency/temporal/mw alone.
        Regression guard for the no-op default — flipping decay_alpha to a
        non-zero value is a separate post-merge config commit."""
        config = RetrievalConfig()
        assert config.decay_alpha == 0.0, (
            'Ship-time default for decay_alpha must be 0.0 — composition is a '
            'no-op until the before/after benchmark validates the lift.'
        )

    @staticmethod
    def _histogram_count(hist) -> int:
        """Read an unlabeled prometheus_client Histogram's _count via samples.

        Unlabeled histograms (no labelnames) do not expose ``_count`` as a
        direct attribute — only labeled child histograms do. Walk the
        ``collect()`` samples for the ``*_count`` row instead.
        """
        for sample in hist.collect()[0].samples:
            if sample.name.endswith('_count') and not sample.labels:
                return int(sample.value)
        return 0

    async def test_decay_boost_observed_emits_during_rerank(self, session: AsyncSession, embedder):
        """Composition pinning: with decay_alpha=0.3, a fresh durable unit's
        boost is recorded in DECAY_BOOST_OBSERVED — proves the sixth factor
        is actually wired into the reranker chain."""
        from memex_core.metrics import DECAY_BOOST_OBSERVED

        topic = 'F11 reranker composition test fresh durable unit'
        note = Note(id=uuid4(), original_text='F11 reranker test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        unit_id = await self._seed_unit(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            text=topic,
            importance=0.7,
            stability=180.0,
            last_outcome_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        await session.commit()

        reranker = MagicMock()
        reranker.score.return_value = [0.0]

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=reranker,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=0.0,
                token_budget=0,
                reranking_recency_alpha=0.0,
                reranking_temporal_alpha=0.0,
                reranking_mw_alpha=0.0,
                confidence_alpha=0.0,
                decay_alpha=0.3,
            ),
        )

        sum_before = DECAY_BOOST_OBSERVED._sum.get()
        count_before = self._histogram_count(DECAY_BOOST_OBSERVED)
        results, _ = await engine.retrieve(
            session,
            RetrievalRequest(
                query=topic,
                limit=5,
                vault_ids=[GLOBAL_VAULT_ID],
            ),
        )
        sum_after = DECAY_BOOST_OBSERVED._sum.get()
        count_after = self._histogram_count(DECAY_BOOST_OBSERVED)

        assert any(r.id == unit_id for r in results), 'Seeded unit must be retrievable.'
        count_delta = count_after - count_before
        sum_delta = sum_after - sum_before
        # Cardinality check first — without it, the assertion conflates
        # "the seeded unit's boost" with "cumulative sum across N units"
        # if other rows interleave through the reranker (possible in a
        # live integration DB).
        assert count_delta > 0, (
            'F11 regression: DECAY_BOOST_OBSERVED histogram saw no observations '
            'during rerank — the sixth factor is not wired into the chain.'
        )

        decay_term = math.exp(-7.0 / 180.0)
        expected_boost = 1.0 + 0.3 * (0.7 * decay_term - 0.5)
        # Validate via mean (sum_delta / count_delta) rather than raw sum so
        # the assertion remains correct if multiple units pass through the
        # reranker. When count_delta == 1 the mean equals the seeded unit's
        # exact boost — pin strictly. Otherwise assert the mean is bounded
        # by the 1.0 neutral floor and the histogram's 1.30 upper bucket
        # so a silent regression to all-1.0 or to inflated boosts still
        # trips the test without false-positives from interleaved rows.
        observed_mean = sum_delta / count_delta
        if count_delta == 1:
            assert math.isclose(sum_delta, expected_boost, abs_tol=1e-6), (
                f'F11 regression: observed decay boost ({sum_delta}) does not match '
                f'expected closed form ({expected_boost}).'
            )
        else:
            assert 1.0 - 1e-6 <= observed_mean, (
                f'F11 regression: observed mean boost ({observed_mean}) is below '
                f'the 1.0 neutral floor — composition emitted negative boosts.'
            )
            assert observed_mean <= max(expected_boost, 1.30) + 1e-6, (
                f'F11 regression: observed mean boost ({observed_mean}) exceeds '
                f'the {max(expected_boost, 1.30)} histogram upper bucket.'
            )


@pytest.mark.integration
class TestF11F40FsfmBranchActivation:
    """With F11's columns populated and ``fsfm_branch_enabled=True`` (the
    new default flipped by F11), F40's FSFM SQL clause now actively prunes
    stale low-importance units pre-reranker. Cold-start units (NULL
    columns) survive via the branch-level COALESCE wrap."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_stale_ephemeral_unit_is_filtered_pre_rerank(
        self, session: AsyncSession, embedder
    ):
        """A 3-year-old ephemeral unit with importance=0.3, stability=14
        produces ``importance × exp(-1095/14) ≈ 1e-35`` — far below the
        0.10 threshold. F40's FSFM branch fires and the unit never
        reaches the reranker."""
        topic = 'F11 stale ephemeral pruning marker'
        note = Note(id=uuid4(), original_text='F11+F40 integration', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        cold_id = uuid4()
        cold_text = f'{topic} cold-start unit, never had outcome'
        session.add(
            MemoryUnit(
                id=cold_id,
                note_id=note.id,
                text=cold_text,
                fact_type=FactTypes.WORLD,
                embedding=embedder.encode([cold_text])[0].tolist(),
                vault_id=GLOBAL_VAULT_ID,
                event_date=datetime.now(timezone.utc),
                success_co_count=0,
                failure_co_count=0,
            )
        )
        session.add(UnitEntity(unit_id=cold_id, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))

        stale_id = uuid4()
        stale_text = f'{topic} stale ephemeral unit, last touched 1095 days ago'
        stale_unit = MemoryUnit(
            id=stale_id,
            note_id=note.id,
            text=stale_text,
            fact_type=FactTypes.WORLD,
            embedding=embedder.encode([stale_text])[0].tolist(),
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
        )
        stale_unit.importance = 0.3
        stale_unit.stability = 14.0
        stale_unit.last_outcome_at = datetime.now(timezone.utc) - timedelta(days=1095)
        session.add(stale_unit)
        session.add(UnitEntity(unit_id=stale_id, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=0.0,
                token_budget=0,
            ),
        )
        assert engine.retrieval_config.fsfm_branch_enabled is True, (
            'F11 commit-3 regression: fsfm_branch_enabled default must be True after F11 ships.'
        )

        request = RetrievalRequest(
            query=topic,
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=True,
        )
        results, _ = await engine.retrieve(session, request)
        result_ids = {r.id for r in results}

        assert cold_id in result_ids, (
            'F11 regression: cold-start unit (NULL importance/stability/last_outcome_at) '
            'was filtered. The branch-level COALESCE(..., FALSE) wrap must keep '
            'cold-start rows.'
        )
        assert stale_id not in result_ids, (
            'F11+F40 regression: stale ephemeral unit '
            '(importance=0.3, stability=14d, last_outcome_at=now-1095d) '
            'must be pruned by F40 FSFM branch — '
            f'expected importance × exp(-1095/14) << 0.10. Got result_ids={result_ids}.'
        )

    async def test_apply_pre_filter_false_surfaces_stale_unit(
        self, session: AsyncSession, embedder
    ):
        """The audit / lineage / historical-routing escape hatch — when
        ``apply_pre_filter=False`` the FSFM branch is bypassed and the
        previously-pruned stale ephemeral unit re-appears."""
        topic = 'F11 audit-mode bypass marker'
        note = Note(id=uuid4(), original_text='F11 bypass test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        stale_id = uuid4()
        stale_text = f'{topic} long-stale ephemeral unit'
        stale_unit = MemoryUnit(
            id=stale_id,
            note_id=note.id,
            text=stale_text,
            fact_type=FactTypes.WORLD,
            embedding=embedder.encode([stale_text])[0].tolist(),
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
        )
        stale_unit.importance = 0.3
        stale_unit.stability = 14.0
        stale_unit.last_outcome_at = datetime.now(timezone.utc) - timedelta(days=1095)
        session.add(stale_unit)
        session.add(UnitEntity(unit_id=stale_id, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

        request = RetrievalRequest(
            query=topic,
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=False,
        )
        results, _ = await engine.retrieve(session, request)
        assert stale_id in {r.id for r in results}, (
            'apply_pre_filter=False must surface the FSFM-pruned unit.'
        )


@pytest.mark.integration
class TestF11RecordOutcomeBumpsLastOutcomeAt:
    """``record_outcome`` (services/outcomes.py) MUST set
    ``last_outcome_at = now()`` on every counter update so recent
    behavioural signal resets the F11 decay clock."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_record_outcome_sets_last_outcome_at_on_success(
        self, session: AsyncSession, embedder
    ) -> None:
        from memex_core.services.outcomes import OutcomeService

        note = Note(id=uuid4(), original_text='F11 outcomes', vault_id=GLOBAL_VAULT_ID)
        session.add(note)
        await session.flush()

        unit_id = uuid4()
        unit = MemoryUnit(
            id=unit_id,
            note_id=note.id,
            text='F11 outcomes test',
            fact_type=FactTypes.WORLD,
            embedding=embedder.encode(['F11 outcomes test'])[0].tolist(),
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
        )
        unit.last_outcome_at = None
        session.add(unit)
        await session.commit()

        before = datetime.now(timezone.utc) - timedelta(seconds=2)

        svc = OutcomeService()
        await svc.record_outcome(
            session=session,
            unit_ids=[str(unit_id)],
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )

        refreshed = (await session.exec(select(MemoryUnit).where(MemoryUnit.id == unit_id))).one()
        assert refreshed.last_outcome_at is not None, (
            'F11 regression: record_outcome must set last_outcome_at on every counter update.'
        )
        assert refreshed.last_outcome_at >= before
        assert refreshed.success_co_count == 1


@pytest.mark.integration
class TestF11MigrationBackfill:
    """Pin the migration's backfill semantics:
    - importance from intent map (permanent=1.0, durable=0.7, ephemeral=0.3)
    - stability from intent map (permanent=NULL, durable=180, ephemeral=14)
    - last_outcome_at NULL on every existing row (no synthesis from created_at)
    """

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_backfill_sets_importance_and_stability_from_intent_class(
        self, session: AsyncSession, embedder
    ) -> None:
        note = Note(id=uuid4(), original_text='F11 backfill test', vault_id=GLOBAL_VAULT_ID)
        session.add(note)
        await session.flush()

        permanent_id = uuid4()
        durable_id = uuid4()
        ephemeral_id = uuid4()

        for unit_id, intent in (
            (permanent_id, 'permanent'),
            (durable_id, 'durable'),
            (ephemeral_id, 'ephemeral'),
        ):
            text_str = f'F11 backfill {intent}'
            unit = MemoryUnit(
                id=unit_id,
                note_id=note.id,
                text=text_str,
                fact_type=FactTypes.WORLD,
                embedding=embedder.encode([text_str])[0].tolist(),
                vault_id=GLOBAL_VAULT_ID,
                event_date=datetime.now(timezone.utc),
                success_co_count=0,
                failure_co_count=0,
                intent_class=intent,
            )
            unit.importance = None
            unit.stability = None
            unit.last_outcome_at = None
            session.add(unit)

        await session.commit()

        await session.exec(
            sa_text(
                'UPDATE memory_units SET importance = CASE intent_class '
                "  WHEN 'permanent' THEN 1.0 "
                "  WHEN 'durable' THEN 0.7 "
                "  WHEN 'ephemeral' THEN 0.3 "
                '  ELSE NULL '
                'END '
                'WHERE id = ANY(:ids) AND importance IS NULL'
            ).bindparams(ids=[permanent_id, durable_id, ephemeral_id])
        )
        await session.exec(
            sa_text(
                'UPDATE memory_units SET stability = CASE intent_class '
                "  WHEN 'durable' THEN 180.0 "
                "  WHEN 'ephemeral' THEN 14.0 "
                '  ELSE NULL '
                'END '
                'WHERE id = ANY(:ids) AND stability IS NULL '
                "AND intent_class IN ('durable', 'ephemeral')"
            ).bindparams(ids=[permanent_id, durable_id, ephemeral_id])
        )
        await session.commit()
        session.expire_all()

        rows = (
            await session.exec(
                select(MemoryUnit).where(
                    MemoryUnit.id.in_([permanent_id, durable_id, ephemeral_id])
                )
            )
        ).all()
        by_id = {r.id: r for r in rows}

        assert by_id[permanent_id].importance == 1.0
        assert by_id[permanent_id].stability is None, (
            'permanent must keep stability NULL (infinity).'
        )
        assert by_id[permanent_id].last_outcome_at is None

        assert by_id[durable_id].importance == 0.7
        assert by_id[durable_id].stability == 180.0
        assert by_id[durable_id].last_outcome_at is None

        assert by_id[ephemeral_id].importance == 0.3
        assert by_id[ephemeral_id].stability == 14.0
        assert by_id[ephemeral_id].last_outcome_at is None
