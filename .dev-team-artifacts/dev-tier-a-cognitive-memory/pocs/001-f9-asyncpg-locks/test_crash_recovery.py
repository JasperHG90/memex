"""TC-12-5 — connection death auto-releases lock (RFC-005:264 safety property).

QA caveat: ``conn.terminate()`` is graceful close, NOT a crash. We MUST exercise
a real backend termination — the worker calls ``os._exit(1)`` while holding the
lock, with no atexit / no pytest cleanup hooks, no asyncpg ``close()``. Only
Postgres's session-end cleanup releases the lock.

Bound: Worker B acquires within 2s of Worker A's death (refinement #2).
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time
import uuid

import asyncpg
import pytest

from _helpers import entity_lock_id


pytestmark = [pytest.mark.integration]


CRASHER_HOLD_S = 0.3
B_DEADLINE_S = 4.0
AUTO_RELEASE_BOUND_S = 2.0


def _crashing_holder(dsn: str, lock_id: int, state: dict) -> None:
    """Acquire lock then call os._exit(1) — simulates a real backend crash.

    os._exit() simulates a process crash; Postgres releases session-scoped
    locks on backend termination regardless of cleanup state. NO finally,
    NO atexit, NO conn.close() — those would be a graceful close that
    masquerades as a crash and would NOT exercise the safety property
    we're trying to validate.
    """

    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
        if not got:
            state['holder_acquire_failed'] = True
            os._exit(2)
        state['holder_acquired_at_ns'] = time.monotonic_ns()
        state['holder_pid'] = os.getpid()
        await asyncio.sleep(CRASHER_HOLD_S)
        state['holder_dying_at_ns'] = time.monotonic_ns()
        os._exit(1)

    asyncio.run(_go())


def _waiting_acquirer(dsn: str, lock_id: int, deadline_s: float, state: dict) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            deadline = time.monotonic() + deadline_s
            while time.monotonic() < deadline:
                got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
                if got:
                    state['acquirer_got_at_ns'] = time.monotonic_ns()
                    await conn.execute('SELECT pg_advisory_unlock($1)', lock_id)
                    return
                await asyncio.sleep(0.05)
            state['acquirer_timed_out'] = True
        finally:
            await conn.close()

    asyncio.run(_go())


def test_lock_released_on_connection_death(asyncpg_dsn: str) -> None:
    ctx = mp.get_context('spawn')
    lock_id = entity_lock_id(uuid.uuid4())

    with ctx.Manager() as manager:
        state = manager.dict()

        crasher = ctx.Process(target=_crashing_holder, args=(asyncpg_dsn, lock_id, state))
        crasher.start()
        crasher.join(timeout=B_DEADLINE_S)
        assert crasher.exitcode == 1, (
            f'crasher exited with {crasher.exitcode}; expected 1 (os._exit(1)). '
            f'A graceful close would invalidate this scenario. state={dict(state)}'
        )
        assert 'holder_acquired_at_ns' in state, f'crasher never acquired: {dict(state)}'
        assert 'holder_dying_at_ns' in state, f'crasher never reached crash point: {dict(state)}'

        died_at_ns = state['holder_dying_at_ns']

        acquirer = ctx.Process(
            target=_waiting_acquirer, args=(asyncpg_dsn, lock_id, B_DEADLINE_S, state)
        )
        acquirer.start()
        acquirer.join(timeout=B_DEADLINE_S + 2)
        assert acquirer.exitcode == 0, f'acquirer exitcode={acquirer.exitcode}'

        snapshot = dict(state)

    assert 'acquirer_got_at_ns' in snapshot, (
        f'acquirer never got the lock — Postgres did not auto-release. state={snapshot}'
    )
    assert 'acquirer_timed_out' not in snapshot

    auto_release_window_s = (snapshot['acquirer_got_at_ns'] - died_at_ns) / 1e9
    assert 0 < auto_release_window_s < AUTO_RELEASE_BOUND_S, (
        f'lock auto-release took {auto_release_window_s:.3f}s; expected '
        f'< {AUTO_RELEASE_BOUND_S}s. Unbounded delay could mask a Postgres bug. '
        f'state={snapshot}'
    )
