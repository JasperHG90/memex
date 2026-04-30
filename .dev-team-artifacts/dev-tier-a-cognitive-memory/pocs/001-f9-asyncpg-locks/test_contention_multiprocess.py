"""TC-12-3 + TC-12-4 — multiprocess contention scenarios (AC-F9-6).

TC-12-3: same-entity contention — two spawn-context workers attempt
``pg_try_advisory_lock`` on the SAME lock_id; second blocks until first
releases. Coordination via a ``Manager().dict()`` of timestamps; no
``multiprocessing.Event`` (flakes on Docker spike).

TC-12-4: different-entity parallelism — two workers acquire DIFFERENT
lock_ids; both succeed within a small window (no false serialization).

Spawn context (NOT fork) per TS guidance — fork shares pytest_asyncio's loop
and creates undefined behavior.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
import uuid

import asyncpg
import pytest

from _helpers import entity_lock_id


pytestmark = [pytest.mark.integration]


HOLD_DURATION_S = 1.0
START_DELAY_S = 0.1
DEADLINE_S = 5.0
PARALLEL_WINDOW_S = 0.5


def _worker_acquire_hold_release(
    dsn: str,
    lock_id: int,
    hold_seconds: float,
    state: dict,
    worker_label: str,
) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
            if not got:
                state[f'{worker_label}_acquire_failed'] = True
                return
            state[f'{worker_label}_acquired_at_ns'] = time.monotonic_ns()
            await asyncio.sleep(hold_seconds)
            state[f'{worker_label}_releasing_at_ns'] = time.monotonic_ns()
            await conn.execute('SELECT pg_advisory_unlock($1)', lock_id)
        finally:
            await conn.close()

    asyncio.run(_go())


def _worker_poll_until_acquire(
    dsn: str,
    lock_id: int,
    deadline_seconds: float,
    state: dict,
    worker_label: str,
) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            deadline = time.monotonic() + deadline_seconds
            while time.monotonic() < deadline:
                got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
                if got:
                    state[f'{worker_label}_acquired_at_ns'] = time.monotonic_ns()
                    await conn.execute('SELECT pg_advisory_unlock($1)', lock_id)
                    return
                await asyncio.sleep(0.05)
            state[f'{worker_label}_timed_out'] = True
        finally:
            await conn.close()

    asyncio.run(_go())


def test_same_entity_serializes(asyncpg_dsn: str) -> None:
    """TC-12-3: same lock_id → second worker waits for first."""
    ctx = mp.get_context('spawn')
    lock_id = entity_lock_id(uuid.uuid4())

    with ctx.Manager() as manager:
        state = manager.dict()

        p_holder = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, lock_id, HOLD_DURATION_S, state, 'A'),
        )
        p_holder.start()
        time.sleep(START_DELAY_S)

        p_poller = ctx.Process(
            target=_worker_poll_until_acquire,
            args=(asyncpg_dsn, lock_id, DEADLINE_S, state, 'B'),
        )
        p_poller.start()

        p_holder.join(timeout=DEADLINE_S + 2)
        p_poller.join(timeout=DEADLINE_S + 2)
        assert p_holder.exitcode == 0, f'holder exitcode={p_holder.exitcode}'
        assert p_poller.exitcode == 0, f'poller exitcode={p_poller.exitcode}'

        snapshot = dict(state)

    assert 'A_acquired_at_ns' in snapshot, f'A never acquired: {snapshot}'
    assert 'A_releasing_at_ns' in snapshot, f'A never released: {snapshot}'
    assert 'B_acquired_at_ns' in snapshot, f'B never acquired (likely deadlocked): {snapshot}'
    assert 'B_timed_out' not in snapshot, f'B timed out polling: {snapshot}'

    a_release_ns = snapshot['A_releasing_at_ns']
    b_acquire_ns = snapshot['B_acquired_at_ns']
    assert b_acquire_ns >= a_release_ns, (
        f'B acquired at {b_acquire_ns} BEFORE A released at {a_release_ns} '
        f'— Postgres advisory lock failed to serialize. snapshot={snapshot}'
    )


def test_different_entities_parallel(asyncpg_dsn: str) -> None:
    """TC-12-4: different lock_ids → both workers succeed concurrently."""
    ctx = mp.get_context('spawn')
    lock_id_a = entity_lock_id(uuid.uuid4())
    lock_id_b = entity_lock_id(uuid.uuid4())
    assert lock_id_a != lock_id_b

    with ctx.Manager() as manager:
        state = manager.dict()

        p_a = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, lock_id_a, HOLD_DURATION_S, state, 'A'),
        )
        p_b = ctx.Process(
            target=_worker_acquire_hold_release,
            args=(asyncpg_dsn, lock_id_b, HOLD_DURATION_S, state, 'B'),
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
    assert 'A_acquire_failed' not in snapshot
    assert 'B_acquire_failed' not in snapshot

    delta_s = abs(snapshot['A_acquired_at_ns'] - snapshot['B_acquired_at_ns']) / 1e9
    assert delta_s < PARALLEL_WINDOW_S, (
        f'A and B acquired {delta_s:.3f}s apart; expected < {PARALLEL_WINDOW_S}s '
        f'for true parallelism. snapshot={snapshot}'
    )

    a_held_for_s = (snapshot['A_releasing_at_ns'] - snapshot['A_acquired_at_ns']) / 1e9
    b_held_for_s = (snapshot['B_releasing_at_ns'] - snapshot['B_acquired_at_ns']) / 1e9
    a_b_overlap_ns = min(snapshot['A_releasing_at_ns'], snapshot['B_releasing_at_ns']) - max(
        snapshot['A_acquired_at_ns'], snapshot['B_acquired_at_ns']
    )
    assert a_b_overlap_ns > 0, (
        f'A and B never overlapped (a_held={a_held_for_s:.3f}s, '
        f'b_held={b_held_for_s:.3f}s); they ran sequentially.'
    )
