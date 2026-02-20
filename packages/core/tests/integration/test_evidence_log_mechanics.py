import pytest
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import MemoryUnit, EvidenceLog, Document
from memex_common.types import FactTypes
from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.confidence import ConfidenceEngine


@pytest.mark.asyncio
async def test_reproduce_evidence_log(session: AsyncSession):
    # 0. Create a Document
    doc_id = uuid4()
    doc = Document(
        id=doc_id,
        original_text='Source document text',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(doc)

    # 1. Create a MemoryUnit
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        document_id=doc.id,
        text='Python is slow.',
        fact_type=FactTypes.OPINION,
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.1] * 384,
        event_date=datetime.now(),
        confidence_alpha=1.0,
        confidence_beta=1.0,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)

    # 2. Adjust belief
    engine = ConfidenceEngine()

    # We explicitly verify the evidence type key
    evidence_key = 'user_validation'

    await engine.adjust_belief(
        session=session,
        unit_uuid=unit_id,
        evidence_type_key=evidence_key,
        description='User confirmed this fact.',
    )

    # Commit the changes (ConfidenceEngine adds to session, but caller must commit)
    await session.commit()

    # 3. Check EvidenceLog
    statement = select(EvidenceLog).where(EvidenceLog.unit_id == unit_id)
    result = await session.exec(statement)
    logs = result.all()

    assert len(logs) == 1, 'Evidence log should contain 1 entry'
    log = logs[0]
    assert log.evidence_type == evidence_key
    assert log.alpha_before == 1.0
    assert log.beta_before == 1.0
    # User validation adds 10.0 to alpha
    assert log.alpha_after == 11.0
    assert log.beta_after == 1.0


@pytest.mark.asyncio
async def test_custom_update_log(session: AsyncSession):
    # 0. Create a Document
    doc_id = uuid4()
    doc = Document(
        id=doc_id,
        original_text='Source doc 2',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(doc)

    # 1. Create a MemoryUnit
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        document_id=doc_id,
        text='Opinion for merge test',
        embedding=[0.0] * 384,
        fact_type=FactTypes.OPINION,
        event_date=datetime.now(timezone.utc),
        confidence_alpha=2.0,
        confidence_beta=1.0,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)

    # 2. Apply Custom Update (simulating opinion merge)
    engine = ConfidenceEngine()
    alpha_delta = 1.5
    beta_delta = 0.5

    await engine.apply_custom_update(
        session=session,
        unit_uuid=unit_id,
        alpha_delta=alpha_delta,
        beta_delta=beta_delta,
        evidence_type='opinion_merge',
        description='Merged with new opinion',
    )

    await session.commit()

    # 3. Verify Log
    statement = select(EvidenceLog).where(EvidenceLog.unit_id == unit_id)
    result = await session.exec(statement)
    logs = result.all()

    assert len(logs) == 1
    log = logs[0]
    assert log.evidence_type == 'opinion_merge'
    assert log.alpha_before == 2.0
    assert log.beta_before == 1.0
    assert log.alpha_after == 3.5  # 2.0 + 1.5
    assert log.beta_after == 1.5  # 1.0 + 0.5
