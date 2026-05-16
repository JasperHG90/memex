"""Integration tests for ``OutcomeService.record_outcome`` (memory_unit path)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.outcomes import OutcomeService

pytestmark = [pytest.mark.integration]


@pytest.fixture
def outcomes() -> OutcomeService:
    return OutcomeService()


@pytest.mark.asyncio
async def test_memory_unit_path_works(outcomes: OutcomeService, metastore) -> None:
    """The default memory_unit path increments unit counters."""
    unit_id = uuid4()
    async with metastore.session() as session:
        await session.execute(
            text(
                'INSERT INTO memory_units (id, vault_id, text, '
                'fact_type, status, confidence, event_date) '
                "VALUES (:id, :vid, 'fact-content', 'world', 'active', 0.9, now())"
            ),
            {'id': unit_id, 'vid': GLOBAL_VAULT_ID},
        )
        await session.commit()

    async with metastore.session() as session:
        result = await outcomes.record_outcome(
            session=session,
            unit_ids=[str(unit_id)],
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
    assert result['units_updated'] == 1

    async with metastore.session() as session:
        from sqlmodel import select as sm_select

        row = (await session.exec(sm_select(MemoryUnit).where(MemoryUnit.id == unit_id))).first()
    assert row is not None
    assert row.success_co_count == 1
    assert row.failure_co_count == 0


@pytest.mark.asyncio
async def test_memory_unit_path_positional_call_signature(
    outcomes: OutcomeService, metastore
) -> None:
    """Positional-shape lock: ``record_outcome(session, unit_ids, success, vault_id)``.

    The contract specifies that ``unit_ids`` and ``success`` remain
    POSITIONAL. If a future refactor inserts a ``*,`` kw-only barrier
    between them, this call fails with TypeError, defeating silent
    regression of the MW-signal-quality contract.
    """
    unit_id = uuid4()
    async with metastore.session() as session:
        await session.execute(
            text(
                'INSERT INTO memory_units (id, vault_id, text, '
                'fact_type, status, confidence, event_date) '
                "VALUES (:id, :vid, 'fact-content', 'world', 'active', 0.9, now())"
            ),
            {'id': unit_id, 'vid': GLOBAL_VAULT_ID},
        )
        await session.commit()

    async with metastore.session() as session:
        result = await outcomes.record_outcome(session, [str(unit_id)], True, str(GLOBAL_VAULT_ID))
    assert result['units_updated'] == 1
