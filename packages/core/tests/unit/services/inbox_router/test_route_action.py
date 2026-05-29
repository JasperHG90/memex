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
async def test_execute_migrates_and_records_feedback():
    note_id, target_vault, source_vault = uuid4(), uuid4(), uuid4()
    api = MagicMock()
    api.migrate_note = AsyncMock(return_value={'source_vault_id': str(source_vault)})
    api.inbox_router.record_feedback = AsyncMock()

    res = await ACTION.execute(
        api,
        {'target_vault_id': str(target_vault)},
        target_id=str(note_id),
        vault_id=source_vault,
        actor='tester',
    )

    api.migrate_note.assert_awaited_once_with(note_id, target_vault)
    # Manual confirmation feeds the model as a positive for the chosen vault.
    api.inbox_router.record_feedback.assert_awaited_once_with(note_id, target_vault, 1)
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
