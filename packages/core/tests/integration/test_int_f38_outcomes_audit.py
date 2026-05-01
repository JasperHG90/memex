"""F38 outcome audit hook (TC-F38-OUTCOMES-AUDIT).

Per RFC-010, ``OutcomeService.record_outcome`` (memory_unit mode) must emit
one ``AuditLog`` row per unit with ``action='outcome.record'``,
``resource_type='memory_unit'``, ``resource_id=str(unit_id)``, and
``details = {'vault_id': str(vault_id), 'outcome': 'success' | 'failure'}``.

This is the diff hook ConsolidationService.select_diff_units reads from.
Without it, F38's diff selection silently misses every newly-outcomed unit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel import select

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    AuditLog,
    MemoryUnit,
    Note,
    Vault,
)
from memex_core.services.outcomes import OutcomeService

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_record_outcome_emits_audit_row(metastore):
    outcomes = OutcomeService()

    async with metastore.session() as session:
        vault_id = uuid4()
        note_id = uuid4()
        unit_id = uuid4()
        session.add(Vault(id=vault_id, name=f'v-{vault_id.hex[:8]}', description=''))
        await session.commit()
        session.add(
            Note(
                id=note_id,
                vault_id=vault_id,
                content_hash=f'h-{note_id.hex[:8]}',
                original_text='seed',
                filestore_path=None,
                assets=[],
            )
        )
        await session.commit()
        session.add(
            MemoryUnit(
                id=unit_id,
                note_id=note_id,
                vault_id=vault_id,
                text='seed unit',
                fact_type=FactTypes.WORLD,
                embedding=[0.1] * 384,
                event_date=datetime.now(timezone.utc),
            )
        )
        await session.commit()

        await outcomes.record_outcome(
            session=session,
            unit_ids=[str(unit_id)],
            success=True,
            vault_id=str(vault_id),
        )

    async with metastore.session() as session:
        rows = (
            await session.exec(
                select(AuditLog).where(
                    AuditLog.action == 'outcome.record',
                    AuditLog.resource_id == str(unit_id),
                )
            )
        ).all()

    assert len(rows) == 1, (
        f'Expected exactly one outcome.record audit row for unit {unit_id}; '
        f'got {len(rows)}. F38 select_diff_units relies on this hook.'
    )
    row = rows[0]
    assert row.resource_type == 'memory_unit'
    assert row.details == {'vault_id': str(vault_id), 'outcome': 'success'}


@pytest.mark.asyncio
async def test_record_outcome_failure_audits_failure_label(metastore):
    outcomes = OutcomeService()

    async with metastore.session() as session:
        vault_id = uuid4()
        note_id = uuid4()
        unit_id = uuid4()
        session.add(Vault(id=vault_id, name=f'v-{vault_id.hex[:8]}', description=''))
        await session.commit()
        session.add(
            Note(
                id=note_id,
                vault_id=vault_id,
                content_hash=f'h-{note_id.hex[:8]}',
                original_text='seed',
                filestore_path=None,
                assets=[],
            )
        )
        await session.commit()
        session.add(
            MemoryUnit(
                id=unit_id,
                note_id=note_id,
                vault_id=vault_id,
                text='seed unit',
                fact_type=FactTypes.WORLD,
                embedding=[0.1] * 384,
                event_date=datetime.now(timezone.utc),
            )
        )
        await session.commit()

        await outcomes.record_outcome(
            session=session,
            unit_ids=[str(unit_id)],
            success=False,
            vault_id=str(vault_id),
        )

    async with metastore.session() as session:
        rows = (
            await session.exec(
                select(AuditLog).where(
                    AuditLog.action == 'outcome.record',
                    AuditLog.resource_id == str(unit_id),
                )
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].details['outcome'] == 'failure'
