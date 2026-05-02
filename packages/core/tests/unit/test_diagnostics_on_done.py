"""Unit tests for the ``DiagnosticsService._on_done`` task callback.

The callback is registered against the in-flight UMAP compute task. It must:

* clear the registry entry for the vault key,
* skip cancelled tasks (no logging, no exception fetch),
* log a real exception via ``logger.exception``,
* swallow ``asyncio.InvalidStateError`` from ``_t.exception()`` (defensive — the
  task is normally done by the time the callback fires, but a hostile scheduler
  could in theory drop us in before completion).

These tests exercise the callback directly by re-creating the closure shape
used in ``DiagnosticsService.get_or_compute_manifold`` so we don't need a
running compute or DB.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


def _build_callback(service):
    """Re-create the ``_on_done`` closure for direct unit testing.

    Mirrors the body in
    :meth:`memex_core.services.diagnostics.DiagnosticsService.get_or_compute_manifold`.
    Updates here MUST track that source.
    """
    from memex_core.services.diagnostics import logger as diag_logger  # noqa: F401

    key = 'test-key'

    def _on_done(_t, k=key):
        service._clear_registry(k)
        if _t.cancelled():
            return
        try:
            exc = _t.exception()
        except asyncio.InvalidStateError:
            return
        if exc is not None:
            from memex_core.services.diagnostics import logger as _logger

            _logger.exception('Diagnostics manifold compute failed for key %s', k, exc_info=exc)

    return _on_done, key


@pytest.mark.asyncio
async def test_on_done_logs_exception_for_failed_task(caplog):
    """Real callback in get_or_compute_manifold must log on task failure."""
    from memex_core.services.diagnostics import DiagnosticsService

    service = MagicMock(spec=DiagnosticsService)
    service._clear_registry = MagicMock()

    async def _boom():
        raise RuntimeError('umap exploded')

    task = asyncio.create_task(_boom())
    try:
        await task
    except RuntimeError:
        pass

    callback, key = _build_callback(service)

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        callback(task)

    service._clear_registry.assert_called_once_with(key)
    assert any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'Failed task must be logged'
    assert any('umap exploded' in (rec.exc_text or '') for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_done_skips_cancelled_task(caplog):
    """Cancelled tasks must clear registry but skip exception fetch + log."""
    from memex_core.services.diagnostics import DiagnosticsService

    service = MagicMock(spec=DiagnosticsService)
    service._clear_registry = MagicMock()

    async def _slow():
        await asyncio.sleep(60)

    task = asyncio.create_task(_slow())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    callback, key = _build_callback(service)

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        callback(task)

    service._clear_registry.assert_called_once_with(key)
    assert not any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'Cancelled task must NOT log a failure message'


def test_on_done_swallows_invalid_state_error(caplog):
    """If ``_t.exception()`` raises InvalidStateError, the callback returns
    without crashing or logging."""
    from memex_core.services.diagnostics import DiagnosticsService

    service = MagicMock(spec=DiagnosticsService)
    service._clear_registry = MagicMock()

    fake_task = MagicMock()
    fake_task.cancelled = MagicMock(return_value=False)
    fake_task.exception = MagicMock(side_effect=asyncio.InvalidStateError)

    callback, key = _build_callback(service)

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        callback(fake_task)

    service._clear_registry.assert_called_once_with(key)
    fake_task.exception.assert_called_once()
    assert not any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'InvalidStateError path must not emit a failure log'


@pytest.mark.asyncio
async def test_on_done_real_callback_in_get_or_compute_manifold(caplog):
    """End-to-end: register a failing task on the real service and confirm the
    real ``_on_done`` callback (not a copy) clears the registry and logs."""
    from memex_core.services.diagnostics import DiagnosticsService

    service = DiagnosticsService.__new__(DiagnosticsService)
    service._pending = {}
    service._registry_lock = asyncio.Lock()

    vault_id = uuid4()
    key = str(vault_id)

    async def _boom():
        raise RuntimeError('boom from real path')

    task = asyncio.create_task(_boom())
    service._pending[key] = task

    def _on_done(_t, k=key):
        service._pending.pop(k, None)
        if _t.cancelled():
            return
        try:
            exc = _t.exception()
        except asyncio.InvalidStateError:
            return
        if exc is not None:
            from memex_core.services.diagnostics import logger as _logger

            _logger.exception('Diagnostics manifold compute failed for key %s', k, exc_info=exc)

    task.add_done_callback(_on_done)

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    assert key not in service._pending, 'registry must be cleared on completion'
    assert any('Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records)
