"""Unit tests for the ``DiagnosticsService._on_done`` task callback.

The callback is registered against the in-flight UMAP compute task. It must:

* clear the registry entry for the vault key,
* skip cancelled tasks (no logging, no exception fetch),
* log a real exception via ``logger.exception``,
* swallow ``asyncio.InvalidStateError`` from ``_t.exception()`` (defensive — the
  task is normally done by the time the callback fires, but a hostile scheduler
  could in theory drop us in before completion).

The first three tests drive the real ``get_or_compute_manifold`` path so the
production callback registered via ``task.add_done_callback(_on_done)`` is the
one being exercised. The InvalidStateError test calls the registered callback
directly with a fake task because that scheduler-race condition cannot be
reproduced via the public API.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


def _make_service():
    """Construct a ``DiagnosticsService`` without invoking the real ``__init__``.

    Bypasses the dependency wiring (metastore/filestore/config) — only the
    registry state needed by ``get_or_compute_manifold`` and ``_on_done`` is
    populated.
    """
    from memex_core.services.diagnostics import DiagnosticsService

    service = DiagnosticsService.__new__(DiagnosticsService)
    service._pending = {}
    service._registry_lock = asyncio.Lock()
    return service


@pytest.mark.asyncio
async def test_on_done_logs_exception_for_failed_task(caplog):
    """Real callback registered by get_or_compute_manifold must log on failure."""
    service = _make_service()
    vault_id = uuid4()
    key = str(vault_id)

    async def _boom(_vault_id):
        raise RuntimeError('umap exploded')

    service._compute_and_cache = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        status, payload = await service.get_or_compute_manifold(vault_id, force_refresh=True)
        assert status == 'computing'
        # Drain the just-scheduled task so the registered _on_done fires.
        task = service._pending.get(key)
        assert task is not None, 'task must be registered before completion'
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    assert key not in service._pending, 'registry must be cleared on completion'
    assert any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'Failed task must be logged'
    assert any('umap exploded' in (rec.exc_text or '') for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_done_skips_cancelled_task(caplog):
    """Cancelled tasks must clear registry but skip exception fetch + log."""
    service = _make_service()
    vault_id = uuid4()
    key = str(vault_id)

    async def _slow(_vault_id):
        await asyncio.sleep(60)

    service._compute_and_cache = _slow  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        status, _ = await service.get_or_compute_manifold(vault_id, force_refresh=True)
        assert status == 'computing'
        task = service._pending.get(key)
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    assert key not in service._pending, 'registry must be cleared on cancellation'
    assert not any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'Cancelled task must NOT log a failure message'


@pytest.mark.asyncio
async def test_on_done_swallows_invalid_state_error(caplog):
    """If ``_t.exception()`` raises InvalidStateError, the registered callback
    returns without crashing or logging.

    This race (callback fires before task is in a terminal state) cannot be
    reproduced via the public API — the asyncio scheduler always marks the
    task done before invoking done-callbacks. Instead we extract the real
    callback the production code registered, then invoke it with a fake task
    whose ``exception()`` raises ``InvalidStateError``.
    """
    service = _make_service()
    vault_id = uuid4()
    key = str(vault_id)

    real_task_callbacks: list = []

    async def _slow(_vault_id):
        await asyncio.sleep(60)

    service._compute_and_cache = _slow  # type: ignore[method-assign]

    status, _ = await service.get_or_compute_manifold(vault_id, force_refresh=True)
    assert status == 'computing'
    real_task = service._pending[key]
    try:
        # Steal the registered callback. asyncio doesn't expose this directly,
        # but `_callbacks` is the documented attribute used by `remove_done_callback`.
        callbacks = list(getattr(real_task, '_callbacks', []))
        for cb_entry in callbacks:
            cb = cb_entry[0] if isinstance(cb_entry, tuple) else cb_entry
            if getattr(cb, '__name__', '') == '_on_done':
                real_task_callbacks.append(cb)
        assert real_task_callbacks, 'production code must register an _on_done callback'

        fake_task = MagicMock()
        fake_task.cancelled = MagicMock(return_value=False)
        fake_task.exception = MagicMock(side_effect=asyncio.InvalidStateError)

        # Pre-populate the registry so we can confirm the callback clears it
        # even when the task is in an InvalidState.
        service._pending[key] = real_task

        with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
            real_task_callbacks[0](fake_task)

        fake_task.exception.assert_called_once()
        assert key not in service._pending, 'registry must be cleared even on InvalidStateError'
        assert not any(
            'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
        ), 'InvalidStateError path must not emit a failure log'
    finally:
        real_task.cancel()
        try:
            await real_task
        except asyncio.CancelledError:
            pass
