"""F32 scheduler — leader-lock task registration (Test 7).

Per RFC-009 + AC-X-8 (single-leader invariant): the F32 weekly manifold
refresh registers under the existing ``MEMEX_LEADER_LOCK_ID`` advisory lock,
not a new one. This test verifies the task registration is reachable on the
scheduler module, that ``periodic_diagnostics_refresh_task`` exists, and that
``run_diagnostics_refresh`` appears as a registered ``@clock.task`` line under
the existing leader-lock loop.

Note: the lock-site invariant ("only MEMEX_LEADER_LOCK_ID is used at runtime")
is enforced by the seed PR's ``test_runtime_advisory_lock_invariant_pre_f9``;
this test is the F32 task-registration cousin of that invariant.
"""

from __future__ import annotations

import inspect

import pytest

from memex_core import scheduler


@pytest.mark.integration
def test_diagnostics_refresh_registered_under_leader_lock():
    # 1. The helper exists and is async.
    assert hasattr(scheduler, 'periodic_diagnostics_refresh_task')
    assert inspect.iscoroutinefunction(scheduler.periodic_diagnostics_refresh_task)

    # 2. The helper uses background_session (cooperates with the runtime
    #    background-session contract that other periodic tasks share).
    src_helper = inspect.getsource(scheduler.periodic_diagnostics_refresh_task)
    assert 'background_session' in src_helper
    assert "'bg-sched-diagnostics-refresh'" in src_helper

    # 3. The leader-lock loop registers a task named ``run_diagnostics_refresh``
    #    via ``@clock.task`` with a weekly Every() trigger, and that task body
    #    awaits the helper. We assert the registration shape on the source of
    #    ``run_scheduler_with_leader_election`` — the lock-site itself uses
    #    MEMEX_LEADER_LOCK_ID (verified by the seed PR's pre-F9 invariant test).
    src_loop = inspect.getsource(scheduler.run_scheduler_with_leader_election)
    assert 'run_diagnostics_refresh' in src_loop
    assert 'periodic_diagnostics_refresh_task(api)' in src_loop
    assert 'Every(seconds=7 * 86400)' in src_loop

    # 4. AC-X-8 invariant cousin: the F32 task does NOT introduce its own
    #    advisory-lock id. Only ``MEMEX_LEADER_LOCK_ID`` is referenced inside
    #    the loop function — no other ``advisory_lock`` calls.
    assert src_loop.count('MEMEX_LEADER_LOCK_ID') >= 1
    # No new pg_try_advisory_lock or pg_advisory_unlock calls beyond the
    # existing leader loop's pair — simple word-count assertion.
    assert src_loop.count('pg_try_advisory_lock') == 1
    assert src_loop.count('pg_advisory_unlock') == 1
