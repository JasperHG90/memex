"""Integration tests for issue #92 — server-side intent_class / risk_class filter.

Verifies that intent_class and risk_class filters apply at the SQL level so the
search HTTP endpoint returns ONLY matching memory units (not a truncated page
that the CLI then has to filter again).
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity


@pytest.mark.integration
class TestIntentRiskFilter:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    @pytest_asyncio.fixture
    async def seeded_units(self, session: AsyncSession):
        """Create units spanning every (intent_class, risk_class) combo we want to filter on."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        entity_id = uuid4()

        permanent_id = uuid4()
        durable_id = uuid4()
        ephemeral_id = uuid4()
        sensitive_id = uuid4()
        private_id = uuid4()
        safety_id = uuid4()

        note = Note(id=note_id, original_text='intent risk filter test note', vault_id=vault_id)
        emb = [0.1] * 384
        now = datetime.now(timezone.utc)

        units = [
            MemoryUnit(
                id=permanent_id,
                note_id=note_id,
                text='Permanent fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='permanent',
                risk_class='none',
            ),
            MemoryUnit(
                id=durable_id,
                note_id=note_id,
                text='Durable fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='durable',
                risk_class='none',
            ),
            MemoryUnit(
                id=ephemeral_id,
                note_id=note_id,
                text='Ephemeral fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='ephemeral',
                risk_class='none',
            ),
            MemoryUnit(
                id=sensitive_id,
                note_id=note_id,
                text='Sensitive fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='durable',
                risk_class='sensitive',
            ),
            MemoryUnit(
                id=private_id,
                note_id=note_id,
                text='Private fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='durable',
                risk_class='private',
            ),
            MemoryUnit(
                id=safety_id,
                note_id=note_id,
                text='Safety fact about classification',
                fact_type=FactTypes.WORLD,
                embedding=emb,
                vault_id=vault_id,
                event_date=now,
                intent_class='durable',
                risk_class='safety',
            ),
        ]
        entity = Entity(id=entity_id, canonical_name='Classification')

        unit_ids = (
            permanent_id,
            durable_id,
            ephemeral_id,
            sensitive_id,
            private_id,
            safety_id,
        )

        session.add(note)
        for unit in units:
            session.add(unit)
        session.add(entity)
        for uid in unit_ids:
            session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=vault_id))
        await session.commit()

        try:
            yield {
                'permanent_id': permanent_id,
                'durable_id': durable_id,
                'ephemeral_id': ephemeral_id,
                'sensitive_id': sensitive_id,
                'private_id': private_id,
                'safety_id': safety_id,
            }
        finally:
            # Explicit teardown — defends against state leak across tests even
            # if the autouse ``clean_tables`` fixture is ever bypassed. Uses
            # ORM ``delete()`` rather than raw ``text()`` because asyncpg can
            # mis-serialise Python lists passed to ``text()`` array binds
            # (round-7 review: the previous ``ANY(CAST(:ids AS uuid[]))``
            # form raised ``syntax error at or near "{"`` at runtime; the
            # ``TRUNCATE`` in ``clean_tables`` was masking the broken path).
            await session.execute(delete(UnitEntity).where(UnitEntity.unit_id.in_(unit_ids)))
            await session.execute(delete(MemoryUnit).where(MemoryUnit.id.in_(unit_ids)))
            await session.execute(delete(Entity).where(Entity.id == entity_id))
            await session.execute(delete(Note).where(Note.id == note_id))
            await session.commit()

    async def test_intent_filter_permanent(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            intent_class='permanent',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        assert seeded_units['permanent_id'] in result_ids
        # Only permanent should pass — every other intent class is excluded
        for key in ('durable_id', 'ephemeral_id', 'sensitive_id', 'private_id', 'safety_id'):
            assert seeded_units[key] not in result_ids

    async def test_intent_filter_ephemeral(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            intent_class='ephemeral',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        assert seeded_units['ephemeral_id'] in result_ids
        for key in ('permanent_id', 'durable_id', 'sensitive_id', 'private_id', 'safety_id'):
            assert seeded_units[key] not in result_ids

    async def test_risk_filter_sensitive(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            risk_class='sensitive',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        assert seeded_units['sensitive_id'] in result_ids
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'private_id', 'safety_id'):
            assert seeded_units[key] not in result_ids

    async def test_risk_filter_private(self, session: AsyncSession, engine_instance, seeded_units):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            risk_class='private',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        assert seeded_units['private_id'] in result_ids
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'sensitive_id', 'safety_id'):
            assert seeded_units[key] not in result_ids

    async def test_risk_filter_safety(self, session: AsyncSession, engine_instance, seeded_units):
        """Risk class 'safety' is the most security-critical — verify isolation explicitly."""
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            risk_class='safety',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        assert seeded_units['safety_id'] in result_ids
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'sensitive_id', 'private_id'):
            assert seeded_units[key] not in result_ids

    async def test_combined_intent_and_risk_filter(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            intent_class='durable',
            risk_class='sensitive',
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        # Only the durable+sensitive unit should remain
        assert seeded_units['sensitive_id'] in result_ids
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'private_id', 'safety_id'):
            assert seeded_units[key] not in result_ids

    async def test_no_filter_returns_all_intents(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='classification',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = {r.id for r in results}
        # All six units should be returned (no filter applied)
        for key in (
            'permanent_id',
            'durable_id',
            'ephemeral_id',
            'sensitive_id',
            'private_id',
            'safety_id',
        ):
            assert seeded_units[key] in result_ids


@pytest.mark.integration
class TestIntentRiskNullSemantics:
    """Round-5 regression guard for NULL ``intent_class`` / ``risk_class`` semantics.

    The server-side filter uses strict SQL equality (``column = :value``), so rows
    with NULL classifications would be excluded when a filter is active. The legacy
    client-side filter (``getattr(u, 'intent_class', 'durable') == wanted``) was
    behaviorally equivalent — ``getattr``'s default fires only when the attribute
    is missing, never when it is the literal ``None``. Migrating from client- to
    server-side filtering therefore preserves the original semantics.

    These tests assert the F25 schema invariants that make this safe in production:
    ``intent_class`` and ``risk_class`` are ``NOT NULL`` with ``server_default``
    backfills, plus CHECK constraints pinning values to the enum domain. If these
    invariants ever regress, the filter's NULL-exclusion behavior could leak.
    """

    async def test_intent_class_rejects_null_at_db_level(self, session: AsyncSession):
        """The NOT NULL constraint must reject INSERTs with intent_class = NULL."""
        note_id = uuid4()
        unit_id = uuid4()
        await session.execute(
            text(
                'INSERT INTO notes (id, original_text, vault_id) '
                'VALUES (CAST(:id AS uuid), :txt, CAST(:vault AS uuid))'
            ),
            {'id': str(note_id), 'txt': 'null-semantics test', 'vault': str(GLOBAL_VAULT_ID)},
        )
        with pytest.raises(Exception, match=r'(?i)null|not[- ]null'):
            await session.execute(
                text(
                    'INSERT INTO memory_units '
                    '(id, note_id, text, fact_type, vault_id, event_date, intent_class) '
                    'VALUES (CAST(:id AS uuid), CAST(:note AS uuid), :txt, :ft, '
                    'CAST(:vault AS uuid), now(), NULL)'
                ),
                {
                    'id': str(unit_id),
                    'note': str(note_id),
                    'txt': 'should fail',
                    'ft': FactTypes.WORLD.value,
                    'vault': str(GLOBAL_VAULT_ID),
                },
            )
        await session.rollback()

    async def test_risk_class_rejects_null_at_db_level(self, session: AsyncSession):
        """The NOT NULL constraint must reject INSERTs with risk_class = NULL."""
        note_id = uuid4()
        unit_id = uuid4()
        await session.execute(
            text(
                'INSERT INTO notes (id, original_text, vault_id) '
                'VALUES (CAST(:id AS uuid), :txt, CAST(:vault AS uuid))'
            ),
            {'id': str(note_id), 'txt': 'null-semantics test', 'vault': str(GLOBAL_VAULT_ID)},
        )
        with pytest.raises(Exception, match=r'(?i)null|not[- ]null'):
            await session.execute(
                text(
                    'INSERT INTO memory_units '
                    '(id, note_id, text, fact_type, vault_id, event_date, risk_class) '
                    'VALUES (CAST(:id AS uuid), CAST(:note AS uuid), :txt, :ft, '
                    'CAST(:vault AS uuid), now(), NULL)'
                ),
                {
                    'id': str(unit_id),
                    'note': str(note_id),
                    'txt': 'should fail',
                    'ft': FactTypes.WORLD.value,
                    'vault': str(GLOBAL_VAULT_ID),
                },
            )
        await session.rollback()

    async def test_intent_class_default_is_durable(self, session: AsyncSession):
        """An INSERT that omits intent_class/risk_class must be backfilled by server_default.

        This guarantees pre-F25 rows (added before the columns existed) are
        seen as ``intent_class='durable'`` / ``risk_class='none'`` rather than
        ``NULL``, matching the legacy client-side filter's implicit assumption.
        """
        note_id = uuid4()
        unit_id = uuid4()
        await session.execute(
            text(
                'INSERT INTO notes (id, original_text, vault_id) '
                'VALUES (CAST(:id AS uuid), :txt, CAST(:vault AS uuid))'
            ),
            {'id': str(note_id), 'txt': 'default test', 'vault': str(GLOBAL_VAULT_ID)},
        )
        await session.execute(
            text(
                'INSERT INTO memory_units '
                '(id, note_id, text, fact_type, vault_id, event_date) '
                'VALUES (CAST(:id AS uuid), CAST(:note AS uuid), :txt, :ft, '
                'CAST(:vault AS uuid), now())'
            ),
            {
                'id': str(unit_id),
                'note': str(note_id),
                'txt': 'default-backfill row',
                'ft': FactTypes.WORLD.value,
                'vault': str(GLOBAL_VAULT_ID),
            },
        )
        await session.commit()

        result = await session.execute(
            text('SELECT intent_class, risk_class FROM memory_units WHERE id = CAST(:id AS uuid)'),
            {'id': str(unit_id)},
        )
        row = result.one()
        assert row.intent_class == 'durable'
        assert row.risk_class == 'none'

        await session.execute(
            text('DELETE FROM memory_units WHERE id = CAST(:id AS uuid)'),
            {'id': str(unit_id)},
        )
        await session.execute(
            text('DELETE FROM notes WHERE id = CAST(:id AS uuid)'),
            {'id': str(note_id)},
        )
        await session.commit()
