"""Integration tests for issue #92 — server-side intent_class / risk_class filter.

Verifies that intent_class and risk_class filters apply at the SQL level so the
search HTTP endpoint returns ONLY matching memory units (not a truncated page
that the CLI then has to filter again).
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

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
        ]
        entity = Entity(id=entity_id, canonical_name='Classification')

        session.add(note)
        for unit in units:
            session.add(unit)
        session.add(entity)
        for uid in (permanent_id, durable_id, ephemeral_id, sensitive_id, private_id):
            session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=vault_id))
        await session.commit()

        return {
            'permanent_id': permanent_id,
            'durable_id': durable_id,
            'ephemeral_id': ephemeral_id,
            'sensitive_id': sensitive_id,
            'private_id': private_id,
        }

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
        # Only permanent should pass — durable/ephemeral/sensitive/private all excluded
        for key in ('durable_id', 'ephemeral_id', 'sensitive_id', 'private_id'):
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
        for key in ('permanent_id', 'durable_id', 'sensitive_id', 'private_id'):
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
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'private_id'):
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
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'sensitive_id'):
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
        for key in ('permanent_id', 'durable_id', 'ephemeral_id', 'private_id'):
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
        # All five units should be returned (no filter applied)
        for key in (
            'permanent_id',
            'durable_id',
            'ephemeral_id',
            'sensitive_id',
            'private_id',
        ):
            assert seeded_units[key] in result_ids

    async def test_invalid_intent_class_rejected(self):
        with pytest.raises(ValueError, match='Invalid intent_class'):
            RetrievalRequest(query='x', intent_class='not-a-real-class')

    async def test_invalid_risk_class_rejected(self):
        with pytest.raises(ValueError, match='Invalid risk_class'):
            RetrievalRequest(query='x', risk_class='not-a-real-class')
