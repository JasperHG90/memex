"""F25 integration tests against real Postgres.

Covers:
- Migration 024 applied: intent_class + risk_class columns exist with defaults
- ProcessedFact intent/risk values flow through insert_facts_batch into the row
- CHECK constraints reject invalid enum values at DB level
- filter_safety_blocked + insert_facts_batch end-to-end never persists safety
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4, UUID

from sqlalchemy import text as sql_text

from memex_core.memory.extraction import storage
from memex_core.memory.extraction.classifier import filter_safety_blocked
from memex_core.memory.extraction.models import ProcessedFact, FactTypes
from memex_core.memory.sql_models import MemoryUnit, Note


def _make_fact(text: str, **overrides) -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type=FactTypes.WORLD,
        embedding=[0.0] * 384,
        mentioned_at=datetime.now(timezone.utc),
        **overrides,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_intent_risk_columns_default_when_unspecified(session):
    """Insert without setting intent/risk → DB picks up the schema defaults."""
    doc_id = uuid4()
    session.add(Note(id=doc_id, original_text='F25 default test'))
    await session.commit()

    fact = _make_fact(text=f'fact {uuid4()}')
    ids = await storage.insert_facts_batch(session, [fact], note_id=str(doc_id))
    db_unit = await session.get(MemoryUnit, UUID(ids[0]))
    assert db_unit is not None
    assert db_unit.intent_class == 'durable'
    assert db_unit.risk_class == 'none'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_intent_risk_values_persist(session):
    """Classifier-set values (e.g. ephemeral / private) survive the round trip."""
    doc_id = uuid4()
    session.add(Note(id=doc_id, original_text='F25 round trip'))
    await session.commit()

    fact = _make_fact(text=f'fact {uuid4()}')
    fact.intent_class = 'ephemeral'
    fact.risk_class = 'private'

    ids = await storage.insert_facts_batch(session, [fact], note_id=str(doc_id))
    db_unit = await session.get(MemoryUnit, UUID(ids[0]))
    assert db_unit is not None
    assert db_unit.intent_class == 'ephemeral'
    assert db_unit.risk_class == 'private'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_check_constraint_rejects_invalid_intent(session):
    """The ck_memory_units_intent_class CHECK rejects out-of-enum values."""
    doc_id = uuid4()
    session.add(Note(id=doc_id, original_text='F25 invalid intent'))
    await session.commit()

    fact = _make_fact(text=f'fact {uuid4()}')
    fact.intent_class = 'forever'  # not in (permanent, durable, ephemeral)
    with pytest.raises(Exception):  # asyncpg/sqlalchemy will raise CheckViolationError
        await storage.insert_facts_batch(session, [fact], note_id=str(doc_id))
        await session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_check_constraint_rejects_invalid_risk(session):
    """The ck_memory_units_risk_class CHECK rejects out-of-enum values."""
    doc_id = uuid4()
    session.add(Note(id=doc_id, original_text='F25 invalid risk'))
    await session.commit()

    fact = _make_fact(text=f'fact {uuid4()}')
    fact.risk_class = 'extremely_dangerous'  # not in the enum
    with pytest.raises(Exception):
        await storage.insert_facts_batch(session, [fact], note_id=str(doc_id))
        await session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_filter_safety_blocked_prevents_persistence(session):
    """filter_safety_blocked + insert_facts_batch: safety facts never reach the DB."""
    doc_id = uuid4()
    session.add(Note(id=doc_id, original_text='F25 safety block'))
    await session.commit()

    keep = _make_fact(text=f'safe {uuid4()}')
    block = _make_fact(text=f'blocked {uuid4()}')
    block.risk_class = 'safety'

    final = filter_safety_blocked([keep, block])
    assert len(final) == 1
    assert final[0].fact_text == keep.fact_text

    ids = await storage.insert_facts_batch(session, final, note_id=str(doc_id))
    assert len(ids) == 1

    # Verify the safety-marked fact did not land in the DB by counting rows
    # whose text matches the blocked one.
    result = await session.exec(
        sql_text('SELECT count(*) FROM memory_units WHERE text = :text').bindparams(
            text=block.fact_text
        )
    )
    count = result.scalar()
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_migration_added_columns_with_defaults(session):
    """Direct DDL probe: information_schema confirms columns + their defaults."""
    result = await session.exec(
        sql_text(
            'SELECT column_name, column_default, is_nullable '
            'FROM information_schema.columns '
            "WHERE table_name = 'memory_units' "
            "AND column_name IN ('intent_class', 'risk_class') "
            'ORDER BY column_name'
        )
    )
    rows = result.all()
    by_name = {r[0]: r for r in rows}
    assert 'intent_class' in by_name
    assert 'risk_class' in by_name
    # server_default may show as 'durable'::text or similar; match on the literal
    assert "'durable'" in str(by_name['intent_class'][1])
    assert "'none'" in str(by_name['risk_class'][1])
    assert by_name['intent_class'][2] == 'NO'
    assert by_name['risk_class'][2] == 'NO'
