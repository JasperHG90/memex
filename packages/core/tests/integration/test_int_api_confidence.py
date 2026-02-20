import pytest
from uuid import UUID
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from memex_core.memory.confidence import ConfidenceEngine
from memex_core.memory.sql_models import MemoryUnit, EvidenceLog
from memex_common.types import FactTypes
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
import datetime as dt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_belief_integration(
    session: AsyncSession, metastore: AsyncPostgresMetaStoreEngine, filestore: BaseAsyncFileStore
):
    # 1. Setup Engine
    engine = ConfidenceEngine()

    # 2. Insert a unit to update
    unit = MemoryUnit(
        text='Test fact for belief adjustment.',
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
        fact_type=FactTypes.OPINION,
        confidence_alpha=1.0,
        confidence_beta=1.0,
        access_count=0,
    )
    session.add(unit)
    await session.commit()
    unit_uuid = str(unit.id)

    # 3. Adjust belief: User Validation (weight 10.0)
    # Note: ConfidenceEngine.adjust_belief expects an active session and does not commit itself.
    await engine.adjust_belief(session, unit_uuid, 'user_validation', 'Verified by user in chat.')
    await session.commit()

    # 4. Verify update
    # We can reuse the same session or verify via a new one.
    # Since we committed, we can refresh or query.
    await session.refresh(unit)

    assert unit.confidence_alpha == 11.0
    assert unit.confidence_beta == 1.0

    # Check Evidence Log
    stmt_log = select(EvidenceLog).where(EvidenceLog.unit_id == UUID(unit_uuid))
    res_log = await session.exec(stmt_log)
    log_entry = res_log.one()

    assert log_entry.evidence_type == 'user_validation'
    assert log_entry.description == 'Verified by user in chat.'
    assert log_entry.alpha_before == 1.0
    assert log_entry.alpha_after == 11.0
