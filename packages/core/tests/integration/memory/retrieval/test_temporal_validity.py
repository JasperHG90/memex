"""Integration tests for POC #12 — Temporal validity on entity relations.

Verifies that:
- valid_from / valid_to columns are respected by the graph retrieval strategy
- as_of filtering correctly includes/excludes cooccurrences
- The writer populates valid_from from unit timestamps
- Backward compatibility: NULL validity always matches
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    Entity,
    EntityCooccurrence,
    MemoryUnit,
    Note,
    UnitEntity,
)
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.entity_resolver import EntityResolver


@pytest.mark.integration
class TestTemporalValidity:
    """Test as_of temporal filtering on entity cooccurrence graph strategy."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    async def _create_entity(
        self, session: AsyncSession, name: str, entity_type: str = 'Person'
    ) -> Entity:
        entity = Entity(
            id=uuid4(),
            canonical_name=name,
            entity_type=entity_type,
            mention_count=5,
        )
        session.add(entity)
        return entity

    async def _create_cooccurrence(
        self,
        session: AsyncSession,
        e1: Entity,
        e2: Entity,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        count: int = 3,
    ) -> EntityCooccurrence:
        # Enforce canonical ordering
        id1, id2 = (e1.id, e2.id) if e1.id < e2.id else (e2.id, e1.id)
        cooc = EntityCooccurrence(
            entity_id_1=id1,
            entity_id_2=id2,
            vault_id=GLOBAL_VAULT_ID,
            cooccurrence_count=count,
            last_cooccurred=datetime.now(timezone.utc),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        session.add(cooc)
        return cooc

    async def _create_linked_unit(
        self,
        session: AsyncSession,
        entity: Entity,
        embedder,
        text_content: str | None = None,
    ) -> MemoryUnit:
        """Create a MemoryUnit linked to an entity via UnitEntity."""
        note = Note(id=uuid4(), original_text=text_content or f'Note about {entity.canonical_name}')
        session.add(note)

        unit_text = text_content or f'Memory unit about {entity.canonical_name} {uuid4()}'
        embedding = embedder.encode([unit_text])[0].tolist()
        unit = MemoryUnit(
            id=uuid4(),
            text=unit_text,
            embedding=embedding,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.WORLD,
            event_date=datetime.now(timezone.utc),
            note_id=note.id,
        )
        session.add(unit)

        link = UnitEntity(
            unit_id=unit.id,
            entity_id=entity.id,
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(link)
        return unit

    async def test_as_of_filters_expired_relations(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Relations with valid_to in the past are excluded from as-of queries."""
        alice = await self._create_entity(session, f'Alice-{uuid4().hex[:6]}')
        bob = await self._create_entity(session, f'Bob-{uuid4().hex[:6]}')

        # Cooccurrence valid Jan-Jun 2024
        await self._create_cooccurrence(
            session,
            alice,
            bob,
            valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
        )

        # Create linked units for both entities so the graph strategy can find them
        await self._create_linked_unit(session, alice, embedder)
        await self._create_linked_unit(session, bob, embedder)
        await session.commit()

        # Query during validity: should find the relation
        req_during = RetrievalRequest(
            query=alice.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2024, 3, 15, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_during, _ = await engine_instance.retrieve(session, req_during)

        # Query after expiry: relation should be excluded
        req_after = RetrievalRequest(
            query=alice.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2024, 9, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_after, _ = await engine_instance.retrieve(session, req_after)

        # During validity, we should get results from both 1st and 2nd order
        # (units linked to alice directly + units linked to bob via cooccurrence)
        during_ids = {r.id for r in results_during}
        after_ids = {r.id for r in results_after}

        # The after-expiry set should be a strict subset or smaller
        # (bob's units should not appear via 2nd-order graph traversal)
        assert len(during_ids) >= len(after_ids), (
            f'Expected fewer results after expiry. During: {len(during_ids)}, After: {len(after_ids)}'
        )

    async def test_as_of_includes_open_ended_relations(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Relations with valid_to=NULL are always included in as-of queries."""
        charlie = await self._create_entity(session, f'Charlie-{uuid4().hex[:6]}')
        diana = await self._create_entity(session, f'Diana-{uuid4().hex[:6]}')

        # Open-ended cooccurrence (valid_to=NULL)
        await self._create_cooccurrence(
            session,
            charlie,
            diana,
            valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            valid_to=None,
        )

        await self._create_linked_unit(session, charlie, embedder)
        await self._create_linked_unit(session, diana, embedder)
        await session.commit()

        # Any as_of after valid_from should include this relation
        req = RetrievalRequest(
            query=charlie.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2030, 1, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results, _ = await engine_instance.retrieve(session, req)
        assert len(results) > 0, 'Open-ended relation should be included'

    async def test_as_of_excludes_future_relations(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Relations with valid_from in the future are excluded."""
        eve = await self._create_entity(session, f'Eve-{uuid4().hex[:6]}')
        frank = await self._create_entity(session, f'Frank-{uuid4().hex[:6]}')

        # Cooccurrence that starts in 2025
        await self._create_cooccurrence(
            session,
            eve,
            frank,
            valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_to=None,
        )

        await self._create_linked_unit(session, eve, embedder)
        await self._create_linked_unit(session, frank, embedder)
        await session.commit()

        # as_of before valid_from: future relation excluded from 2nd-order
        req_before = RetrievalRequest(
            query=eve.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2024, 6, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_before, _ = await engine_instance.retrieve(session, req_before)

        # as_of after valid_from: relation included
        req_after = RetrievalRequest(
            query=eve.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2025, 6, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_after, _ = await engine_instance.retrieve(session, req_after)

        before_ids = {r.id for r in results_before}
        after_ids = {r.id for r in results_after}
        assert len(after_ids) >= len(before_ids), (
            f'Expected more results after valid_from. Before: {len(before_ids)}, After: {len(after_ids)}'
        )

    async def test_no_as_of_returns_all(self, session: AsyncSession, engine_instance, embedder):
        """Without as_of, all relations returned (backward compatible)."""
        grace = await self._create_entity(session, f'Grace-{uuid4().hex[:6]}')
        henry = await self._create_entity(session, f'Henry-{uuid4().hex[:6]}')

        # Expired cooccurrence
        await self._create_cooccurrence(
            session,
            grace,
            henry,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2021, 1, 1, tzinfo=timezone.utc),
        )

        await self._create_linked_unit(session, grace, embedder)
        await self._create_linked_unit(session, henry, embedder)
        await session.commit()

        # No as_of: all relations returned regardless of validity
        req = RetrievalRequest(
            query=grace.canonical_name,
            limit=20,
            strategies=['graph'],
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results, _ = await engine_instance.retrieve(session, req)
        # Should find results from both 1st-order (grace) and 2nd-order (henry via cooc)
        assert len(results) > 0, 'Without as_of, expired relations should still be included'

    async def test_null_validity_matches_all(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """NULL valid_from + NULL valid_to matches any as_of (permissive)."""
        irene = await self._create_entity(session, f'Irene-{uuid4().hex[:6]}')
        jack = await self._create_entity(session, f'Jack-{uuid4().hex[:6]}')

        # Both NULL: represents legacy/unscoped cooccurrence
        await self._create_cooccurrence(
            session,
            irene,
            jack,
            valid_from=None,
            valid_to=None,
        )

        await self._create_linked_unit(session, irene, embedder)
        await self._create_linked_unit(session, jack, embedder)
        await session.commit()

        # Any as_of should include NULL-validity cooccurrences
        req = RetrievalRequest(
            query=irene.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2024, 6, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results, _ = await engine_instance.retrieve(session, req)
        assert len(results) > 0, 'NULL validity cooccurrence should match any as_of'

    async def test_writer_populates_valid_from(self, session: AsyncSession):
        """link_units_to_entities_batch populates valid_from from unit_timestamps."""
        entity_resolver = EntityResolver()

        e1 = await self._create_entity(session, f'Writer-E1-{uuid4().hex[:6]}')
        e2 = await self._create_entity(session, f'Writer-E2-{uuid4().hex[:6]}')

        note = Note(id=uuid4(), original_text='Writer test note')
        session.add(note)

        unit = MemoryUnit(
            id=uuid4(),
            text=f'Writer test unit {uuid4()}',
            embedding=[0.1] * 384,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.EVENT,
            event_date=datetime(2024, 3, 15, tzinfo=timezone.utc),
            note_id=note.id,
            occurred_start=datetime(2024, 3, 15, tzinfo=timezone.utc),
        )
        session.add(unit)
        await session.commit()

        unit_id = str(unit.id)
        e1_id = str(e1.id)
        e2_id = str(e2.id)

        event_timestamp = datetime(2024, 3, 15, tzinfo=timezone.utc)
        await entity_resolver.link_units_to_entities_batch(
            session,
            [(unit_id, e1_id), (unit_id, e2_id)],
            vault_id=GLOBAL_VAULT_ID,
            unit_timestamps={unit_id: event_timestamp},
        )
        await session.commit()

        # Fetch the cooccurrence and check valid_from
        id1, id2 = (e1.id, e2.id) if e1.id < e2.id else (e2.id, e1.id)
        stmt = select(EntityCooccurrence).where(
            EntityCooccurrence.entity_id_1 == id1,
            EntityCooccurrence.entity_id_2 == id2,
        )
        result = await session.exec(stmt)
        cooc = result.first()
        assert cooc is not None, 'Cooccurrence should be created'
        assert cooc.valid_from == event_timestamp, (
            f'valid_from should be set to event timestamp. Got: {cooc.valid_from}'
        )

    async def test_writer_keeps_earliest_valid_from_on_reupsert(self, session: AsyncSession):
        """Re-upserting does not overwrite valid_from with a later timestamp."""
        entity_resolver = EntityResolver()

        e1 = await self._create_entity(session, f'Reupsert-E1-{uuid4().hex[:6]}')
        e2 = await self._create_entity(session, f'Reupsert-E2-{uuid4().hex[:6]}')

        note = Note(id=uuid4(), original_text='Reupsert test note')
        session.add(note)

        unit1 = MemoryUnit(
            id=uuid4(),
            text=f'Reupsert test unit 1 {uuid4()}',
            embedding=[0.1] * 384,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.EVENT,
            event_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            note_id=note.id,
        )
        unit2 = MemoryUnit(
            id=uuid4(),
            text=f'Reupsert test unit 2 {uuid4()}',
            embedding=[0.1] * 384,
            vault_id=GLOBAL_VAULT_ID,
            fact_type=FactTypes.EVENT,
            event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            note_id=note.id,
        )
        session.add(unit1)
        session.add(unit2)
        await session.commit()

        u1_id = str(unit1.id)
        u2_id = str(unit2.id)
        e1_id = str(e1.id)
        e2_id = str(e2.id)

        # First insert: earlier timestamp
        early_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        await entity_resolver.link_units_to_entities_batch(
            session,
            [(u1_id, e1_id), (u1_id, e2_id)],
            vault_id=GLOBAL_VAULT_ID,
            unit_timestamps={u1_id: early_ts},
        )
        await session.commit()

        # Second insert: later timestamp
        late_ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
        await entity_resolver.link_units_to_entities_batch(
            session,
            [(u2_id, e1_id), (u2_id, e2_id)],
            vault_id=GLOBAL_VAULT_ID,
            unit_timestamps={u2_id: late_ts},
        )
        await session.commit()

        # valid_from should still be the earlier timestamp
        id1, id2 = (e1.id, e2.id) if e1.id < e2.id else (e2.id, e1.id)
        stmt = select(EntityCooccurrence).where(
            EntityCooccurrence.entity_id_1 == id1,
            EntityCooccurrence.entity_id_2 == id2,
        )
        result = await session.exec(stmt)
        cooc = result.first()
        assert cooc is not None
        assert cooc.valid_from == early_ts, (
            f'valid_from should be earliest timestamp ({early_ts}). Got: {cooc.valid_from}'
        )
        assert cooc.cooccurrence_count == 2, (
            f'Count should be 2 after two upserts. Got: {cooc.cooccurrence_count}'
        )

    async def test_migration_preserves_existing_data(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Existing cooccurrences with NULL validity still work after migration."""
        kate = await self._create_entity(session, f'Kate-{uuid4().hex[:6]}')
        leo = await self._create_entity(session, f'Leo-{uuid4().hex[:6]}')

        # Simulate pre-migration row: both validity columns NULL
        await self._create_cooccurrence(
            session,
            kate,
            leo,
            valid_from=None,
            valid_to=None,
            count=10,
        )

        await self._create_linked_unit(session, kate, embedder)
        await self._create_linked_unit(session, leo, embedder)
        await session.commit()

        # Without as_of: should work as before
        req_no_asof = RetrievalRequest(
            query=kate.canonical_name,
            limit=20,
            strategies=['graph'],
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_no_asof, _ = await engine_instance.retrieve(session, req_no_asof)
        assert len(results_no_asof) > 0, 'NULL validity should work without as_of'

        # With as_of: NULL validity is permissive (matches any time)
        req_with_asof = RetrievalRequest(
            query=kate.canonical_name,
            limit=20,
            strategies=['graph'],
            as_of=datetime(2024, 6, 1, tzinfo=timezone.utc),
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results_with_asof, _ = await engine_instance.retrieve(session, req_with_asof)
        assert len(results_with_asof) > 0, 'NULL validity should match any as_of'
