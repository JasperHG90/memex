"""Unit tests for the diagnostics task-completion callback.

The callback (``_handle_diagnostics_task_completion``) is registered against
the in-flight UMAP compute task. It must:

* clear the registry entry for the vault key,
* skip cancelled tasks (no logging, no exception fetch),
* log a real exception with a populated traceback,
* swallow ``asyncio.InvalidStateError`` from ``task.exception()`` (defensive —
  the task is normally done by the time the callback fires, but a hostile
  scheduler could in theory drop us in before completion).

The first two tests drive the real ``get_or_compute_manifold`` path so the
production callback registered via ``task.add_done_callback`` is the one
being exercised. The InvalidStateError test calls the module-level
``_handle_diagnostics_task_completion`` directly with a fake task — the
callback was extracted to module scope precisely to avoid monkey-patching
``Task.add_done_callback`` on a real asyncio.Task (fragile under PyPy/uvloop).
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
    registry state needed by ``get_or_compute_manifold`` and the done-callback
    is populated.
    """
    from memex_core.services.diagnostics import DiagnosticsService

    service = DiagnosticsService.__new__(DiagnosticsService)
    service._pending = {}
    service._registry_lock = asyncio.Lock()
    return service


def _force_format_exc_text(records: list[logging.LogRecord]) -> None:
    """Lazy-populate ``LogRecord.exc_text`` for any record carrying ``exc_info``.

    ``LogCaptureHandler`` does not call ``Formatter.format()``, so ``exc_text``
    stays ``None`` even when a traceback was logged. Without this, the assertion
    ``'... in (rec.exc_text or "")`` silently passes for the wrong reason.
    Mirrors ``test_batch_manager.test_task_done_callback_captures_exception_traceback``.
    """
    formatter = logging.Formatter()
    for record in records:
        if record.exc_info:
            formatter.format(record)


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
        # Drain the just-scheduled task so the registered callback fires.
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

    # exc_info is populated at LogRecord construction time — assert on it
    # directly rather than relying on lazily-formatted exc_text.
    exc_info_records = [rec for rec in caplog.records if rec.exc_info]
    assert exc_info_records, 'failed task must produce a record carrying exc_info'
    assert any(
        rec.exc_info is not None and 'umap exploded' in str(rec.exc_info[1])
        for rec in exc_info_records
    ), 'exc_info must reference the originating RuntimeError'

    # Belt-and-braces: also force exc_text formatting and check it for parity
    # with test_batch_manager (catches a regression where the explicit
    # exc_info tuple is dropped and the formatter has no traceback to render).
    _force_format_exc_text(caplog.records)
    assert any('umap exploded' in (rec.exc_text or '') for rec in caplog.records), (
        'rendered traceback must reference the exception message'
    )


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


def test_on_done_swallows_invalid_state_error(caplog):
    """If ``task.exception()`` raises InvalidStateError, the callback returns
    without crashing or logging.

    This race (callback fires before task is in a terminal state) cannot be
    reproduced via the public API — the asyncio scheduler always marks the
    task done before invoking done-callbacks. The done-callback body lives at
    module scope (``_handle_diagnostics_task_completion``) precisely so this
    test can call it directly with a fake task. No monkey-patching of
    ``Task.add_done_callback`` required, no reliance on CPython's
    instance-level method replacement (fragile under PyPy/uvloop).
    """
    from memex_core.services.diagnostics import _handle_diagnostics_task_completion

    service = _make_service()
    vault_id = uuid4()
    key = str(vault_id)

    fake_task = MagicMock()
    fake_task.cancelled = MagicMock(return_value=False)
    fake_task.exception = MagicMock(side_effect=asyncio.InvalidStateError)

    # Pre-populate the registry so we can confirm the callback clears it
    # even when the task is in an InvalidState.
    service._pending[key] = MagicMock(spec=asyncio.Task)

    with caplog.at_level(logging.ERROR, logger='memex.core.services.diagnostics'):
        _handle_diagnostics_task_completion(service, key, fake_task)

    fake_task.exception.assert_called_once()
    assert key not in service._pending, 'registry must be cleared even on InvalidStateError'
    assert not any(
        'Diagnostics manifold compute failed' in rec.getMessage() for rec in caplog.records
    ), 'InvalidStateError path must not emit a failure log'


def test_on_done_clear_registry_failure_does_not_swallow_task_exception(caplog):
    """Round-10 regression: a failure inside ``_clear_registry`` MUST NOT
    bypass the task-exception logging branch.

    The done-callback wraps ``service._clear_registry(key)`` in try/except
    specifically to defend against this swallow. If a future refactor drops
    the wrapper (or replaces it with bare ``service._clear_registry(key)``),
    the registry-clear exception would propagate out of the callback before
    we ever reach ``task.exception()``, and the task's real failure would
    never hit the logs. This test pins the defense by:

    * mocking ``_clear_registry`` to raise ``RuntimeError('clear failed')``
    * passing a task whose ``exception()`` returns the real task error
    * asserting BOTH log records appear (registry-clear failure AND
      original task exception, the latter with ``exc_info`` populated).
    """
    from memex_core.services import diagnostics as diagnostics_module
    from memex_core.services.diagnostics import _handle_diagnostics_task_completion

    service = _make_service()
    vault_id = uuid4()
    key = str(vault_id)

    real_task_exc = RuntimeError('umap exploded')
    fake_task = MagicMock()
    fake_task.cancelled = MagicMock(return_value=False)
    # Set ``__traceback__`` so the explicit exc_info tuple in the callback has
    # a non-None traceback slot (matches what asyncio normally provides).
    try:
        raise real_task_exc
    except RuntimeError as e:
        captured = e
    fake_task.exception = MagicMock(return_value=captured)

    # Patch the bound method to raise; ``_handle_diagnostics_task_completion``
    # calls ``service._clear_registry(key)`` so we replace the instance attribute.
    def _boom(_key: str) -> None:
        raise RuntimeError('clear failed')

    service._clear_registry = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger=diagnostics_module.logger.name):
        _handle_diagnostics_task_completion(service, key, fake_task)

    # (a) registry-clear failure must be logged with exc_info (logger.exception).
    clear_records = [
        rec for rec in caplog.records if 'Failed to clear diagnostics registry' in rec.getMessage()
    ]
    assert clear_records, 'registry-clear failure must produce a log record'
    assert clear_records[0].exc_info is not None, (
        'registry-clear failure must carry exc_info from logger.exception'
    )
    assert 'clear failed' in str(clear_records[0].exc_info[1])

    # (b) original task exception must STILL be logged with exc_info — the
    # whole point of the try/except wrapper around ``_clear_registry``.
    task_records = [
        rec for rec in caplog.records if 'Diagnostics manifold compute failed' in rec.getMessage()
    ]
    assert task_records, (
        'task exception MUST be logged even when _clear_registry failed; '
        'the try/except around _clear_registry exists precisely to prevent '
        'this swallow'
    )
    assert task_records[0].exc_info is not None
    assert task_records[0].exc_info[1] is captured
    assert 'umap exploded' in str(task_records[0].exc_info[1])

    # Two distinct records — one per failure surface.
    assert len(clear_records) == 1
    assert len(task_records) == 1
    assert clear_records[0] is not task_records[0]

    # ``task.exception()`` MUST have been consulted — proves we reached the
    # post-clear branch even though clear raised.
    fake_task.exception.assert_called_once()
