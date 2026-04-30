"""Integration tests for OutcomeService — MW counter recording against real Postgres.

Covers F1a: atomic counter increments on MemoryUnit, UnitEntity, and MentalModel,
plus MW score/boost computation from real DB state.
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    MentalModel,
    Note,
    Observation,
    UnitEntity,
)
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.services.outcomes import (
    OutcomeService,
    compute_mw_score,
    compute_mw_boost,
)


@pytest.mark.integration
class TestOutcomeService:
    @pytest_asyncio.fixture
    async def seeded_data(self, session: AsyncSession):
        """Create a Note + MemoryUnit + Entity + UnitEntity + MentalModel."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        unit_id = uuid4()
        entity_id = uuid4()
        model_id = uuid4()

        note = Note(id=note_id, original_text='Test note', vault_id=vault_id)
        unit = MemoryUnit(
            id=unit_id,
            note_id=note_id,
            text='Test fact',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
        )
        entity = Entity(id=entity_id, canonical_name='Test Entity')
        ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id)
        obs = Observation(title='Test', content='Observation', evidence=[])
        model = MentalModel(
            id=model_id,
            entity_id=entity_id,
            name='Test Entity',
            observations=[obs.model_dump(mode='json')],
            last_refreshed=datetime.now(timezone.utc),
            embedding=[0.1] * 384,
            vault_id=vault_id,
        )

        session.add(note)
        session.add(unit)
        session.add(entity)
        session.add(ue)
        session.add(model)
        await session.commit()

        return {
            'note_id': note_id,
            'unit_id': unit_id,
            'entity_id': entity_id,
            'model_id': model_id,
            'vault_id': str(vault_id),
        }

    async def test_record_outcome_increments_success_counter(
        self, session: AsyncSession, seeded_data
    ):
        svc = OutcomeService()
        result = await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id'])],
            success=True,
            vault_id=seeded_data['vault_id'],
        )

        assert result['units_updated'] == 1
        unit = await session.get(MemoryUnit, seeded_data['unit_id'])
        assert unit is not None
        assert unit.success_co_count == 1
        assert unit.failure_co_count == 0

    async def test_record_outcome_increments_failure_counter(
        self, session: AsyncSession, seeded_data
    ):
        svc = OutcomeService()
        result = await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id'])],
            success=False,
            vault_id=seeded_data['vault_id'],
        )

        assert result['units_updated'] == 1
        unit = await session.get(MemoryUnit, seeded_data['unit_id'])
        assert unit is not None
        assert unit.failure_co_count == 1
        assert unit.success_co_count == 0

    async def test_record_outcome_propagates_to_unit_entity(
        self, session: AsyncSession, seeded_data
    ):
        svc = OutcomeService()
        await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id'])],
            success=True,
            vault_id=seeded_data['vault_id'],
        )

        ue = (
            await session.exec(
                select(UnitEntity).where(UnitEntity.unit_id == seeded_data['unit_id'])
            )
        ).first()
        assert ue is not None
        assert ue.success_co_count == 1

    async def test_record_outcome_propagates_to_mental_model(
        self, session: AsyncSession, seeded_data
    ):
        svc = OutcomeService()
        await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id'])],
            success=True,
            vault_id=seeded_data['vault_id'],
        )

        model = await session.get(MentalModel, seeded_data['model_id'])
        assert model is not None
        assert model.success_co_count == 1

    async def test_record_outcome_atomicity_invalid_unit(self, session: AsyncSession, seeded_data):
        svc = OutcomeService()
        fake_id = str(uuid4())
        result = await svc.record_outcome(
            session,
            unit_ids=[fake_id],
            success=True,
            vault_id=seeded_data['vault_id'],
        )

        assert result['units_updated'] == 0
        # Verify real unit was not affected
        unit = await session.get(MemoryUnit, seeded_data['unit_id'])
        assert unit is not None
        assert unit.success_co_count == 0

    async def test_record_outcome_cold_start_mw_score(self, session: AsyncSession, seeded_data):
        unit = await session.get(MemoryUnit, seeded_data['unit_id'])
        assert unit is not None
        assert unit.success_co_count == 0
        assert unit.failure_co_count == 0
        assert compute_mw_score(0, 0) == 0.5

    async def test_mw_boost_formula(self, session: AsyncSession, seeded_data):
        svc = OutcomeService()

        # Record 3 successes, 1 failure
        for _ in range(3):
            await svc.record_outcome(
                session,
                unit_ids=[str(seeded_data['unit_id'])],
                success=True,
                vault_id=seeded_data['vault_id'],
            )
        await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id'])],
            success=False,
            vault_id=seeded_data['vault_id'],
        )

        unit = await session.get(MemoryUnit, seeded_data['unit_id'])
        assert unit is not None
        assert unit.success_co_count == 3
        assert unit.failure_co_count == 1

        mw_score = compute_mw_score(3, 1)
        mw_boost = compute_mw_boost(3, 1)

        assert mw_score == (3 + 1) / (3 + 1 + 2)  # 4/6 = 0.6667
        assert mw_boost == pytest.approx(1.0 + 0.3 * (mw_score - 0.5))
        assert mw_boost > 1.0  # More successes -> boost

    async def test_record_outcome_multiple_units(self, session: AsyncSession, seeded_data):
        # Create a second unit
        unit2_id = uuid4()
        unit2 = MemoryUnit(
            id=unit2_id,
            note_id=seeded_data['note_id'],
            text='Second fact',
            fact_type=FactTypes.WORLD,
            embedding=[0.2] * 384,
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
        )
        ue2 = UnitEntity(
            unit_id=unit2_id,
            entity_id=seeded_data['entity_id'],
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit2)
        session.add(ue2)
        await session.commit()

        svc = OutcomeService()
        result = await svc.record_outcome(
            session,
            unit_ids=[str(seeded_data['unit_id']), str(unit2_id)],
            success=True,
            vault_id=seeded_data['vault_id'],
        )

        assert result['units_updated'] == 2

        u1 = await session.get(MemoryUnit, seeded_data['unit_id'])
        u2 = await session.get(MemoryUnit, unit2_id)
        assert u1 is not None and u1.success_co_count == 1
        assert u2 is not None and u2.success_co_count == 1
