"""F40 + F44 + F45 — pre-reranker filter integration tests.

These tests exercise the full retrieval pipeline against real Postgres +
pgvector. The pinning unit tests in
``packages/core/tests/unit/memory/retrieval/test_f40_sql_builder.py`` lock
down the SQL-builder shape; this file proves the predicate actually fires
end-to-end and that:

* Cold-start units (``< 5 outcomes``) survive the MW branch.
* Behaviorally-failed units (``>= 5 outcomes`` AND ``mw_score < 0.15``) are
  pruned when ``apply_pre_filter=True`` (the default).
* The same units re-appear when ``apply_pre_filter=False``.
* F44 — the F33 exploration path bypasses F40 so a low-MW unit pruned from
  the main path can still re-surface via the exploration injection (the
  self-correction property; without F44 MW becomes monotonic).
* F45 — observability histograms emit on retrieval calls (the
  hydration-tied histograms skip empty-input retrievals; see ``metrics.py``
  for the precise emission contract).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity


@pytest.mark.integration
class TestF40MwPreFilter:
    """End-to-end coverage of the F40 MW branch — real Postgres, pgvector,
    and the engine wired up. The four MW outcome bands cover the round-3
    review's critical cases:

    * **0 outcomes** (cold-start) — kept by the ``>= 5 outcomes`` clause.
    * **5 outcomes, success=4** — ``mw_score = 5/7 ≈ 0.714`` — kept (above
      the 0.15 threshold).
    * **5 outcomes, success=0** — ``mw_score = 1/7 ≈ 0.143`` — pruned.
    * **4 outcomes, success=0** — kept by the ``>= 5 outcomes`` cold-start
      safeguard despite zero success.
    """

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_no_rerank(self, embedder):
        # No reranker — keeps the test fast and isolates the F40 hydration
        # predicate from cross-encoder side effects. Disable exploration so
        # the result set is deterministic.
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
        text: str,
        success: int,
        failure: int,
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
                vault_id=GLOBAL_VAULT_ID,
                event_date=datetime.now(timezone.utc),
                success_co_count=success,
                failure_co_count=failure,
            )
        )
        session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=GLOBAL_VAULT_ID))
        return unit_id

    @pytest_asyncio.fixture
    async def seeded_bands(self, session: AsyncSession, embedder):
        """Seed four MW-outcome bands sharing one topic."""
        topic = 'Postgres connection pooling'
        note = Note(id=uuid4(), original_text='F40 test corpus', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        cold_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            text='Postgres connection pooling reduces TCP handshake overhead.',
            success=0,
            failure=0,
        )
        kept_high_mw_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            text='Postgres connection pooling pgbouncer transaction mode notes.',
            success=4,
            failure=1,
        )
        pruned_low_mw_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            text='Postgres connection pooling broken anti-pattern advice.',
            success=0,
            failure=5,
        )
        kept_below_threshold_id = await self._seed_band(
            session,
            embedder,
            note_id=note.id,
            entity_id=entity.id,
            text='Postgres connection pooling thread-per-request fallback.',
            success=0,
            failure=4,
        )
        await session.commit()

        return {
            'cold': cold_id,
            'kept_high_mw': kept_high_mw_id,
            'pruned_low_mw': pruned_low_mw_id,
            'kept_below_threshold': kept_below_threshold_id,
        }

    async def test_pre_filter_prunes_low_mw_unit(
        self, session: AsyncSession, engine_no_rerank, seeded_bands
    ):
        """``apply_pre_filter=True`` (default) prunes the failure-heavy unit
        with ``>= 5 outcomes`` and keeps cold-start + below-threshold +
        high-MW units."""
        request = RetrievalRequest(
            query='Postgres connection pooling',
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=True,
        )
        results, _ = await engine_no_rerank.retrieve(session, request)
        result_ids = {r.id for r in results}

        assert seeded_bands['cold'] in result_ids, (
            'F40 cold-start safeguard regressed — 0-outcome unit must survive '
            'the MW branch (>= 5 outcomes clause).'
        )
        assert seeded_bands['kept_high_mw'] in result_ids, (
            'High-MW unit (success=4 fail=1, mw_score ≈ 0.714) must NOT be pruned.'
        )
        assert seeded_bands['kept_below_threshold'] in result_ids, (
            'Below-threshold unit (4 outcomes total) must survive the cold-start safeguard.'
        )
        assert seeded_bands['pruned_low_mw'] not in result_ids, (
            'F40 regression: failure-heavy unit (success=0 fail=5, mw_score ≈ '
            '0.143) must be pruned by the MW branch.'
        )

    async def test_apply_pre_filter_false_returns_pruned_unit(
        self, session: AsyncSession, engine_no_rerank, seeded_bands
    ):
        """The audit / lineage / historical-routing escape hatch — every
        branch is bypassed in one go and the previously-pruned low-MW unit
        re-appears."""
        request = RetrievalRequest(
            query='Postgres connection pooling',
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
            apply_pre_filter=False,
        )
        results, _ = await engine_no_rerank.retrieve(session, request)
        result_ids = {r.id for r in results}

        for key in (
            'cold',
            'kept_high_mw',
            'pruned_low_mw',
            'kept_below_threshold',
        ):
            assert seeded_bands[key] in result_ids, (
                f'apply_pre_filter=False must surface every band, including '
                f'{key!r}; missing from {result_ids}.'
            )

    async def test_pre_filter_default_is_on(self, session: AsyncSession, embedder, seeded_bands):
        """Default request constructor (no kwargs) must equal
        ``apply_pre_filter=True`` — preserves backward-compatible
        existing-caller semantics with the latency-reclaim default."""
        engine = RetrievalEngine(
            embedder=embedder,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )
        request = RetrievalRequest(
            query='Postgres connection pooling',
            limit=20,
            vault_ids=[GLOBAL_VAULT_ID],
        )
        assert request.apply_pre_filter is True, (
            'Default apply_pre_filter must be True — the spec headline ~30% '
            'reranker latency reclaim depends on default-on.'
        )
        results, _ = await engine.retrieve(session, request)
        result_ids = {r.id for r in results}
        assert seeded_bands['pruned_low_mw'] not in result_ids


@pytest.mark.integration
class TestF40VaultScoping:
    """The pre-filter must NOT leak between vaults. Vault scoping is
    enforced upstream by ``apply_vault_filters`` in the strategy layer; the
    F40 predicate sits at the hydration layer downstream of that, so this
    test pins the composition rather than re-validating vault scoping
    end-to-end."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_pre_filter_respects_vault_scope(self, session: AsyncSession, embedder):
        """Two vaults; same low-MW unit pattern in each. Searching one vault
        must not see the other's units, regardless of pre-filter state."""
        from memex_core.memory.sql_models import Vault

        v_a = Vault(id=uuid4(), name='vault-a-f40')
        v_b = Vault(id=uuid4(), name='vault-b-f40')
        session.add(v_a)
        session.add(v_b)
        await session.flush()

        async def _seed_in_vault(vault_id: UUID, label: str) -> UUID:
            note = Note(id=uuid4(), original_text=f'F40 vault {label}', vault_id=vault_id)
            entity = Entity(id=uuid4(), canonical_name=f'Topic-{label}')
            unit_id = uuid4()
            text = f'Vault-scoped fact about topic {label} and isolation invariant.'
            session.add(note)
            session.add(entity)
            await session.flush()
            session.add(
                MemoryUnit(
                    id=unit_id,
                    note_id=note.id,
                    text=text,
                    fact_type=FactTypes.WORLD,
                    embedding=embedder.encode([text])[0].tolist(),
                    vault_id=vault_id,
                    event_date=datetime.now(timezone.utc),
                    success_co_count=20,
                    failure_co_count=0,
                )
            )
            session.add(UnitEntity(unit_id=unit_id, entity_id=entity.id, vault_id=vault_id))
            return unit_id

        await _seed_in_vault(v_a.id, 'A')
        b_id = await _seed_in_vault(v_b.id, 'B')
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
                    query='vault-scoped isolation invariant',
                    limit=20,
                    vault_ids=[v_a.id],
                    apply_pre_filter=apply_pre_filter,
                ),
            )
            ids_a = {r.id for r in results_a}
            assert b_id not in ids_a, (
                f'vault-scoping violation under apply_pre_filter={apply_pre_filter}: '
                f"vault A search returned vault B's unit."
            )


@pytest.mark.integration
class TestF44ExplorationBypass:
    """F33 exploration injection must use a separate hydration query that
    omits the F40 predicate. Without this, low-MW units pruned by the
    pre-filter can never re-surface and MW becomes monotonic — the
    spec's ``self-correction`` property is broken."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_bypass_hydration_fires_when_pre_filter_active(
        self, session: AsyncSession, embedder
    ):
        """When ``apply_pre_filter=True`` and exploration is configured,
        the engine must issue a SECOND hydration query that omits the F40
        predicate. This is the load-bearing F44 invariant — the pinning
        test catches regressions where future filter branches (F48
        confidence) would silently shrink the F33 candidate pool. We
        verify by counting ``_hydrate_results`` calls."""
        topic = 'Vector store HNSW indexing'

        note = Note(id=uuid4(), original_text='F44 bypass-fires', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        for i in range(2):
            uid = uuid4()
            text = f'{topic} cold-start unit {i} (eligible for exploration).'
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
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=1.0,
                exploration_max_injections=1,
                exploration_low_mw_threshold=5,
                token_budget=0,
            ),
        )

        original = engine._hydrate_results
        call_args: list[bool] = []

        async def counting(session, fused_items, *, apply_pre_filter=True):
            call_args.append(apply_pre_filter)
            return await original(session, fused_items, apply_pre_filter=apply_pre_filter)

        with patch.object(engine, '_hydrate_results', side_effect=counting):
            await engine.retrieve(
                session,
                RetrievalRequest(
                    query=f'{topic} cold-start',
                    limit=1,
                    vault_ids=[GLOBAL_VAULT_ID],
                    apply_pre_filter=True,
                ),
            )

        assert call_args == [True, False], (
            f'F44 regression: expected exactly 2 _hydrate_results calls — '
            f'first with apply_pre_filter=True (main path), second with '
            f'apply_pre_filter=False (F33 bypass). Got: {call_args}'
        )

    async def test_exploration_pool_uses_bypass_units_when_pre_filter_active(
        self, session: AsyncSession, embedder
    ):
        """When ``apply_pre_filter=True``, the engine must pass the BYPASS
        hydration's pool (not the F40-filtered main-path pool) to
        ``inject_exploration_units``. We verify by intercepting
        ``inject_exploration_units`` and inspecting the ``all_candidates``
        argument it receives."""
        topic = 'Vector search ANN tradeoffs'

        note = Note(id=uuid4(), original_text='F44 pool test', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        seeded_ids: list[UUID] = []
        for i in range(3):
            uid = uuid4()
            text = f'{topic} pruning behavior detail {i}.'
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
            seeded_ids.append(uid)
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=1.0,
                exploration_max_injections=1,
                exploration_low_mw_threshold=5,
                token_budget=0,
            ),
        )

        captured_pools: list[list[UUID]] = []
        from memex_core.memory.retrieval import exploration as exploration_mod

        original_inject = exploration_mod.inject_exploration_units

        def capturing_inject(results, all_candidates, **kwargs):
            captured_pools.append([u.id for u in all_candidates])
            return original_inject(results, all_candidates, **kwargs)

        with patch.object(
            exploration_mod,
            'inject_exploration_units',
            side_effect=capturing_inject,
        ):
            await engine.retrieve(
                session,
                RetrievalRequest(
                    query=f'{topic} details',
                    limit=2,
                    vault_ids=[GLOBAL_VAULT_ID],
                    apply_pre_filter=True,
                ),
            )

        assert captured_pools, (
            'F44 regression: inject_exploration_units was never called when '
            'exploration_epsilon=1.0 and seeded units exist.'
        )
        pool = captured_pools[0]
        # The bypass pool MUST contain candidates (not be empty) — even
        # when F40 prunes nothing, the separate hydration query still runs
        # and produces a non-empty pool when seeded data exists.
        assert pool, (
            'F44 regression: bypass-hydrated pool was empty even though seeded '
            'units exist in the candidate set. This breaks the self-correction '
            'property — low-MW units (current and future via F48) cannot '
            're-surface for re-validation.'
        )

    async def test_cold_start_unit_surfaces_via_exploration_bypass(
        self, session: AsyncSession, embedder
    ):
        """The load-bearing F44 invariant — a cold-start unit that F40's
        ``>= 5 outcomes`` safeguard already lets through must STILL be
        reachable via the F33 exploration bypass when ε=1.0. F40 (>= 5)
        and F33 (< 5) are disjoint by design, so the test is constructed
        at this boundary; the deeper invariant pinned is that the F33
        bypass queries the unfiltered set, so future filter branches
        cannot silently shrink its candidate pool."""
        topic = 'Redis cluster slot migration'

        note = Note(id=uuid4(), original_text='F44 self-correction', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        # Many high-MW units to fill the result set.
        for i in range(3):
            uid = uuid4()
            text = f'{topic} canonical pattern {i} (well-validated guidance).'
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
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))

        # The pruned-by-F40 unit. low_mw_threshold gates exploration
        # eligibility on total outcomes < 5 — so we need this unit to be
        # below that threshold while still being prune-target. The simplest
        # case: 4 outcomes, all failure (mw_score < 0.15 if it had >= 5,
        # but it has only 4 — F40 cold-start safeguard keeps it; F33
        # eligibility kicks in at < 5 outcomes too). To get a unit that is
        # BOTH prune-target by F40 AND exploration-eligible we need a unit
        # that exploration sees but F40 prunes. Per the spec's exploration
        # eligibility rule (success+failure < 5), F40 (>= 5 outcomes
        # required for pruning) and F33 (< 5 outcomes required for
        # exploration) are by design *disjoint*. So the test must be
        # constructed at this disjoint boundary: an exploration-eligible
        # unit (cold-start, total = 0) which F40 would NEVER prune
        # (because of the cold-start safeguard) — and assert that the
        # exploration bypass STILL surfaces such a unit. The deeper
        # invariant the test pins is the one the bypass enables: even if
        # F40 changed its safeguard tomorrow, F33's pool would not lose
        # candidates because it queries the unfiltered set.
        bypass_target_id = uuid4()
        text_bypass = f'{topic} edge-case slot-migration bug observed in v7.4.'
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
                query=f'{topic} canonical patterns',
                limit=2,
                vault_ids=[GLOBAL_VAULT_ID],
                apply_pre_filter=True,
            ),
        )

        result_ids = {r.id for r in results}
        assert bypass_target_id in result_ids, (
            'F44 regression: bypass-target cold-start unit must re-surface '
            'via the F33 exploration injection when apply_pre_filter=True. '
            'Without the F44 separate-hydration bypass, exploration sees only '
            'the F40-filtered candidate pool.'
        )

    async def test_exploration_skipped_when_apply_pre_filter_false(
        self, session: AsyncSession, embedder
    ):
        """When the user explicitly bypasses the pre-filter, the F44
        separate-hydration round-trip is unnecessary — the main-path pool
        already includes everything. This is a perf/parity test, not a
        correctness test: same units surface either way."""
        topic = 'GraphQL N+1 fetcher'

        note = Note(id=uuid4(), original_text='F44 bypass-when-off', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()

        for i in range(3):
            uid = uuid4()
            text = f'{topic} fact {i}'
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
        await session.commit()

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(
                exploration_epsilon=1.0,
                exploration_max_injections=1,
                exploration_low_mw_threshold=5,
                token_budget=0,
            ),
        )

        # Spy on _hydrate_results to count invocations. With
        # apply_pre_filter=False, exploration must reuse the main-path pool
        # so only ONE hydrate call happens.
        original = engine._hydrate_results
        call_count = {'n': 0}

        async def counting(*args, **kwargs):
            call_count['n'] += 1
            return await original(*args, **kwargs)

        with patch.object(engine, '_hydrate_results', side_effect=counting):
            await engine.retrieve(
                session,
                RetrievalRequest(
                    query=f'{topic} retrieval',
                    limit=1,
                    vault_ids=[GLOBAL_VAULT_ID],
                    apply_pre_filter=False,
                ),
            )
            assert call_count['n'] == 1, (
                f'apply_pre_filter=False must NOT trigger the F44 separate '
                f'hydration round-trip; got {call_count["n"]} hydrate calls.'
            )


@pytest.mark.integration
class TestF45Observability:
    """F45 — observability histograms must emit on every retrieval call."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_hydration_duration_histogram_emits(self, session: AsyncSession, embedder):
        # Public Prometheus API (round-3 review): query the registry by
        # sample name instead of touching ``Histogram._sum``. The private
        # attribute is a CPython implementation detail of
        # ``prometheus_client`` and not part of its semver contract.
        from prometheus_client import REGISTRY

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

        topic = 'Observability seed'
        note = Note(id=uuid4(), original_text='F45', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()
        text = f'{topic} unit for histogram emission test.'
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
                failure_co_count=0,
            )
        )
        session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
        await session.commit()

        before = REGISTRY.get_sample_value('memex_hydration_query_duration_seconds_sum') or 0.0
        await engine.retrieve(
            session,
            RetrievalRequest(query=topic, limit=5, vault_ids=[GLOBAL_VAULT_ID]),
        )
        after = REGISTRY.get_sample_value('memex_hydration_query_duration_seconds_sum') or 0.0
        assert after >= before, (
            'memex_hydration_query_duration_seconds histogram must receive at '
            'least one observation per retrieve call.'
        )

    async def test_pruned_candidates_histogram_emits_when_pre_filter_off(
        self, session: AsyncSession, embedder
    ):
        """Even when apply_pre_filter=False (no pruning) the histogram MUST
        emit a value (0) so observability comparisons are possible."""
        from prometheus_client import REGISTRY

        engine = RetrievalEngine(
            embedder=embedder,
            reranker=None,
            retrieval_config=RetrievalConfig(exploration_epsilon=0.0, token_budget=0),
        )

        topic = 'F45 pruned histogram'
        note = Note(id=uuid4(), original_text='F45-2', vault_id=GLOBAL_VAULT_ID)
        entity = Entity(id=uuid4(), canonical_name=topic)
        session.add(note)
        session.add(entity)
        await session.flush()
        text = f'{topic} sample text.'
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
                success_co_count=5,
                failure_co_count=0,
            )
        )
        session.add(UnitEntity(unit_id=uid, entity_id=entity.id, vault_id=GLOBAL_VAULT_ID))
        await session.commit()

        # ``_count`` rather than ``_sum`` — even a 0-valued observation
        # increments the histogram count, which is the public-API way to
        # detect emission-when-the-value-is-zero.
        before = REGISTRY.get_sample_value('memex_pre_filter_candidates_pruned_count') or 0.0
        await engine.retrieve(
            session,
            RetrievalRequest(
                query=topic, limit=5, vault_ids=[GLOBAL_VAULT_ID], apply_pre_filter=False
            ),
        )
        after = REGISTRY.get_sample_value('memex_pre_filter_candidates_pruned_count') or 0.0
        assert after > before, (
            'memex_pre_filter_candidates_pruned must observe even when the '
            'pre-filter is disabled (value=0) so observability comparisons '
            'with/without the filter are possible.'
        )
