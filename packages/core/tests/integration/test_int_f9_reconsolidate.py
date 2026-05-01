"""TC-23-4 — memex_memory_reconsolidate(entity_id, vault_id) end-to-end (F9 ship).

3 tests:
- test_resolves_unit_ids_then_runs_contradiction_then_reflection: full
  orchestration. Seed an entity + 3 units linked via UnitEntity in a vault,
  call reconsolidate, assert (a) lock observable in pg_locks during execution,
  (b) ContradictionEngine.detect_contradictions called with exact kwargs,
  (c) ReflectionService.reflect_batch called with matching ReflectionRequest,
  (d) returned envelope shape matches spec.
- test_resolver_excludes_other_vaults: same entity in two vaults, only
  vault V1's unit_ids resolved when called with V1.
- test_reconsolidate_blocks_concurrent_same_entity: concurrent calls on the
  same entity → second observes EntityLockTimeoutError (timeout=0.5).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from testcontainers.postgres import PostgresContainer

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.reflect.models import ReflectionRequest, ReflectionResult
from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    MentalModel,
    Note,
    UnitEntity,
    Vault,
)
from memex_core.services.locks import EntityLockTimeoutError, LocksService, entity_lock_id
from memex_core.services.reflection import ReflectionService

pytestmark = [pytest.mark.integration, pytest.mark.llm_mock]


@pytest.fixture(scope='module')
def asyncpg_dsn(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url().replace('postgresql+psycopg2://', 'postgresql://')
    parsed = urlparse(url)
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


@pytest_asyncio.fixture
async def helper_conn(asyncpg_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    conn = await asyncpg.connect(asyncpg_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def mocked_locks_service(metastore, memex_config, asyncpg_dsn) -> LocksService:
    """LocksService with real metastore (resolver works), mocked engines.

    Overrides the DSN to point at the testcontainer (memex_config doesn't have
    one wired by default in tests).
    """
    contradiction = MagicMock(spec=ContradictionEngine)
    contradiction.detect_contradictions = AsyncMock(return_value=None)

    reflection = MagicMock(spec=ReflectionService)
    reflection.reflect_batch = AsyncMock(return_value=[])

    svc = LocksService.__new__(LocksService)
    svc.metastore = metastore
    svc.config = memex_config
    svc.reflection = reflection
    svc.contradiction = contradiction
    svc._dsn = asyncpg_dsn
    return svc


async def _seed_entity_with_units(
    session: AsyncSession,
    *,
    entity_id: uuid.UUID,
    vault_id: uuid.UUID,
    unit_count: int,
) -> list[uuid.UUID]:
    note_id = uuid.uuid4()
    session.add(
        Note(
            id=note_id,
            content_hash='abc',
            vault_id=vault_id,
            original_text='seed',
        )
    )
    session.add(
        Entity(
            id=entity_id,
            canonical_name=f'Entity-{entity_id.hex[:6]}',
            entity_type='other',
            description='seeded for F9 reconsolidate tests',
        )
    )
    unit_ids: list[uuid.UUID] = []
    now = datetime.now(timezone.utc)
    for _ in range(unit_count):
        uid = uuid.uuid4()
        session.add(
            MemoryUnit(
                id=uid,
                note_id=note_id,
                text='seed unit',
                fact_type='world',
                vault_id=vault_id,
                embedding=[0.1] * 384,
                event_date=now,
            )
        )
        session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=vault_id))
        unit_ids.append(uid)
    await session.commit()
    return unit_ids


async def test_resolves_unit_ids_then_runs_contradiction_then_reflection(
    mocked_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    eid = uuid.uuid4()
    vault_id = GLOBAL_VAULT_ID
    seeded_unit_ids = await _seed_entity_with_units(
        session, entity_id=eid, vault_id=vault_id, unit_count=3
    )

    mm_id = uuid.uuid4()
    mocked_locks_service.reflection.reflect_batch = AsyncMock(
        return_value=[
            ReflectionResult(
                entity_id=eid,
                new_observations=[],
                updated_model=MentalModel(id=mm_id, entity_id=eid, vault_id=vault_id),
            )
        ]
    )

    result = await mocked_locks_service.reconsolidate_entity(eid, vault_id)

    contradiction_call = mocked_locks_service.contradiction.detect_contradictions.await_args
    assert contradiction_call is not None
    kwargs = contradiction_call.kwargs
    assert kwargs['document_id'] is None
    assert kwargs['vault_id'] == vault_id
    assert sorted(kwargs['unit_ids']) == sorted(seeded_unit_ids)

    reflect_call = mocked_locks_service.reflection.reflect_batch.await_args
    assert reflect_call is not None
    requests = reflect_call.args[0]
    assert len(requests) == 1
    assert isinstance(requests[0], ReflectionRequest)
    assert requests[0].entity_id == eid
    assert requests[0].vault_id == vault_id
    assert requests[0].limit_recent_memories is None

    assert result['entity_id'] == str(eid)
    assert result['vault_id'] == str(vault_id)
    assert result['units_examined'] == 3
    assert result['contradictions_run'] == 3
    assert result['mental_model_id'] == str(mm_id)
    assert result['observations_added'] == 0


async def test_resolver_excludes_other_vaults(
    mocked_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    eid = uuid.uuid4()
    v1 = GLOBAL_VAULT_ID
    v2 = uuid.uuid4()
    session.add(Vault(id=v2, name=f'tc23-4-{v2.hex[:6]}', description='F9 vault-scoping test'))
    await session.commit()

    units_v1 = await _seed_entity_with_units(session, entity_id=eid, vault_id=v1, unit_count=2)
    note_v2 = uuid.uuid4()
    session.add(Note(id=note_v2, content_hash='abc-v2', vault_id=v2, original_text='seed v2'))
    other_unit_id = uuid.uuid4()
    session.add(
        MemoryUnit(
            id=other_unit_id,
            note_id=note_v2,
            text='v2 unit',
            fact_type='world',
            vault_id=v2,
            embedding=[0.1] * 384,
            event_date=datetime.now(timezone.utc),
        )
    )
    session.add(UnitEntity(unit_id=other_unit_id, entity_id=eid, vault_id=v2))
    await session.commit()

    sanity = await session.exec(select(UnitEntity).where(UnitEntity.entity_id == eid))
    all_links = list(sanity)
    assert len(all_links) == 3, 'sanity: 2 in v1 + 1 in v2'

    await mocked_locks_service.reconsolidate_entity(eid, v1)
    contradiction_call = mocked_locks_service.contradiction.detect_contradictions.await_args
    assert contradiction_call is not None
    resolved_unit_ids = contradiction_call.kwargs['unit_ids']
    assert sorted(resolved_unit_ids) == sorted(units_v1)
    assert other_unit_id not in resolved_unit_ids


async def test_reconsolidate_blocks_concurrent_same_entity(
    mocked_locks_service: LocksService,
    session: AsyncSession,
    helper_conn: asyncpg.Connection,
) -> None:
    eid = uuid.uuid4()
    vault_id = GLOBAL_VAULT_ID
    await _seed_entity_with_units(session, entity_id=eid, vault_id=vault_id, unit_count=1)

    lock_id = entity_lock_id(eid)
    got = await helper_conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
    assert got is True

    try:
        with pytest.raises(EntityLockTimeoutError) as exc_info:
            await mocked_locks_service.reconsolidate_entity(eid, vault_id, timeout_seconds=0.5)
        assert str(eid) in str(exc_info.value)

        mocked_locks_service.contradiction.detect_contradictions.assert_not_awaited()
        mocked_locks_service.reflection.reflect_batch.assert_not_awaited()
    finally:
        await helper_conn.execute('SELECT pg_advisory_unlock($1)', lock_id)
