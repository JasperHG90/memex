from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from memex_core.config import MemexConfig
from memex_core.diagnostics.lint_dashboard import aggregate_lint_findings
from memex_core.diagnostics.summary import compute_diagnostics_summary
from memex_core.diagnostics.umap import (
    UMAPNotInstalledError,
    compute_manifold,
    warm_cache_hit,
)
from memex_core.services.base import BaseService
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.diagnostics')


class DiagnosticsService(BaseService):
    """Owns the in-process pending-task registry for cold-cache UMAP compute.

    Concurrent cold requests for the same vault share a single asyncio.Task;
    exactly one compute fires per cold-cache window. Restart loses the
    registry — next request triggers a fresh compute (cache file persists).
    """

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
    ) -> None:
        super().__init__(metastore=metastore, filestore=filestore, config=config)
        self._pending: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._registry_lock = asyncio.Lock()

    async def get_summary(self, vault_id: UUID) -> dict[str, Any]:
        async with self._registry_lock:
            pending = self._pending.get(str(vault_id))
            in_flight = pending is not None and not pending.done()
        return await compute_diagnostics_summary(
            self.metastore,
            self.filestore,
            vault_id,
            pending_compute=in_flight,
        )

    async def get_lint_dashboard(self, vault_id: UUID) -> dict[str, Any]:
        """F26 — Pivot MaintenanceProposal rows by (lint_type, status, source) plus top-5 pending.

        Thin wrapper over :func:`memex_core.diagnostics.lint_dashboard.aggregate_lint_findings`.
        """
        return await aggregate_lint_findings(self.metastore, vault_id)

    async def get_or_compute_manifold(
        self,
        vault_id: UUID,
        *,
        force_refresh: bool = False,
    ) -> tuple[str, dict[str, Any] | None]:
        """Returns (status, payload). status ∈ {'ready', 'computing', 'unavailable', 'absent'}.

        - 'ready' → payload is the cached manifold dict (200 OK).
        - 'computing' → payload includes task_id (202 Accepted).
        - 'unavailable' → umap-learn not installed (501 Not Implemented).
        - 'absent' → no in-flight task and no cached manifold (404 Not Found);
          only emitted by :meth:`get_manifold_status`, never by this method.

        Note: ``force_refresh=True`` skips the warm-cache check but does NOT
        cancel an in-flight compute. If a task is already running for this
        vault, the existing task's id is returned. Cancellation is avoided
        because the in-flight task may have side effects (cache writes,
        metrics, traces) that should complete.
        """
        if not force_refresh:
            cached = await warm_cache_hit(self.filestore, self.metastore, vault_id)
            if cached is not None:
                return 'ready', cached

        async with self._registry_lock:
            key = str(vault_id)
            existing = self._pending.get(key)
            if existing is not None and not existing.done():
                return 'computing', {'task_id': _task_id_for(existing)}
            task = asyncio.create_task(self._compute_and_cache(vault_id))
            self._pending[key] = task

            def _on_done(_t: asyncio.Task[dict[str, Any]], k: str = key) -> None:
                self._clear_registry(k)
                if _t.cancelled():
                    return
                try:
                    exc = _t.exception()
                except asyncio.InvalidStateError:
                    return
                if exc is not None:
                    logger.exception(
                        'Diagnostics manifold compute failed for key %s', k, exc_info=exc
                    )

            task.add_done_callback(_on_done)
            return 'computing', {'task_id': _task_id_for(task)}

    async def get_manifold_status(
        self,
        vault_id: UUID,
        task_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Polling endpoint for an in-flight compute.

        Returns ('ready', payload) when done + cached, ('computing', {task_id}) if still running,
        or ('absent', None) if no matching task and no cached file.
        """
        async with self._registry_lock:
            pending = self._pending.get(str(vault_id))
        if pending is not None and _task_id_for(pending) == task_id:
            if pending.done():
                try:
                    payload = pending.result()
                except UMAPNotInstalledError:
                    return 'unavailable', None
                except Exception as e:
                    logger.exception('Manifold compute failed for vault %s: %s', vault_id, e)
                    return 'absent', None
                return 'ready', payload
            return 'computing', {'task_id': task_id}
        cached = await warm_cache_hit(self.filestore, self.metastore, vault_id)
        if cached is not None:
            return 'ready', cached
        return 'absent', None

    async def _compute_and_cache(self, vault_id: UUID) -> dict[str, Any]:
        return await compute_manifold(self.metastore, self.filestore, vault_id)

    def _clear_registry(self, key: str) -> None:
        self._pending.pop(key, None)

    async def shutdown(self) -> None:
        """Cancel all in-flight tasks; intended for app shutdown."""
        async with self._registry_lock:
            tasks = list(self._pending.values())
            self._pending.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _task_id_for(task: asyncio.Task[Any]) -> str:
    return f'{id(task):x}'
