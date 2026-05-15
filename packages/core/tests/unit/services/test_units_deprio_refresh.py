"""Units service: deprio enqueues refresh; observation-id raises 400 path.

V21 invariants verified here (no DB; the SQL path is exercised in
``packages/core/tests/integration/``):

* ``set_unit_deprioritized`` accepts ``defer_observation_refresh=True``.
* ``batch_set_unit_deprioritized`` accepts a ``vault_id`` kwarg so the
  post-commit flush helper can be invoked.
* ``flush_deferred_observation_refresh`` returns 0 on an empty input
  without opening a session.
* The deprio path produces an ``ObservationReadOnlyError`` when the
  unit_id maps to an observation (verified via mocks of the helper).
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_common.exceptions import ObservationReadOnlyError


def test_set_unit_deprioritized_carries_defer_kwarg():
    from memex_core.services.units import UnitsService

    sig = inspect.signature(UnitsService.set_unit_deprioritized)
    assert 'defer_observation_refresh' in sig.parameters
    assert sig.parameters['defer_observation_refresh'].default is False


def test_batch_set_unit_deprioritized_carries_vault_kwarg():
    from memex_core.services.units import UnitsService

    sig = inspect.signature(UnitsService.batch_set_unit_deprioritized)
    assert 'vault_id' in sig.parameters
    assert sig.parameters['vault_id'].default is None


@pytest.mark.asyncio
async def test_flush_deferred_observation_refresh_empty_input_returns_zero():
    """Empty input must short-circuit before opening a session."""
    from memex_core.services.units import UnitsService

    service = UnitsService.__new__(UnitsService)
    service.metastore = MagicMock()  # any call to .session() would assert below
    service.config = MagicMock()
    count = await service.flush_deferred_observation_refresh(unit_ids=[], vault_id=uuid4())
    assert count == 0
    service.metastore.session.assert_not_called()


@pytest.mark.asyncio
async def test_observation_read_only_error_raised_when_unit_id_is_observation():
    """The _flip_deprioritized pre-resolve must raise ObservationReadOnlyError
    when the unit_id maps to an Observation inside any MentalModel.

    We mock the JSONB-scan helper and the session.get so the path under
    test is just the branch logic.
    """
    from memex_core.services.units import UnitsService

    service = UnitsService.__new__(UnitsService)
    service.metastore = MagicMock()
    service.config = MagicMock()
    service._audit_service = None

    # session.get returns None — no MU row.
    session = AsyncMock()
    session.get.return_value = None
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    service.metastore.session.return_value = cm

    source_mu = uuid4()

    async def fake_find(*a, **kw):
        return [source_mu]

    service._find_source_mus_for_observation = fake_find  # type: ignore[attr-defined]

    with pytest.raises(ObservationReadOnlyError) as exc:
        await service._flip_deprioritized(
            uuid4(),
            value=True,
            action='memory_deprioritize',
            details={'reason': 'x'},
            vault_id=uuid4(),
            actor=None,
            background_tasks=None,
        )
    assert exc.value.source_memory_units == [source_mu]


def test_enqueue_priority_reflect_method_exists_on_queue_service():
    from memex_core.memory.reflect.queue_service import ReflectionQueueService

    assert hasattr(ReflectionQueueService, 'enqueue_priority_reflect')
    sig = inspect.signature(ReflectionQueueService.enqueue_priority_reflect)
    assert 'entity_ids' in sig.parameters
    assert 'vault_id' in sig.parameters


def test_complete_reflection_filters_task_type():
    """complete_reflection's body filters by task_type='reflect' so refresh rows
    aren't nuked on a reflect ack."""
    import textwrap

    from memex_core.memory.reflect import queue_service

    src = inspect.getsource(queue_service.ReflectionQueueService.complete_reflection)
    src_normalized = textwrap.dedent(src)
    assert (
        "task_type) == 'reflect'" in src_normalized or 'task_type) == "reflect"' in src_normalized
    )
