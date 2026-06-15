"""Unit tests: a case that spawns a NEW procedure files the activation lint
item at submission, so the draft anchor is reviewable in ``memex lint review``
immediately (draft → published via ``activate_procedural_entry``) instead of
only after the Phase-3 derivation worker distills it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from memex_common.procedural_schemas import CaseSubmit
from memex_core.services.case_service import CaseService
from memex_core.services.procedural_derivation_service import DISTILLATION_RULE_NAME


def _payload() -> CaseSubmit:
    return CaseSubmit(
        title='Rotate the signing key without downtime',
        trigger='signing key neared expiry',
        outcome='success',
        lesson='stage the new key before flipping',
        submitted_by='memex-cli',
    )


@pytest.mark.asyncio
async def test_file_activation_proposal_targets_entry_with_activate_action():
    """The filed proposal targets the draft ENTRY, pre-selects
    ``activate_procedural_entry``, and reuses the derivation rule name so a
    later distillation re-file dedups onto this pending row (no double-nag)."""
    entry_id, vault_id, finding_id = uuid4(), uuid4(), uuid4()

    svc = CaseService(AsyncMock())
    with patch(
        'memex_core.services.lint_external.insert_external_proposal',
        new=AsyncMock(return_value=('created', finding_id)),
    ) as ins:
        result = await svc._file_activation_proposal(entry_id, vault_id, _payload())

    assert result == finding_id
    assert ins.await_args is not None
    req = ins.await_args.args[1]  # insert_external_proposal(api, req, ...)
    assert req.target_type == 'procedural_entry'
    assert req.target_id == str(entry_id)
    assert req.lint_type == 'governance'
    assert req.proposed_action.action_name == 'activate_procedural_entry'
    assert req.proposed_action.params == {}
    assert req.rule_name == DISTILLATION_RULE_NAME


@pytest.mark.asyncio
async def test_activation_proposal_filing_failure_is_swallowed():
    """A lint-surface failure must not sink the submission — the draft anchor
    is already created + attached, so a missing breadcrumb only delays
    activation. Returns None, never raises."""
    svc = CaseService(AsyncMock())
    with patch(
        'memex_core.services.lint_external.insert_external_proposal',
        new=AsyncMock(side_effect=RuntimeError('lint surface down')),
    ):
        result = await svc._file_activation_proposal(uuid4(), uuid4(), _payload())

    assert result is None


@pytest.mark.asyncio
async def test_submit_job_returns_note_id_dict():
    """submit_job adapts submit() into the job-result dict create_single_job
    records — note_id (a str) drives the tracked BatchJob's note_ids."""
    from memex_common.procedural_schemas import CaseAssignment, CaseSubmitResult

    note_id, vault_id = uuid4(), uuid4()
    svc = CaseService(AsyncMock())
    svc.submit = AsyncMock(  # type: ignore[method-assign]
        return_value=CaseSubmitResult(
            note_id=note_id,
            vault_id=vault_id,
            assignment=CaseAssignment(mode='explicit', entry_id=uuid4()),
        )
    )

    out = await svc.submit_job(request=_payload(), vault_id=None)

    assert out['note_id'] == str(note_id)
    assert out['vault_id'] == str(vault_id)
    assert out['assignment_mode'] == 'explicit'
