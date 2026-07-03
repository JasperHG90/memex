"""F6 — scheduler integration tests (TC-20-4).

Two cases:

  * ``test_scheduler_uses_only_existing_leader_lock_id`` — static AST-style audit
    of ``packages/core/src/memex_core/scheduler.py``: exactly one
    ``MEMEX_LEADER_LOCK_ID`` literal is defined, and every advisory-lock SQL
    call references that constant. AC-F6-3 (no new lock).
  * ``test_periodic_lint_task_emits_findings_under_leader_lock`` — drives the
    scheduler-callable ``periodic_lint_task(api)`` against the integration
    ``api`` fixture. Seeds one fires-case for one rule, calls
    ``periodic_lint_task`` directly (mimicking what the AioClock job runs
    once the Postgres advisory lock is held), asserts the finding lands in
    ``maintenance_proposals``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core import scheduler as scheduler_mod
from memex_core.memory.sql_models import MemoryUnit, Note, Vault


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# AC-F6-3 — no new advisory lock
# ---------------------------------------------------------------------------


def test_scheduler_uses_only_existing_leader_lock_id() -> None:
    """Static audit: scheduler.py defines exactly ONE lock-ID constant
    (``MEMEX_LEADER_LOCK_ID``) and every advisory-lock call references it.

    Failure modes this catches:
      * a future commit hardcoding a magic number on a new lock
      * a feature workstream introducing its own lock constant
      * a typo'd reference to a different lock
    """
    src_path = Path(scheduler_mod.__file__)
    src = src_path.read_text()

    # Exactly one lock-ID definition.
    lock_defs = re.findall(r'^([A-Z_]+_LOCK_ID)\s*=\s*\d+', src, flags=re.MULTILINE)
    assert lock_defs == ['MEMEX_LEADER_LOCK_ID'], (
        f'Expected exactly one *_LOCK_ID definition (MEMEX_LEADER_LOCK_ID); got {lock_defs}'
    )

    # Match every line containing pg_*advisory_* and capture the constant
    # name following it; assert it's only ever MEMEX_LEADER_LOCK_ID.
    advisory_refs = re.findall(
        r'pg_(?:try_)?advisory_(?:lock|unlock).*?([A-Z_][A-Z0-9_]+_LOCK_ID)',
        src,
    )
    assert advisory_refs, 'Expected at least one advisory-lock SQL call'
    assert set(advisory_refs) == {'MEMEX_LEADER_LOCK_ID'}, (
        f'Advisory-lock calls reference unexpected constants: {set(advisory_refs)}'
    )

    # Hardcoded magic numbers in advisory-lock SQL would be caught here.
    bad = re.findall(r'pg_(?:try_)?advisory_(?:lock|unlock)\([\'"]?\d+', src)
    assert not bad, f'Hardcoded lock IDs found: {bad}'


# ---------------------------------------------------------------------------
# AC-F6-3 — periodic_lint_task fires under leader-lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_lint_task_emits_findings_under_leader_lock(
    session: AsyncSession, api
) -> None:
    """Drive ``periodic_lint_task(api)`` end-to-end.

    The function is what AioClock invokes once the Postgres advisory lock is
    held; calling it directly proves the scheduler hook produces findings
    against the live DB. Tests AC-F6-3 (the lint task is reachable from the
    scheduler) and AC-F6-2 (run_rules writes via the scheduler path).
    """
    vault = Vault(name=f'F6-sched-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='seed',
    )
    session.add(note)
    await session.commit()

    # Sensitive-unreviewed-unit fires immediately (no time-travel needed).
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='sensitive scheduler-trigger unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        risk_class='sensitive',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(unit)
    await session.commit()

    # Drive the scheduler-callable directly. This mimics what AioClock invokes
    # under the held MEMEX_LEADER_LOCK_ID; the lock is the leader-election
    # gate, not a per-call wrapper, so no per-test lock dance is needed.
    await scheduler_mod.periodic_lint_task(api)

    rows = (
        (
            await session.execute(
                text(
                    'SELECT target_id, status, source FROM maintenance_proposals '
                    "WHERE rule_name = 'sensitive_unreviewed_unit' AND vault_id = :v"
                ),
                {'v': str(vault.id)},
            )
        )
        .mappings()
        .all()
    )

    assert len(rows) == 1, (
        f'periodic_lint_task should have emitted 1 sensitive-unit finding; got {len(rows)}'
    )
    row = dict(rows[0])
    assert row['target_id'] == str(unit.id)
    assert row['status'] == 'pending'
    assert row['source'] == 'rule'
