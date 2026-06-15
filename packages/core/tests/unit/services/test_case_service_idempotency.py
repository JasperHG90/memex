"""Unit tests for content-idempotent case re-submission.

Ingest is content-addressed: re-submitting byte-identical case content skips
and returns ``{'status': 'skipped'}`` with NO ``note_id``. The case service
must treat that as "already filed" and return a clean ``skipped`` result —
NOT KeyError into a 500, and NOT re-stamp/re-assign (which would duplicate the
provenance edge). Regression guard for the user-reported 500-on-resubmit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from memex_common.procedural_schemas import CaseSubmit
from memex_core.services.case_service import CaseService


def _payload() -> CaseSubmit:
    return CaseSubmit(
        title='Fixed staging deploy timeout',
        trigger='staging deploy kept timing out',
        situation='health check on the wrong port',
        actions=['found the health-check on the wrong port', 'set it to 8080'],
        outcome='success',
        lesson='check the health-check port when a deploy hangs',
        submitted_by='memex-cli',
    )


@pytest.mark.asyncio
async def test_resubmit_skips_cleanly_without_restamping():
    """An idempotency-skip ingest yields a ``skipped`` result with the
    deterministic note id, and never touches stamp/assign."""
    vault_id = uuid4()
    api = AsyncMock()
    api.ingest = AsyncMock(return_value={'status': 'skipped', 'reason': 'idempotency_check'})

    svc = CaseService(api)
    svc._resolve_case_vault = AsyncMock(return_value=vault_id)  # type: ignore[method-assign]
    svc._stamp_case_note = AsyncMock()  # type: ignore[method-assign]
    svc._assign = AsyncMock()  # type: ignore[method-assign]

    payload = _payload()
    result = await svc.submit(payload)

    assert result.assignment.mode == 'skipped'
    assert result.vault_id == vault_id
    # Note id is content-addressed — matches what a first submit would have filed.
    assert result.note_id == UUID(svc._build_note_input(payload).idempotency_key)

    # Crucially: no re-stamp, no re-assign (would duplicate the provenance edge).
    svc._stamp_case_note.assert_not_called()
    svc._assign.assert_not_called()
