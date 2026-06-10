"""Integration tests for the V7 derivation-queue worker contract.

The derivation queue is the async path by which cases graduate into
procedures / strategies. The queue's claim path uses
``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple workers don't
double-claim the same row. The complete / fail paths transition
status and stamp attempt counts.

Covers the load-bearing worker behaviours the unit tests (which
mock the metastore) cannot:

* enqueue + claim round-trip (a single worker claims the just-
  enqueued row, status flips pending → in_progress, attempt count
  increments)
* mark_derivation_completed sets status=completed and stamps
  result_entry_id + completed_at
* mark_derivation_failed below max_attempts re-queues (status back
  to pending) — workers don't lose work to a transient error
* mark_derivation_failed at max_attempts flips status=failed
* strategy derivations require target_verb and NO target_context
  (strategy anchor ≡ (scope, verb); §18.1). A malformed target would
  otherwise surface as a 500 at the worker.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from memex_core.services.procedural_repository import (
    ProceduralRepository,
)

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_vault(session, name_prefix: str) -> uuid.UUID:
    vault_id = uuid.uuid4()
    await session.execute(
        text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
        {'id': str(vault_id), 'name': f'{name_prefix}_{vault_id.hex[:8]}'},
    )
    await session.commit()
    return vault_id


def _repo(metastore) -> ProceduralRepository:
    return ProceduralRepository(metastore=metastore)


# ---------------------------------------------------------------------------
# enqueue + claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_then_claim_transitions_status_and_increments_attempts(metastore):
    """A pending derivation row is claimable by exactly one worker.

    The claim path uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so
    parallel workers don't double-claim. After claim, the row's
    status is in_progress and attempt_count is 1."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_claim')

    repo = _repo(metastore)
    enqueued = await repo.enqueue_derivation(
        vault_id=vault_id,
        source_entry_ids=[uuid.uuid4()],
        target_kind='procedure',
        target_scope='global',
        target_verb='codify',
        target_context='incident_response',
    )
    assert enqueued.status == 'pending'
    assert enqueued.attempt_count == 0

    claimed = await repo.claim_derivation_tasks(limit=1, vault_id=vault_id)
    assert len(claimed) == 1
    assert claimed[0].queue_id == enqueued.id
    assert claimed[0].target_kind == 'procedure'
    assert claimed[0].target_verb == 'codify'
    assert claimed[0].target_context == 'incident_response'

    # The row's status flipped in the DB.
    async with metastore.session() as session:
        row = (
            await session.execute(
                text(
                    'SELECT status, attempt_count, claimed_at IS NOT NULL '
                    'FROM procedural_derivation_queue WHERE id = :id'
                ),
                {'id': str(enqueued.id)},
            )
        ).first()
    assert row is not None
    status, attempt_count, claimed_at_set = row
    assert status == 'in_progress'
    assert attempt_count == 1
    assert claimed_at_set, 'claimed_at must be stamped on claim'


# ---------------------------------------------------------------------------
# mark_derivation_completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_completed_records_result_entry_id_and_completed_at(metastore):
    """A successful worker completion sets status=completed, stamps
    result_entry_id (the entry the worker produced) and completed_at."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_complete')

    repo = _repo(metastore)
    enqueued = await repo.enqueue_derivation(
        vault_id=vault_id,
        source_entry_ids=[uuid.uuid4()],
        target_kind='procedure',
        target_scope='global',
        target_verb='codify',
        target_context='rollout',
    )
    await repo.claim_derivation_tasks(limit=1, vault_id=vault_id)

    result_entry_id = uuid.uuid4()
    completed = await repo.mark_derivation_completed(enqueued.id, result_entry_id)

    assert completed.status == 'completed'
    assert completed.result_entry_id == result_entry_id
    assert completed.completed_at is not None


# ---------------------------------------------------------------------------
# mark_derivation_failed — re-queue under the threshold, fail at it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_failed_below_max_attempts_requeues(metastore):
    """A transient worker error keeps the row claimable: status
    flips back to pending (so another worker can retry) and
    last_error is stamped for debugging.

    A regression that flipped failure to immediately-failed would
    silently drop work on any transient LLM API blip."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_retry')

    repo = _repo(metastore)
    enqueued = await repo.enqueue_derivation(
        vault_id=vault_id,
        source_entry_ids=[uuid.uuid4()],
        target_kind='procedure',
        target_scope='global',
        target_verb='codify',
        target_context='incident',
    )
    await repo.claim_derivation_tasks(limit=1, vault_id=vault_id)

    retried = await repo.mark_derivation_failed(
        enqueued.id, last_error='LLM 502 from upstream', max_attempts=3
    )

    assert retried.status == 'pending', 'attempt 1 of 3 must re-queue, not fail'
    assert retried.last_error == 'LLM 502 from upstream'
    assert retried.attempt_count == 1


@pytest.mark.asyncio
async def test_mark_failed_at_max_attempts_flips_to_failed(metastore):
    """Once attempt_count == max_attempts, a failure flips status to
    'failed' (not 'pending'). A regression that re-queued forever
    would let a poison-pill task starve workers indefinitely."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_exhausted')

    repo = _repo(metastore)
    enqueued = await repo.enqueue_derivation(
        vault_id=vault_id,
        source_entry_ids=[uuid.uuid4()],
        target_kind='procedure',
        target_scope='global',
        target_verb='codify',
        target_context='incident',
    )
    # First claim + fail (attempt 1 of 2).
    await repo.claim_derivation_tasks(limit=1, vault_id=vault_id)
    await repo.mark_derivation_failed(enqueued.id, last_error='first failure', max_attempts=2)
    # Second claim + fail (attempt 2 of 2) — should land on 'failed'.
    await repo.claim_derivation_tasks(limit=1, vault_id=vault_id)
    final = await repo.mark_derivation_failed(
        enqueued.id, last_error='final failure', max_attempts=2
    )

    assert final.status == 'failed'
    assert final.last_error == 'final failure'
    assert final.attempt_count == 2


# ---------------------------------------------------------------------------
# Validation — strategy derivations require verb and NO context (§18.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_strategy_without_verb_raises_value_error(metastore):
    """A strategy anchors on (scope, verb) — verb is required (context
    is forbidden; §18.1). Enqueueing a strategy derivation without a
    verb is a programming error that must fail loud at enqueue time
    (not 500 in the worker)."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_strat')

    repo = _repo(metastore)
    with pytest.raises(ValueError, match='strategy derivations require'):
        await repo.enqueue_derivation(
            vault_id=vault_id,
            source_entry_ids=[uuid.uuid4()],
            target_kind='strategy',
            target_scope='global',
            target_verb=None,
            target_context='something',
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_invalid_target_kind(metastore):
    """The target_kind literal is closed (procedure|strategy). A typo
    like 'stratigy' or 'procedure_' must surface at enqueue time,
    not deep in the worker where the resulting
    ``IdentityAnchorConflict`` would be confusing."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'v7_q_kind')

    repo = _repo(metastore)
    with pytest.raises(ValueError, match='target_kind must be procedure|strategy'):
        await repo.enqueue_derivation(
            vault_id=vault_id,
            source_entry_ids=[uuid.uuid4()],
            target_kind='stratigy',  # typo
            target_scope='global',
        )
