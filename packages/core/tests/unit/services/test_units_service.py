"""Unit tests for UnitsService (F4 — deprioritize / restore).

T3: AuditService.log is called with the exact contract AC-F4-2 specifies.
No DB; the metastore is a stand-in async context manager that yields a
`session` whose `.get()` returns a real `MemoryUnit` row built in-memory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.units import UnitsService


class _StubMetastore:
    """Minimal async metastore: ``async with metastore.session()`` yields a stub session."""

    def __init__(self, unit: MemoryUnit | None) -> None:
        self._unit = unit
        self.added: list[MemoryUnit] = []
        self.commits = 0
        self.flushes = 0
        self.refreshes = 0

    @asynccontextmanager
    async def session(self):
        meta = self

        class _StubSession:
            async def get(self, model, unit_id):
                return meta._unit

            def add(self, obj):
                meta.added.append(obj)

            async def flush(self):
                meta.flushes += 1

            async def commit(self):
                meta.commits += 1

            async def refresh(self, obj):
                meta.refreshes += 1

            async def exec(self, _stmt):
                class _Empty:
                    def all(self):
                        return []

                    def first(self):
                        return None

                return _Empty()

            async def execute(self, _stmt):
                class _Empty:
                    rowcount = 0

                    def all(self):
                        return []

                    def first(self):
                        return None

                return _Empty()

        yield _StubSession()


def _make_service(unit: MemoryUnit | None) -> tuple[UnitsService, _StubMetastore, MagicMock]:
    metastore = _StubMetastore(unit)
    service = UnitsService.__new__(UnitsService)
    service.metastore = metastore  # type: ignore[assignment]
    service.filestore = None  # type: ignore[assignment]
    service.config = None  # type: ignore[assignment]
    audit_mock = MagicMock()
    service._audit_service = audit_mock  # type: ignore[attr-defined]
    return service, metastore, audit_mock


@pytest.mark.asyncio
async def test_set_unit_deprioritized_writes_audit_with_exact_kwargs():
    """T3: AuditService.log called once with action='memory_deprioritize',
    resource_type='memory_unit', correct resource_id, details, and
    background_tasks forwarded.

    session_id is populated from the request-scoped ContextVar — assert it
    is forwarded (any value), but don't pin a specific value here.
    """
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=uuid4(),
        type='fact',
        text='example',
        is_deprioritized=False,
    )
    service, metastore, audit_mock = _make_service(unit)
    bg_tasks = MagicMock()

    result = await service.set_unit_deprioritized(
        unit_id, reason='it was wrong', actor='agent:claude', background_tasks=bg_tasks
    )

    assert result.is_deprioritized is True
    assert metastore.commits == 1
    assert audit_mock.log.call_count == 1
    kwargs = audit_mock.log.call_args.kwargs
    assert kwargs['action'] == 'memory_deprioritize'
    assert kwargs['actor'] == 'agent:claude'
    assert kwargs['resource_type'] == 'memory_unit'
    assert kwargs['resource_id'] == str(unit_id)
    assert kwargs['details'] == {'reason': 'it was wrong'}
    assert kwargs['background_tasks'] is bg_tasks
    assert 'session_id' in kwargs


@pytest.mark.asyncio
async def test_restore_unit_writes_audit_without_reason():
    """T3 (restore variant): AuditService.log called with action='memory_restore'
    and details=None (no reason for restore).
    """
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=uuid4(),
        type='fact',
        text='example',
        is_deprioritized=True,
    )
    service, metastore, audit_mock = _make_service(unit)
    bg_tasks = MagicMock()

    result = await service.restore_unit(unit_id, actor='agent:claude', background_tasks=bg_tasks)

    assert result.is_deprioritized is False
    assert metastore.commits == 1
    assert audit_mock.log.call_count == 1
    kwargs = audit_mock.log.call_args.kwargs
    assert kwargs['action'] == 'memory_restore'
    assert kwargs['actor'] == 'agent:claude'
    assert kwargs['resource_type'] == 'memory_unit'
    assert kwargs['resource_id'] == str(unit_id)
    assert kwargs['details'] is None
    assert kwargs['background_tasks'] is bg_tasks


@pytest.mark.asyncio
async def test_set_unit_deprioritized_raises_when_unit_missing():
    """Unit not found → raises MemoryUnitNotFoundError; no audit row written."""
    from memex_common.exceptions import MemoryUnitNotFoundError

    service, _, audit_mock = _make_service(None)
    with pytest.raises(MemoryUnitNotFoundError):
        await service.set_unit_deprioritized(
            uuid4(), reason='ignored', actor='x', background_tasks=None
        )
    audit_mock.log.assert_not_called()


@pytest.mark.asyncio
async def test_set_unit_deprioritized_rejects_cross_vault():
    """HIGH-005: passing vault_id that does not match the unit's vault must
    raise MemoryUnitNotFoundError and write NO audit row.

    This is the service-layer backstop behind the route's
    `check_vault_access` gate. A scoped key targeting an in-scope vault
    that supplies a (unit_id, vault_id) pair where the unit actually lives
    in a different vault must NOT mutate the row.
    """
    from memex_common.exceptions import MemoryUnitNotFoundError

    unit_id = uuid4()
    real_vault = uuid4()
    other_vault = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=real_vault,
        type='fact',
        text='example',
        is_deprioritized=False,
    )
    service, metastore, audit_mock = _make_service(unit)

    with pytest.raises(MemoryUnitNotFoundError):
        await service.set_unit_deprioritized(
            unit_id,
            reason='cross-vault attempt',
            vault_id=other_vault,
            actor='attacker',
            background_tasks=None,
        )

    # The flag MUST NOT have flipped on the in-memory unit — the service
    # raised before the assignment.
    assert unit.is_deprioritized is False
    # No commit, no audit write.
    assert metastore.commits == 0
    audit_mock.log.assert_not_called()


@pytest.mark.asyncio
async def test_restore_unit_rejects_cross_vault():
    """HIGH-005 (restore variant): cross-vault restore is rejected the same
    way as deprioritize.
    """
    from memex_common.exceptions import MemoryUnitNotFoundError

    unit_id = uuid4()
    real_vault = uuid4()
    other_vault = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=real_vault,
        type='fact',
        text='example',
        is_deprioritized=True,
    )
    service, metastore, audit_mock = _make_service(unit)

    with pytest.raises(MemoryUnitNotFoundError):
        await service.restore_unit(
            unit_id,
            vault_id=other_vault,
            actor='attacker',
            background_tasks=None,
        )

    assert unit.is_deprioritized is True  # unchanged
    assert metastore.commits == 0
    audit_mock.log.assert_not_called()


@pytest.mark.asyncio
async def test_set_unit_deprioritized_allows_matching_vault():
    """HIGH-005 happy path: matching vault_id flips the flag and writes an
    audit row.
    """
    unit_id = uuid4()
    vault_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=vault_id,
        type='fact',
        text='example',
        is_deprioritized=False,
    )
    service, metastore, audit_mock = _make_service(unit)

    result = await service.set_unit_deprioritized(
        unit_id,
        reason='in-scope',
        vault_id=vault_id,
        actor='agent:claude',
        background_tasks=None,
    )

    assert result.is_deprioritized is True
    assert metastore.commits == 1
    assert audit_mock.log.call_count == 1


@pytest.mark.asyncio
async def test_set_unit_deprioritized_skips_vault_check_when_none():
    """Legacy path: when vault_id is None (in-process CLI / tests) the
    service must not impose a vault check.
    """
    unit_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        vault_id=uuid4(),
        type='fact',
        text='example',
        is_deprioritized=False,
    )
    service, metastore, audit_mock = _make_service(unit)

    result = await service.set_unit_deprioritized(
        unit_id, reason='no vault', actor='cli', background_tasks=None
    )
    assert result.is_deprioritized is True
    assert metastore.commits == 1
    assert audit_mock.log.call_count == 1
