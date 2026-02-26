import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.sql_models import (
    Vault,
    Note,
    MemoryUnit,
    MentalModel,
    TokenUsage,
    ReflectionQueue,
    EvidenceLog,
    Chunk,
    Entity,
)
from memex_common.types import FactTypes
from memex_common.config import GLOBAL_VAULT_ID
import uuid
import datetime as dt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_cascading_delete(session: AsyncSession):
    """
    Integration test to verify that deleting a Vault cascades to all related entities.
    """

    # 1. Create a new temporary Vault
    vault_id = uuid.uuid4()
    vault = Vault(
        id=vault_id,
        name='Cascade Test Vault',
        description='A vault to test cascading deletes',
    )
    session.add(vault)
    await session.commit()  # Commit vault first to ensure it exists for FKs

    # 2. Create related entities tied to this vault
    # Note
    doc_id = uuid.uuid4()
    document = Note(
        id=doc_id,
        vault_id=vault_id,
        original_text='Test document for cascade',
        content_hash='hash_123',
    )
    session.add(document)

    # Chunk
    chunk_id = uuid.uuid4()
    chunk = Chunk(
        id=chunk_id,
        vault_id=vault_id,
        note_id=doc_id,
        text='Test chunk',
        chunk_index=0,  # Correct field name
    )
    session.add(chunk)

    # MemoryUnit
    unit_id = uuid.uuid4()
    memory_unit = MemoryUnit(
        id=unit_id,
        vault_id=vault_id,
        note_id=doc_id,
        text='Test memory unit',
        fact_type=FactTypes.WORLD,  # Correct fact_type
        embedding=[0.1] * 384,
        event_date=dt.datetime.now(dt.timezone.utc),
    )
    session.add(memory_unit)

    # Entity
    entity_id = uuid.uuid4()
    entity = Entity(
        id=entity_id,
        canonical_name='Test Entity',
    )
    session.add(entity)

    # Flush to ensure Note, Unit, Entity IDs exist for dependent FKs
    await session.flush()

    # MentalModel (depends on Vault and Entity)
    mm_id = uuid.uuid4()
    mental_model = MentalModel(
        id=mm_id,
        vault_id=vault_id,
        entity_id=entity_id,
        name='Test Mental Model',
        observations=[],
    )
    session.add(mental_model)

    # TokenUsage
    tu_id = uuid.uuid4()
    token_usage = TokenUsage(
        id=tu_id,
        vault_id=vault_id,
        models=['test-model'],
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
    )
    session.add(token_usage)

    # ReflectionQueue (depends on Entity and Vault)
    rq_id = uuid.uuid4()
    reflection_queue = ReflectionQueue(
        id=rq_id,
        vault_id=vault_id,
        entity_id=entity_id,
        priority=1,
    )
    session.add(reflection_queue)

    # EvidenceLog (depends on Unit and Vault)
    el_id = uuid.uuid4()
    evidence_log = EvidenceLog(
        id=el_id,
        vault_id=vault_id,
        unit_id=unit_id,
        evidence_type='update',
        description='Test update',
        alpha_before=1.0,
        beta_before=1.0,
        alpha_after=2.0,
        beta_after=1.0,
    )
    session.add(evidence_log)

    await session.commit()

    # 3. Verify entities exist
    assert await session.get(Vault, vault_id)
    assert await session.get(Note, doc_id)
    assert await session.get(Chunk, chunk_id)
    assert await session.get(MemoryUnit, unit_id)
    assert await session.get(Entity, entity_id)
    assert await session.get(MentalModel, mm_id)
    assert await session.get(TokenUsage, tu_id)
    assert await session.get(ReflectionQueue, rq_id)
    assert await session.get(EvidenceLog, el_id)

    # 4. Create Global Vault entities (Control group)
    # Global vault should already exist from fixture
    global_doc_id = uuid.uuid4()
    global_doc = Note(
        id=global_doc_id,
        vault_id=GLOBAL_VAULT_ID,
        original_text='Global document',
        content_hash='global_hash_123',
    )
    session.add(global_doc)
    await session.commit()
    assert await session.get(Note, global_doc_id)

    # 5. Delete the Vault
    # Re-fetch vault to ensure it's attached to session
    vault_to_delete = await session.get(Vault, vault_id)
    await session.delete(vault_to_delete)
    await session.commit()

    # Clear identity map so next gets hit the DB
    session.expire_all()

    # 6. Verify Cascade
    assert (await session.get(Vault, vault_id)) is None
    assert (await session.get(Note, doc_id)) is None
    assert (await session.get(Chunk, chunk_id)) is None
    assert (await session.get(MemoryUnit, unit_id)) is None
    assert (await session.get(MentalModel, mm_id)) is None
    assert (await session.get(TokenUsage, tu_id)) is None
    assert (await session.get(ReflectionQueue, rq_id)) is None
    assert (await session.get(EvidenceLog, el_id)) is None

    # 7. Verify Control group remains
    assert await session.get(Vault, GLOBAL_VAULT_ID)
    assert await session.get(Note, global_doc_id)
