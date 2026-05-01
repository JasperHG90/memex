"""F38 ConsolidationService — F9 lock-acquire/release contract (CRIT-002 unit).

Source-level + behavioural unit tests verifying that ``tick()`` wraps each
per-entity iteration in ``acquire_entity_lock`` so F38 cannot race with
``memex_memory_reconsolidate`` on the same MentalModel.
"""

from __future__ import annotations

import inspect
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memex_core.services import consolidation
from memex_core.services.consolidation import ConsolidationService

_SOURCE = inspect.getsource(consolidation)


def test_tick_imports_and_uses_acquire_entity_lock():
    """tick() must reference ``acquire_entity_lock`` and ``EntityLockTimeoutError``
    so the F9 lock cannot be silently removed in a future refactor."""
    assert 'acquire_entity_lock(' in _SOURCE, (
        'CRIT-002 regression: ConsolidationService no longer calls acquire_entity_lock — '
        'F38 would race with memex_memory_reconsolidate on the same MentalModel.'
    )
    assert 'EntityLockTimeoutError' in _SOURCE, (
        'tick() must import EntityLockTimeoutError to handle lock contention'
    )


def test_lock_acquire_is_inside_tick_per_entity_loop():
    """The acquire_entity_lock call site must live inside ``async def tick``."""
    src = _SOURCE
    tick_match = re.search(r'async def tick\b', src)
    assert tick_match, 'tick() not found in source'
    # Find the next top-level `async def` after tick to bound the search.
    tick_start = tick_match.start()
    next_def = re.search(r'\n    async def\s', src[tick_start + 1 :])
    tick_end = (tick_start + 1 + next_def.start()) if next_def else len(src)
    tick_body = src[tick_start:tick_end]
    assert 'acquire_entity_lock(' in tick_body, (
        'acquire_entity_lock must be called inside tick(), not at module level'
    )


@pytest.mark.asyncio
async def test_tick_acquires_and_releases_lock_per_entity(monkeypatch):
    """Behavioural: each entity yields exactly one matched acquire/release
    pair; reflection runs only between acquire and release."""
    vault_id = uuid4()
    entity_a = uuid4()
    entity_b = uuid4()
    unit_a = uuid4()
    unit_b = uuid4()
    unit_to_entity = {unit_a: entity_a, unit_b: entity_b}

    events: list[tuple[str, UUID]] = []

    @asynccontextmanager
    async def _fake_lock(_dsn, eid, *, timeout_seconds):
        events.append(('acquire', eid))
        try:
            yield
        finally:
            events.append(('release', eid))

    monkeypatch.setattr(consolidation, 'acquire_entity_lock', _fake_lock)

    metastore = MagicMock()

    # Patch out the DB-touching methods so we do not need a real Postgres.
    config = MagicMock()
    config.server.meta_store.instance.connection_string = (
        'postgresql+asyncpg://x:y@localhost:5432/db'
    )

    async def _record_contradiction(**kw):
        # Map unit_id back to entity_id for ordering checks.
        events.append(('contradiction', unit_to_entity[kw['unit_ids'][0]]))

    async def _record_reflection(reqs):
        events.append(('reflection', reqs[0].entity_id))
        return []

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock(side_effect=_record_contradiction)
    reflection = MagicMock()
    reflection.reflect_batch = AsyncMock(side_effect=_record_reflection)

    svc = ConsolidationService(
        metastore=metastore,
        config=config,
        reflection=reflection,
        contradiction=contradiction_spy,
    )

    # Stub out the diff-selection + grouping + stale-filter so we can drive
    # the per-entity loop deterministically.
    svc.get_last_tick_timestamp = AsyncMock(return_value=None)  # type: ignore
    svc.select_diff_units = AsyncMock(return_value=[unit_a, unit_b])  # type: ignore
    svc._resolve_unit_ids_grouped_by_entity = AsyncMock(  # type: ignore
        return_value={entity_a: [unit_a], entity_b: [unit_b]}
    )
    svc._select_already_stale_ids = AsyncMock(return_value=[])  # type: ignore
    svc._write_tick_summary = AsyncMock(return_value=uuid4())  # type: ignore

    # `metastore.session()` is used for diff/staleness reads; return a mocked
    # async context that yields a sentinel session (we stubbed the methods
    # that would have used it, so the body is unused).
    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()

    metastore.session = MagicMock(side_effect=_fake_session)
    metastore.session_maker = MagicMock(return_value=MagicMock())

    result = await svc.tick(vault_id)

    # Both entities processed; no deferrals.
    assert result['entities_reflected'] == 2
    assert result['entities_deferred'] == 0
    assert result['error'] is None

    # Extract acquire/release pairs and verify each entity has exactly one
    # matched pair, with the inner work happening between them.
    for eid in (entity_a, entity_b):
        eid_events = [e for e in events if e[1] == eid]
        # Expect: acquire, contradiction, reflection, release (in that order).
        kinds = [k for k, _ in eid_events]
        assert kinds == ['acquire', 'contradiction', 'reflection', 'release'], (
            f'lock+work ordering for {eid}: {kinds}'
        )


@pytest.mark.asyncio
async def test_tick_releases_lock_when_reflection_raises(monkeypatch):
    """If the inner work raises, the lock is still released by the
    context-manager — the release event must fire after the exception."""
    from memex_core.services.consolidation import ConsolidationService

    vault_id = uuid4()
    entity_id = uuid4()
    unit_id = uuid4()

    events: list[tuple[str, UUID]] = []

    @asynccontextmanager
    async def _fake_lock(_dsn, eid, *, timeout_seconds):
        events.append(('acquire', eid))
        try:
            yield
        finally:
            events.append(('release', eid))

    monkeypatch.setattr(consolidation, 'acquire_entity_lock', _fake_lock)

    config = MagicMock()
    config.server.meta_store.instance.connection_string = (
        'postgresql+asyncpg://x:y@localhost:5432/db'
    )

    metastore = MagicMock()

    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()

    metastore.session = MagicMock(side_effect=_fake_session)
    metastore.session_maker = MagicMock(return_value=MagicMock())

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection = MagicMock()
    reflection.reflect_batch = AsyncMock(side_effect=RuntimeError('boom'))

    svc = ConsolidationService(
        metastore=metastore,
        config=config,
        reflection=reflection,
        contradiction=contradiction_spy,
    )

    svc.get_last_tick_timestamp = AsyncMock(return_value=None)  # type: ignore
    svc.select_diff_units = AsyncMock(return_value=[unit_id])  # type: ignore
    svc._resolve_unit_ids_grouped_by_entity = AsyncMock(  # type: ignore
        return_value={entity_id: [unit_id]}
    )
    svc._select_already_stale_ids = AsyncMock(return_value=[])  # type: ignore
    svc._write_tick_summary = AsyncMock(return_value=uuid4())  # type: ignore

    result = await svc.tick(vault_id)

    assert result['error'] is not None and 'boom' in result['error']
    kinds = [k for k, _ in events]
    assert kinds == ['acquire', 'release'], f'lock not released on exception: events={events}'
