"""Integration tests for F14 ``OutcomeService.record_outcome`` extension (TC-F14-3).

Three angles:

* The new ``target_type='kv_key'`` path upserts into ``procedure_outcomes``,
  atomically incrementing the right counter and stamping ``last_outcome_at``.
* Cross-vault isolation: same kv_key in two vaults yields two distinct
  rows, neither leaking counts to the other.
* The default positional ``target_type='memory_unit'`` path remains
  unchanged — adding the new mode must NOT regress existing callers.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.kv import KVService
from memex_core.services.outcomes import OutcomeService

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def kv(metastore, filestore, memex_config):
    return KVService(metastore=metastore, filestore=filestore, config=memex_config)


@pytest.fixture
def outcomes() -> OutcomeService:
    return OutcomeService()


def _unique_proc_key() -> str:
    return f'procedure:run_tests:tag-{uuid4().hex[:8]}'


@pytest.mark.asyncio
async def test_kv_key_mode_inserts_and_increments_counter(
    kv: KVService, outcomes: OutcomeService
) -> None:
    """First call inserts a row at (1,0); second success call lifts to (2,0)."""
    key = _unique_proc_key()
    # Procedure key must exist as a kv_entries row (FK target).
    await kv.put(key=key, value='step-one')

    async with kv.metastore.session() as session:
        first = await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
    assert first['success_co_count'] == 1
    assert first['failure_co_count'] == 0
    assert first['last_outcome_at'] is not None

    async with kv.metastore.session() as session:
        second = await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
    assert second['success_co_count'] == 2
    assert second['failure_co_count'] == 0


@pytest.mark.asyncio
async def test_kv_key_mode_failure_increments_failure_only(
    kv: KVService, outcomes: OutcomeService
) -> None:
    """``success=False`` lifts only ``failure_co_count``."""
    key = _unique_proc_key()
    await kv.put(key=key, value='step-x')

    async with kv.metastore.session() as session:
        a = await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
    async with kv.metastore.session() as session:
        b = await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=False,
            vault_id=str(GLOBAL_VAULT_ID),
        )
    assert a['success_co_count'] == 1
    assert a['failure_co_count'] == 0
    assert b['success_co_count'] == 1
    assert b['failure_co_count'] == 1


@pytest.mark.asyncio
async def test_cross_vault_isolation_for_kv_key_outcomes(
    kv: KVService, outcomes: OutcomeService, metastore
) -> None:
    """Same kv_key in two vaults → two distinct counter rows, no leakage."""
    key = _unique_proc_key()
    await kv.put(key=key, value='shared-procedure')

    other_vault_id = uuid4()
    async with metastore.session() as session:
        await session.execute(
            text("INSERT INTO vaults (id, name, description) VALUES (:id, :name, '')"),
            {'id': other_vault_id, 'name': f'v-{other_vault_id.hex[:8]}'},
        )
        await session.commit()

    async with metastore.session() as session:
        await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
        await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )
        await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=False,
            vault_id=str(other_vault_id),
        )

    async with metastore.session() as session:
        rows = (
            await session.execute(
                text(
                    'SELECT vault_id, success_co_count, failure_co_count '
                    'FROM procedure_outcomes WHERE kv_key = :k '
                    'ORDER BY vault_id'
                ),
                {'k': key},
            )
        ).all()
    by_vault = {str(r[0]): (r[1], r[2]) for r in rows}
    assert len(by_vault) == 2, f'expected two distinct vault rows; got {by_vault}'
    assert by_vault[str(GLOBAL_VAULT_ID)] == (2, 0)
    assert by_vault[str(other_vault_id)] == (0, 1)


@pytest.mark.asyncio
async def test_kv_key_mode_rejects_non_procedure_keys(
    kv: KVService, outcomes: OutcomeService, metastore
) -> None:
    """``kv_key`` must match ``procedure:<verb>:<tag>`` regex; otherwise ValueError."""
    async with metastore.session() as session:
        with pytest.raises(ValueError, match='Invalid procedure key'):
            await outcomes.record_outcome(
                session=session,
                target_type='kv_key',
                kv_key='global:not-a-procedure',
                success=True,
                vault_id=str(GLOBAL_VAULT_ID),
            )


@pytest.mark.asyncio
async def test_memory_unit_positional_path_unchanged(outcomes: OutcomeService, metastore) -> None:
    """The default ``target_type='memory_unit'`` path still works positionally.

    Regression guard: F14 added a keyword-only ``target_type``/``kv_key``;
    existing callers passing only positional args must continue to land on
    the memory_unit path.
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
async def test_unknown_target_type_raises(outcomes: OutcomeService, metastore) -> None:
    """Unknown ``target_type`` rejects with ValueError."""
    async with metastore.session() as session:
        with pytest.raises(ValueError, match='target_type must be'):
            await outcomes.record_outcome(
                session=session,
                target_type='entity',  # not supported
                vault_id=str(GLOBAL_VAULT_ID),
            )


@pytest.mark.asyncio
async def test_kv_key_outcome_persists_through_kv_entry(
    kv: KVService, outcomes: OutcomeService, metastore
) -> None:
    """End-to-end: kv.put creates the FK target, record_outcome inserts the counter."""
    key = _unique_proc_key()
    await kv.put(key=key, value='full-loop')

    async with metastore.session() as session:
        await outcomes.record_outcome(
            session=session,
            target_type='kv_key',
            kv_key=key,
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )

    async with metastore.session() as session:
        result = await session.execute(
            text(
                'SELECT po.success_co_count, kv.value '
                'FROM procedure_outcomes po '
                'JOIN kv_entries kv ON po.kv_key = kv.key '
                'WHERE po.kv_key = :k'
            ),
            {'k': key},
        )
        row = result.first()
    assert row is not None
    assert row[0] == 1
    # value is still the JSON envelope at the kv_entries level
    assert '"value": "full-loop"' in row[1]
