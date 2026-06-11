"""Synchronous wrappers for the procedural-plane client.

Hermes calls memory-provider methods synchronously, but
:class:`memex_common.client.RemoteMemexAPI` is async-only. This module
is the procedural-plane counterpart to the inline ``run_sync(...)`` calls
in :mod:`memex_hermes_plugin.memex.provider` — a thin layer of
sync-shaped functions that marshal each call onto the shared event loop
in :mod:`memex_hermes_plugin.memex.async_bridge`.

The four methods here mirror the HTTP routes 1:1 (see the design §6):

* ``get``          → GET    /procedural/{id}
* ``get_by_identity`` → GET  /procedural/by-identity
* ``search``       → POST   /procedural/search
* ``case_submit``  → POST   /cases

All four accept and return the same DTOs the async client does — the
sync wrapper is purely a coroutine-marshalling shim, not a re-shape.
Callers in Hermes can import this module and call straight through.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from memex_common.procedural_schemas import (
    CaseSubmit,
    CaseSubmitResult,
    ProceduralEntryDTO,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    ShortLabel,
)

from .async_bridge import run_sync

#: Default timeout for a procedural-plane round-trip. The plane's writes
#: are O(1) (a single INSERT) but the search path can do BM25+vector+RRF
#: fusion over many rows; 30s is the same budget the briefing fetch uses.
_DEFAULT_TIMEOUT: float = 30.0


def get(
    api: Any,
    entry_id: UUID,
    *,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO:
    """Fetch a single entry by UUID. 404 on miss or vault mismatch."""
    return run_sync(api.procedural_get(entry_id, vault_id=vault_id), timeout=timeout)


def get_by_identity(
    api: Any,
    kind: ShortLabel,
    scope: ShortLabel,
    *,
    verb: ShortLabel | None = None,
    context: ShortLabel | None = None,
    vault_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralEntryDTO | None:
    """Look up a single entry by its (kind, scope, verb, context) anchor.

    Returns ``None`` when the anchor is unbound — the cheap
    "did we already learn this?" probe. Never raises 404.
    """
    return run_sync(
        api.procedural_get_by_identity(kind, scope, verb=verb, context=context, vault_id=vault_id),
        timeout=timeout,
    )


def search(
    api: Any,
    request: ProceduralSearchRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ProceduralSearchResponse:
    """Hybrid BM25 + vector search (RRF-merged) across the procedural plane."""
    return run_sync(api.procedural_search(request), timeout=timeout)


def case_submit(
    api: Any,
    payload: CaseSubmit,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> CaseSubmitResult:
    """Submit a worked episode as a case (§5.1).

    The note lands in the hidden ``procedural`` system vault with
    ``role='case'``; assignment runs synchronously (explicit ``case_of``
    / judge auto-assign / lint escalation — see the result envelope).
    """
    return run_sync(api.case_submit(payload), timeout=timeout)


__all__ = [
    'get',
    'get_by_identity',
    'search',
    'case_submit',
]
