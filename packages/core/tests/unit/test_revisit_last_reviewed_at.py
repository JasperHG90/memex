"""F20 regression: ``revisit_last_reviewed_at`` is correctly threaded through review().

Pins the FSRS-5 last_review fix from PR #101 c1 HIGH-1:

- ``populate_initial_schedules`` writes a ``revisit_last_reviewed_at`` value
  alongside the initial schedule so subsequent ``review()`` calls have a
  proper prior ``last_review`` to compute elapsed days from.
- ``review()`` reads ``revisit_last_reviewed_at`` (NOT ``revisit_due_at``)
  into the FSRS-5 ``UnitState.last_review``, falling back to
  ``revisit_due_at`` for legacy rows that predate migration 030.
- ``review()`` writes the new ``review_at`` timestamp into
  ``revisit_last_reviewed_at`` after the FSRS update so the next review
  computes elapsed days from the correct anchor.

Without this, ``elapsed_days = (now - revisit_due_at)`` collapsed to ~0 on
on-time reviews, and FSRS produced absurdly small intervals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.memory.revisit import Quality, UnitState
from memex_core.services.revisitation import RevisitationService


def _make_session(unit: SimpleNamespace) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    session.get = AsyncMock(return_value=unit)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    class _SessionCtx:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    metastore = MagicMock()
    metastore.session = MagicMock(return_value=_SessionCtx())
    return metastore, session


@pytest.mark.asyncio
async def test_review_uses_last_reviewed_at_for_prior_state() -> None:
    """When ``revisit_last_reviewed_at`` is set, it is used as the FSRS
    ``last_review`` anchor — NOT ``revisit_due_at`` (which is next-due).
    """
    vault = uuid4()
    unit_id = uuid4()
    last_reviewed = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    next_due = last_reviewed + timedelta(days=3)
    review_at = last_reviewed + timedelta(days=3)

    unit = SimpleNamespace(
        id=unit_id,
        vault_id=vault,
        revisit_stability=3.0,
        revisit_difficulty=5.0,
        revisit_due_at=next_due,
        revisit_last_reviewed_at=last_reviewed,
        revisit_review_count=0,
        is_deprioritized=False,
    )

    metastore, _session = _make_session(unit)
    service = RevisitationService(metastore, MagicMock(), MagicMock())
    service._audit_service = MagicMock()  # type: ignore[attr-defined]

    captured_state: list[UnitState | None] = []

    async def _fake_record_outcome(**_kwargs: object) -> None:
        return None

    with (
        patch('memex_core.services.revisitation.schedule') as mock_schedule,
        patch(
            'memex_core.services.revisitation.OutcomeService',
            return_value=SimpleNamespace(record_outcome=_fake_record_outcome),
        ),
        patch('memex_core.services.revisitation.audit_event') as _mock_audit,
    ):
        mock_schedule.return_value = (
            review_at + timedelta(days=10),
            10,
            5.0,
            4.5,
        )

        def _capture(**kwargs: object) -> tuple[object, ...]:
            state_arg = kwargs.get('state')
            captured_state.append(state_arg if isinstance(state_arg, UnitState) else None)
            return (review_at + timedelta(days=10), 10, 5.0, 4.5)

        mock_schedule.side_effect = _capture

        await service.review(
            unit_id,
            Quality.GOOD,
            vault_id=vault,
            now=review_at,
        )

    assert len(captured_state) == 1
    state = captured_state[0]
    assert state is not None
    assert state.last_review == last_reviewed, (
        f'FSRS last_review should be revisit_last_reviewed_at ({last_reviewed}), '
        f'got {state.last_review}'
    )
    assert unit.revisit_last_reviewed_at == review_at, (
        'review() must persist the new review timestamp'
    )


@pytest.mark.asyncio
async def test_review_falls_back_to_due_at_for_legacy_rows() -> None:
    """Legacy rows from migration 026 have ``revisit_last_reviewed_at = NULL``
    but populated stability/difficulty — fall back to ``revisit_due_at``.
    """
    vault = uuid4()
    unit_id = uuid4()
    legacy_due = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    review_at = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    unit = SimpleNamespace(
        id=unit_id,
        vault_id=vault,
        revisit_stability=2.5,
        revisit_difficulty=6.0,
        revisit_due_at=legacy_due,
        revisit_last_reviewed_at=None,
        revisit_review_count=0,
        is_deprioritized=False,
    )

    metastore, _session = _make_session(unit)
    service = RevisitationService(metastore, MagicMock(), MagicMock())
    service._audit_service = MagicMock()  # type: ignore[attr-defined]

    captured_state: list[UnitState | None] = []

    async def _fake_record_outcome(**_kwargs: object) -> None:
        return None

    with (
        patch('memex_core.services.revisitation.schedule') as mock_schedule,
        patch(
            'memex_core.services.revisitation.OutcomeService',
            return_value=SimpleNamespace(record_outcome=_fake_record_outcome),
        ),
        patch('memex_core.services.revisitation.audit_event'),
    ):

        def _capture(**kwargs: object) -> tuple[object, ...]:
            state_arg = kwargs.get('state')
            captured_state.append(state_arg if isinstance(state_arg, UnitState) else None)
            return (review_at + timedelta(days=14), 14, 4.0, 5.5)

        mock_schedule.side_effect = _capture

        await service.review(
            unit_id,
            Quality.GOOD,
            vault_id=vault,
            now=review_at,
        )

    state = captured_state[0]
    assert state is not None
    assert state.last_review == legacy_due, (
        'legacy rows (revisit_last_reviewed_at=NULL) should fall back to revisit_due_at'
    )
    assert unit.revisit_last_reviewed_at == review_at, (
        'review() must populate revisit_last_reviewed_at on legacy rows too'
    )
