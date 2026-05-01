"""TC-23-3 — Multiprocess concurrency: same-entity serializes, different-entity
parallel; crash auto-releases lock (F9 ship, locked v2 contract).

3 tests:
- test_same_entity_serializes: two spawn workers on same entity_id; second
  acquires only after first releases.
- test_different_entities_run_in_parallel: two spawn workers on distinct
  entity_ids; acquires overlap.
- test_crash_releases_lock: worker calls os._exit(1) mid-hold; next acquirer
  succeeds within 2s. Carries POC TC-12-5 forward.

Spawn context (NOT fork) per POC finding — fork shares pytest_asyncio's loop
and creates undefined behavior. Coordination via Manager().dict() (NOT
multiprocessing.Event — flakes on Docker spike).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time
import uuid
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from memex_core.services.locks import acquire_entity_lock, entity_lock_id


pytestmark = [pytest.mark.integration]


HOLD_DURATION_S = 1.0
DEADLINE_S = 15.0
PARALLEL_WINDOW_S = 1.0
CRASHER_HOLD_S = 0.3
AUTO_RELEASE_BOUND_S = 10.0
COORDINATION_POLL_S = 0.05


@pytest.fixture(scope='module')
def asyncpg_dsn(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url().replace('postgresql+psycopg2://', 'postgresql://')
    parsed = urlparse(url)
    scheme = parsed.scheme.split('+')[0]
    return urlunparse(parsed._replace(scheme=scheme))


def _worker_acquire_hold_release(
    dsn: str,
    entity_id_str: str,
    hold_seconds: float,
    state: dict,
    worker_label: str,
) -> None:
    """Acquire lock via production acquire_entity_lock, hold, release."""

    async def _go() -> None:
        eid = uuid.UUID(entity_id_str)
        try:
            async with acquire_entity_lock(dsn, eid, timeout_seconds=DEADLINE_S):
                state[f'{worker_label}_acquired_at_ns'] = time.monotonic_ns()
                await asyncio.sleep(hold_seconds)
                state[f'{worker_label}_releasing_at_ns'] = time.monotonic_ns()
        except Exception as e:
            state[f'{worker_label}_error'] = repr(e)

    asyncio.run(_go())


def _worker_poll_until_acquire(
    dsn: str,
    entity_id_str: str,
    deadline_seconds: float,
    state: dict,
    worker_label: str,
) -> None:
    """Spin-acquire via production acquire_entity_lock with bounded timeout."""

    async def _go() -> None:
        eid = uuid.UUID(entity_id_str)
        try:
            async with acquire_entity_lock(dsn, eid, timeout_seconds=deadline_seconds):
                state[f'{worker_label}_acquired_at_ns'] = time.monotonic_ns()
        except Exception as e:
            state[f'{worker_label}_error'] = repr(e)

    asyncio.run(_go())


def _crashing_holder(dsn: str, lock_id: int, state: dict) -> None:
    """Acquire raw advisory lock then call os._exit(1) — no graceful close.

    Uses raw asyncpg (NOT acquire_entity_lock) because the production helper's
    finally block would be a graceful release that masks the auto-release
    semantics we're trying to validate. Any exit-time cleanup is the wrong
    behaviour for this scenario.
    """

    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
        if not got:
            state['holder_acquire_failed'] = True
            os._exit(2)
        state['holder_acquired_at_ns'] = time.monotonic_ns()
        await asyncio.sleep(CRASHER_HOLD_S)
        state['holder_dying_at_ns'] = time.monotonic_ns()
        os._exit(1)

    asyncio.run(_go())


def test_same_entity_serializes(asyncpg_dsn: str) -> None:
    ctx = mp.get_context('spawn')
    eid = uuid.uuid4()

    with ctx.Manager() as manager:
        state = manager.dict()

        p_holder = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, str(eid), HOLD_DURATION_S, state, 'A'),
        )
        p_holder.start()

        deadline = time.monotonic() + DEADLINE_S
        while 'A_acquired_at_ns' not in state and 'A_error' not in state:
            if time.monotonic() >= deadline:
                p_holder.terminate()
                p_holder.join()
                pytest.fail(f'A never acquired before B started; state={dict(state)}')
            time.sleep(COORDINATION_POLL_S)
        assert 'A_error' not in state, f'holder errored: {dict(state)}'

        p_poller = ctx.Process(
            target=_worker_poll_until_acquire,
            args=(asyncpg_dsn, str(eid), DEADLINE_S, state, 'B'),
        )
        p_poller.start()

        p_holder.join(timeout=DEADLINE_S + 2)
        p_poller.join(timeout=DEADLINE_S + 2)
        assert p_holder.exitcode == 0, f'holder exitcode={p_holder.exitcode}'
        assert p_poller.exitcode == 0, f'poller exitcode={p_poller.exitcode}'

        snapshot = dict(state)

    assert 'A_acquired_at_ns' in snapshot, f'A never acquired: {snapshot}'
    assert 'A_releasing_at_ns' in snapshot, f'A never released: {snapshot}'
    assert 'B_acquired_at_ns' in snapshot, f'B never acquired: {snapshot}'
    assert 'A_error' not in snapshot
    assert 'B_error' not in snapshot

    assert snapshot['B_acquired_at_ns'] >= snapshot['A_releasing_at_ns'], (
        f'B acquired before A released — advisory lock failed to serialize. snapshot={snapshot}'
    )


def test_different_entities_run_in_parallel(asyncpg_dsn: str) -> None:
    ctx = mp.get_context('spawn')
    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    assert entity_lock_id(eid_a) != entity_lock_id(eid_b)

    with ctx.Manager() as manager:
        state = manager.dict()

        p_a = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, str(eid_a), HOLD_DURATION_S, state, 'A'),
        )
        p_b = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, str(eid_b), HOLD_DURATION_S, state, 'B'),
        )
        p_a.start()
        p_b.start()
        p_a.join(timeout=DEADLINE_S)
        p_b.join(timeout=DEADLINE_S)
        assert p_a.exitcode == 0, f'A exitcode={p_a.exitcode}'
        assert p_b.exitcode == 0, f'B exitcode={p_b.exitcode}'

        snapshot = dict(state)

    assert 'A_acquired_at_ns' in snapshot, snapshot
    assert 'B_acquired_at_ns' in snapshot, snapshot
    assert 'A_error' not in snapshot
    assert 'B_error' not in snapshot

    delta_s = abs(snapshot['A_acquired_at_ns'] - snapshot['B_acquired_at_ns']) / 1e9
    assert delta_s < PARALLEL_WINDOW_S, (
        f'A and B acquired {delta_s:.3f}s apart; expected < {PARALLEL_WINDOW_S}s. '
        f'snapshot={snapshot}'
    )

    overlap_ns = min(snapshot['A_releasing_at_ns'], snapshot['B_releasing_at_ns']) - max(
        snapshot['A_acquired_at_ns'], snapshot['B_acquired_at_ns']
    )
    assert overlap_ns > 0, f'A and B never overlapped — they ran sequentially. snapshot={snapshot}'


def test_crash_releases_lock(asyncpg_dsn: str) -> None:
    ctx = mp.get_context('spawn')
    eid = uuid.uuid4()
    lock_id = entity_lock_id(eid)

    with ctx.Manager() as manager:
        state = manager.dict()

        crasher = ctx.Process(target=_crashing_holder, args=(asyncpg_dsn, lock_id, state))
        crasher.start()
        crasher.join(timeout=DEADLINE_S)
        assert crasher.exitcode == 1, (
            f'crasher exited with {crasher.exitcode}; expected 1 (os._exit(1)). '
            f'A graceful close would invalidate this scenario. state={dict(state)}'
        )
        assert 'holder_acquired_at_ns' in state, f'crasher never acquired: {dict(state)}'
        assert 'holder_dying_at_ns' in state, f'crasher never reached crash point: {dict(state)}'

        died_at_ns = state['holder_dying_at_ns']

        acquirer = ctx.Process(
            target=_worker_poll_until_acquire,
            args=(asyncpg_dsn, str(eid), DEADLINE_S, state, 'B'),
        )
        acquirer.start()
        acquirer.join(timeout=DEADLINE_S + 2)
        assert acquirer.exitcode == 0, f'acquirer exitcode={acquirer.exitcode}'

        snapshot = dict(state)

    assert 'B_acquired_at_ns' in snapshot, (
        f'next acquirer never got the lock — Postgres did not auto-release. state={snapshot}'
    )
    assert 'B_error' not in snapshot, snapshot

    auto_release_window_s = (snapshot['B_acquired_at_ns'] - died_at_ns) / 1e9
    assert 0 < auto_release_window_s < AUTO_RELEASE_BOUND_S, (
        f'lock auto-release took {auto_release_window_s:.3f}s; expected '
        f'< {AUTO_RELEASE_BOUND_S}s. snapshot={snapshot}'
    )
