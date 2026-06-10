"""Controller + pure helpers for the procedural-plane curation TUI.

Mirrors the cockpit pattern (``memex_cli.cockpit.controller``): a
``Protocol`` client surface so unit tests pass a fake without HTTP, a
controller holding the async fetch/mutate methods, and free pure
helpers (context-key validation, version diff) that carry no I/O.

The TUI curates the §19.8 pin chain — global → project:<id> →
app:<consumer> — and the §18.8 non-destructive version ledger. Pins
have NO ``user`` context (per-user curation rides app/project
contexts; JG decision 2026-06-10).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

# Pin / scope grammar — global | project:<id> | app:<id>[:<sub>].
# Mirrors memex_common.procedural_schemas.SCOPE_PATTERN; duplicated here
# so the TUI gives a fast local error before a round-trip (and so the
# helper is unit-testable without importing the server schemas).
_CONTEXT_KEY_PATTERN = re.compile(
    r'^(global|project:[A-Za-z0-9._-]+|app:[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?)$'
)

PIN_CAP_PER_CONTEXT = 10


def validate_context_key(value: str) -> str:
    """Validate a pin context key against the §19.8 grammar.

    Raises ``ValueError`` with an actionable message. ``user`` is called
    out because it is a valid *KV* scope and the most likely wrong
    carry-over onto the plane.
    """
    candidate = (value or '').strip()
    if not _CONTEXT_KEY_PATTERN.match(candidate):
        hint = ''
        if candidate == 'user' or candidate.startswith('user:'):
            hint = (
                ' The procedural plane has no user context — per-user curation '
                'is done by pinning into project/app contexts, not a user one.'
            )
        raise ValueError(
            f'context key {candidate!r} must be global | project:<id> | app:<id>.{hint}'
        )
    return candidate


def unified_version_diff(older: Any, newer: Any) -> str:
    """Unified diff between two version DTOs over title + trigger + body.

    Accepts anything with ``.version``, ``.title``, ``.trigger``,
    ``.body`` attributes (the real ``ProceduralEntryVersionDTO`` or a
    test stub). Returns '' when the rendered snapshots are identical.
    """

    def _render(v: Any) -> list[str]:
        body = getattr(v, 'body', '') or ''
        trigger = getattr(v, 'trigger', '') or ''
        title = getattr(v, 'title', '') or ''
        return f'title: {title}\ntrigger: {trigger}\n\n{body}'.splitlines(keepends=True)

    diff = difflib.unified_diff(
        _render(older),
        _render(newer),
        fromfile=f'v{getattr(older, "version", "?")}',
        tofile=f'v{getattr(newer, "version", "?")}',
    )
    return ''.join(diff)


@dataclass(frozen=True)
class ChainContext:
    """One context in the assembled briefing chain + its pin count."""

    context_key: str
    pin_count: int

    @property
    def is_full(self) -> bool:
        return self.pin_count >= PIN_CAP_PER_CONTEXT

    @property
    def capacity_label(self) -> str:
        return f'{self.pin_count}/{PIN_CAP_PER_CONTEXT}'


def build_chain(project_id: str | None, app: str | None) -> list[str]:
    """The layered pin-context chain, most-general first (§19.8)."""
    chain = ['global']
    if project_id:
        chain.append(f'project:{project_id}')
    if app:
        chain.append(f'app:{app}')
    return chain


class ProceduralCurationClient(Protocol):
    """Subset of ``RemoteMemexAPI`` the curation TUI depends on.

    A Protocol so unit tests pass a fake. The real client
    (``memex_common.client.RemoteMemexAPI``) satisfies it naturally.
    """

    async def procedural_search(self, request: Any) -> Any: ...

    async def procedural_list_pins(
        self, context_key: str, *, limit: int | None = None
    ) -> list[Any]: ...

    async def procedural_pin(
        self,
        entry_id: UUID,
        *,
        context_key: str,
        position: int | None = None,
        pinned_by: str | None = None,
    ) -> Any: ...

    async def procedural_unpin(self, entry_id: UUID, *, context_key: str) -> int: ...

    async def procedural_briefing_cards(
        self,
        context_keys: list[str],
        *,
        scope: str | None = None,
        limit_per_context: int = 5,
    ) -> Any: ...

    async def procedural_list_versions(self, entry_id: UUID) -> list[Any]: ...

    async def procedural_rollback(
        self, entry_id: UUID, version: int, *, rolled_back_by: str | None = None
    ) -> Any: ...

    async def procedural_get(self, entry_id: UUID, *, vault_id: UUID | None = None) -> Any: ...


class ProceduralCurationController:
    """Async data + mutation surface for the curation TUI.

    Holds no Textual state — the App owns the widgets and calls these.
    Every mutation validates the context key locally first so a typo
    surfaces as an inline error, not a 422 round-trip.
    """

    PINNED_BY = 'memex-tui'

    def __init__(self, client: ProceduralCurationClient) -> None:
        self._client = client

    async def search(self, query: str, *, limit: int = 20) -> list[Any]:
        """Hybrid search for entries to curate. Empty query → []."""
        if not query or not query.strip():
            return []
        from memex_common.procedural_schemas import ProceduralSearchRequest

        response = await self._client.procedural_search(
            ProceduralSearchRequest(query=query.strip(), status='published', limit=limit)
        )
        return [hit.entry for hit in getattr(response, 'hits', [])]

    async def list_pins(self, context_key: str) -> list[Any]:
        key = validate_context_key(context_key)
        return await self._client.procedural_list_pins(key)

    async def context_state(self, context_key: str) -> ChainContext:
        pins = await self.list_pins(context_key)
        return ChainContext(context_key=validate_context_key(context_key), pin_count=len(pins))

    async def pin(self, entry_id: UUID, context_key: str) -> Any:
        """Append an entry to a context chain. Server enforces the cap."""
        key = validate_context_key(context_key)
        return await self._client.procedural_pin(
            entry_id, context_key=key, position=None, pinned_by=self.PINNED_BY
        )

    async def unpin(self, entry_id: UUID, context_key: str) -> int:
        key = validate_context_key(context_key)
        return await self._client.procedural_unpin(entry_id, context_key=key)

    async def briefing_preview(self, chain: list[str]) -> Any:
        keys = [validate_context_key(k) for k in chain]
        return await self._client.procedural_briefing_cards(
            keys, limit_per_context=PIN_CAP_PER_CONTEXT
        )

    async def list_versions(self, entry_id: UUID) -> list[Any]:
        return await self._client.procedural_list_versions(entry_id)

    async def rollback(self, entry_id: UUID, version: int) -> Any:
        return await self._client.procedural_rollback(
            entry_id, version, rolled_back_by=self.PINNED_BY
        )


__all__ = [
    'PIN_CAP_PER_CONTEXT',
    'ChainContext',
    'ProceduralCurationClient',
    'ProceduralCurationController',
    'build_chain',
    'unified_version_diff',
    'validate_context_key',
]
