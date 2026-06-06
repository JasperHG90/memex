"""Unit tests for the route_note_to_vault proposal action (mocked api, no DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ProposalActionError,
)
from memex_core.services.proposal_actions.route_note_to_vault import RouteNoteToVaultAction

ACTION = RouteNoteToVaultAction()


def test_validate_rejects_wrong_target_type():
    with pytest.raises(ActionValidationError):
        ACTION.validate(
            {'target_vault_id': str(uuid4())}, target_type='memory_unit', target_id=str(uuid4())
        )


def test_validate_rejects_bad_target_id():
    with pytest.raises(ActionValidationError):
        ACTION.validate(
            {'target_vault_id': str(uuid4())}, target_type='note', target_id='not-a-uuid'
        )


def test_validate_requires_target_vault_id():
    with pytest.raises(ActionValidationError):
        ACTION.validate({}, target_type='note', target_id=str(uuid4()))


def test_validate_rejects_bad_target_vault_id():
    with pytest.raises(ActionValidationError):
        ACTION.validate({'target_vault_id': 'nope'}, target_type='note', target_id=str(uuid4()))


def test_validate_accepts_well_formed():
    ACTION.validate({'target_vault_id': str(uuid4())}, target_type='note', target_id=str(uuid4()))


@pytest.mark.asyncio
async def test_execute_migrates_and_records_positive_and_negative_feedback():
    note_id, target_vault, source_vault, other = uuid4(), uuid4(), uuid4(), uuid4()
    api = MagicMock()
    api.migrate_note = AsyncMock(return_value={'source_vault_id': str(source_vault)})
    api.inbox_router.record_feedback = AsyncMock()

    res = await ACTION.execute(
        api,
        {'target_vault_id': str(target_vault), 'other_vault_ids': [str(other)]},
        target_id=str(note_id),
        vault_id=source_vault,
        actor='tester',
    )

    api.migrate_note.assert_awaited_once_with(note_id, target_vault)
    # Chosen vault is a positive; the unchosen candidate is a negative.
    calls = api.inbox_router.record_feedback.await_args_list
    assert (note_id, target_vault, 1) in [c.args for c in calls]
    assert (note_id, other, 0) in [c.args for c in calls]
    assert res.prior_state['source_vault_id'] == str(source_vault)
    assert res.applied_state['target_vault_id'] == str(target_vault)


@pytest.mark.asyncio
async def test_execute_survives_feedback_failure():
    note_id, target_vault = uuid4(), uuid4()
    api = MagicMock()
    api.migrate_note = AsyncMock(return_value={'source_vault_id': str(uuid4())})
    api.inbox_router.record_feedback = AsyncMock(side_effect=RuntimeError('boom'))
    # Learning failure must not break the migration.
    res = await ACTION.execute(
        api,
        {'target_vault_id': str(target_vault)},
        target_id=str(note_id),
        vault_id=uuid4(),
        actor='tester',
    )
    assert res.applied_state['target_vault_id'] == str(target_vault)


@pytest.mark.asyncio
async def test_reverse_migrates_back_to_source():
    note_id, source_vault = uuid4(), uuid4()
    api = MagicMock()
    api.migrate_note = AsyncMock(return_value={})
    res = await ACTION.reverse(
        api,
        {},
        {'note_id': str(note_id)},
        {'source_vault_id': str(source_vault)},
        target_id=str(note_id),
        vault_id=uuid4(),
        actor='tester',
    )
    api.migrate_note.assert_awaited_once_with(note_id, source_vault)
    assert res.restored_state['vault_id'] == str(source_vault)


@pytest.mark.asyncio
async def test_reverse_without_source_raises():
    api = MagicMock()
    with pytest.raises(ProposalActionError):
        await ACTION.reverse(
            api,
            {},
            {'note_id': str(uuid4())},
            {},
            target_id=str(uuid4()),
            vault_id=uuid4(),
            actor='tester',
        )


@pytest.mark.asyncio
async def test_route_global_noop_stores_none_not_string():
    """A GLOBAL (NULL-vault) finding whose migrate is a no-op must record
    prior_state['source_vault_id'] as None — NOT the literal string 'None'.

    REGRESSION: execute stored ``str(source_vault_id)``. When the finding has
    no vault (vault_id=None) AND migrate_note returns no source (source==target
    no-op), source_vault_id stays None and ``str(None)`` persisted the literal
    'None'. reverse() then read a TRUTHY 'None' string and called UUID('None'),
    which raises ValueError -> 500. The fix stores None, so reverse refuses
    cleanly with ProposalActionError instead of blowing up. This pins both
    halves: prior_state is None (not 'None'), and reverse raises the action
    error (not a bare ValueError)."""
    note_id, target_vault = uuid4(), uuid4()
    api = MagicMock()
    # No-op migrate: source == target, so migrate_note reports no source vault.
    api.migrate_note = AsyncMock(return_value={'source_vault_id': None, 'status': 'noop'})
    api.inbox_router.record_feedback = AsyncMock()

    res = await ACTION.execute(
        api,
        {'target_vault_id': str(target_vault)},
        target_id=str(note_id),
        vault_id=None,  # GLOBAL finding: no source vault to fall back to.
        actor='tester',
    )
    # The load-bearing assertion: None, never the string 'None'.
    assert res.prior_state['source_vault_id'] is None
    assert res.prior_state['source_vault_id'] != 'None'

    # reverse must refuse cleanly (ProposalActionError), NOT raise ValueError
    # from UUID('None') as the pre-fix string would have caused.
    with pytest.raises(ProposalActionError):
        await ACTION.reverse(
            api,
            {},
            res.applied_state,
            res.prior_state,
            target_id=str(note_id),
            vault_id=None,
            actor='tester',
        )
