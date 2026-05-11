"""Unit tests for contradiction_resolution action dispatch.

Drives ``apply_winner_proposal`` against a mock AsyncSession capturing
every SQL statement so each ``action`` literal's dispatch logic is
exercised without booting Postgres. Covers:

- mark_loser_stale flips unit status.
- supersede_loser_note rewrites note.superseded_by.
- supersede_loser_note falls back to mark_loser_stale when both units
  share the same parent note (records evidence.resolution.fallback_reason).
- refine_not_contradict rewrites the link's link_type.
- inconclusive is a no-op write.
- unknown action raises ContradictionResolutionError.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memex_core.services.contradiction_resolution import (
    ContradictionResolutionError,
    apply_winner_proposal,
)


def _row(**kwargs):
    return SimpleNamespace(**kwargs)


def _make_session(rows_by_query: list):
    """Build an AsyncSession mock that yields rows in call order.

    Each entry in ``rows_by_query`` is the ``.first()`` result of the
    matching ``session.execute(...)`` call (None when no row).
    """
    captured: list[tuple] = []

    class _Result:
        def __init__(self, row):
            self._row = row
            self.rowcount = 1

        def first(self):
            return self._row

    async def execute(sql, params=None):
        try:
            sql_text = str(getattr(sql, 'text', sql))
        except Exception:
            sql_text = ''
        captured.append((sql_text, params))
        idx = len(captured) - 1
        if idx < len(rows_by_query):
            return _Result(rows_by_query[idx])
        return _Result(None)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session._captured = captured

    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return session, cm


def _api_with_session(cm):
    api = MagicMock()
    api.metastore.session = MagicMock(return_value=cm)
    return api


def _proposal(action, *, loser_unit_id=None, winner_unit_id=None, link_id=None):
    return _row(
        id=str(uuid4()),
        vault_id='11111111-1111-1111-1111-111111111111',
        rule_name='propose_contradiction_winner',
        target_type='memory_unit',
        target_id=loser_unit_id or str(uuid4()),
        evidence=json.dumps(
            {
                'action': action,
                'winner_unit_id': winner_unit_id,
                'loser_unit_id': loser_unit_id,
                'link_id': link_id,
                'winner_id': 'unit_a',
                'loser_id': 'unit_b',
                'confidence': 0.8,
                'rationale': 'mock',
            }
        ),
        status='pending',
    )


@pytest.mark.asyncio
async def test_mark_loser_stale_dispatches_unit_status_update():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    proposal = _proposal('mark_loser_stale', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    winner_row = _row(id=winner_id, status='active', note_id=str(uuid4()))
    session, cm = _make_session([proposal, loser_row, winner_row])
    api = _api_with_session(cm)

    result = await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    assert result['status'] == 'resolved'
    assert result['effective_action'] == 'mark_loser_stale'
    assert result['fallback_reason'] is None
    statuses = [
        params
        for sql, params in session._captured
        if isinstance(params, dict) and params.get('status') == 'stale'
    ]
    assert statuses, 'expected a status=stale UPDATE'


@pytest.mark.asyncio
async def test_supersede_loser_note_falls_back_when_shared_parent():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    shared_note = str(uuid4())
    proposal = _proposal('supersede_loser_note', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=shared_note)
    winner_row = _row(id=winner_id, status='active', note_id=shared_note)
    session, cm = _make_session([proposal, loser_row, winner_row])
    api = _api_with_session(cm)

    result = await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    assert result['effective_action'] == 'mark_loser_stale'
    assert result['fallback_reason'] == 'shared_parent_note'


@pytest.mark.asyncio
async def test_supersede_loser_note_updates_note_superseded_by():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    loser_note = str(uuid4())
    winner_note = str(uuid4())
    proposal = _proposal('supersede_loser_note', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=loser_note)
    winner_row = _row(id=winner_id, status='active', note_id=winner_note)
    note_row = _row(id=loser_note, superseded_by=None)
    session, cm = _make_session([proposal, loser_row, winner_row, note_row])
    api = _api_with_session(cm)

    result = await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    assert result['effective_action'] == 'supersede_loser_note'
    superseded = [
        params
        for sql, params in session._captured
        if isinstance(params, dict) and params.get('superseded_by') == winner_note
    ]
    assert superseded, 'expected a superseded_by UPDATE'


@pytest.mark.asyncio
async def test_refine_not_contradict_rewrites_link_type():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    link_id = str(uuid4())
    proposal = _proposal(
        'refine_not_contradict',
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_id=link_id,
    )
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    winner_row = _row(id=winner_id, status='active', note_id=str(uuid4()))
    link_row = _row(
        id=link_id,
        link_type='contradicts',
        from_unit_id=winner_id,
        to_unit_id=loser_id,
    )
    session, cm = _make_session([proposal, loser_row, winner_row, link_row])
    api = _api_with_session(cm)

    result = await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    assert result['effective_action'] == 'refine_not_contradict'
    refines = [
        params
        for sql, params in session._captured
        if isinstance(params, dict) and params.get('link_type') == 'refines'
    ]
    assert refines, 'expected a link_type=refines UPDATE'


@pytest.mark.asyncio
async def test_inconclusive_is_a_noop_write():
    loser_id = str(uuid4())
    proposal = _proposal('inconclusive', loser_unit_id=loser_id, winner_unit_id=None)
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    session, cm = _make_session([proposal, loser_row])
    api = _api_with_session(cm)

    result = await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    assert result['effective_action'] == 'inconclusive'
    # No status flip, no note update, no link update.
    mutating = [
        params
        for sql, params in session._captured
        if isinstance(params, dict)
        and (
            params.get('status') == 'stale'
            or params.get('superseded_by') is not None
            or params.get('link_type') == 'refines'
        )
    ]
    assert not mutating, 'inconclusive must not mutate target rows'


@pytest.mark.asyncio
async def test_unknown_action_raises():
    loser_id = str(uuid4())
    proposal = _proposal('eat_the_database', loser_unit_id=loser_id, winner_unit_id=None)
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    session, cm = _make_session([proposal, loser_row])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_missing_finding_raises():
    session, cm = _make_session([None])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_apply_requires_actor():
    api = MagicMock()
    with pytest.raises(ContradictionResolutionError, match='actor required'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor=None,
        )


@pytest.mark.asyncio
async def test_apply_requires_actor_when_empty_string():
    api = MagicMock()
    with pytest.raises(ContradictionResolutionError, match='actor required'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='',
        )


@pytest.mark.asyncio
async def test_apply_requires_vault_id():
    api = MagicMock()
    with pytest.raises(ContradictionResolutionError, match='vault_id required'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=None,
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_apply_rejects_vault_mismatch():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    proposal = _proposal('mark_loser_stale', loser_unit_id=loser_id, winner_unit_id=winner_id)
    session, cm = _make_session([proposal])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='vault_id mismatch'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('22222222-2222-2222-2222-222222222222'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_requires_actor():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    api = MagicMock()
    with pytest.raises(ContradictionResolutionError, match='actor required'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor=None,
        )


@pytest.mark.asyncio
async def test_reverse_requires_vault_id():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    api = MagicMock()
    with pytest.raises(ContradictionResolutionError, match='vault_id required'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=None,
            actor='unit-test',
        )


# ---------------------------------------------------------------------------
# Reverse-path guards: CAS state-divergence checks and rowcount checks.
# ---------------------------------------------------------------------------


def _make_session_with_rowcounts(rows_and_rowcounts: list):
    """Like ``_make_session`` but each entry is ``(row, rowcount)``.

    Lets a test simulate "UPDATE matched zero rows" (deleted target) by
    yielding a zero-rowcount Result on the appropriate UPDATE call.
    """
    captured: list[tuple] = []

    class _Result:
        def __init__(self, row, rowcount=1):
            self._row = row
            self.rowcount = rowcount

        def first(self):
            return self._row

    async def execute(sql, params=None):
        try:
            sql_text = str(getattr(sql, 'text', sql))
        except Exception:
            sql_text = ''
        captured.append((sql_text, params))
        idx = len(captured) - 1
        if idx < len(rows_and_rowcounts):
            entry = rows_and_rowcounts[idx]
            if isinstance(entry, tuple):
                row, rowcount = entry
                return _Result(row, rowcount=rowcount)
            return _Result(entry)
        return _Result(None)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session._captured = captured

    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return session, cm


def _resolved_proposal(
    *,
    effective_action: str,
    loser_unit_id: str | None = None,
    winner_unit_id: str | None = None,
    link_id: str | None = None,
    prior_state: dict | None = None,
    applied_state: dict | None = None,
):
    """Build a resolved proposal row for reverse-path tests."""
    return _row(
        id=str(uuid4()),
        vault_id='11111111-1111-1111-1111-111111111111',
        rule_name='propose_contradiction_winner',
        target_type='memory_unit',
        target_id=loser_unit_id or str(uuid4()),
        evidence=json.dumps(
            {
                'action': effective_action,
                'winner_unit_id': winner_unit_id,
                'loser_unit_id': loser_unit_id,
                'link_id': link_id,
                'resolution': {
                    'schema_version': 1,
                    'action': effective_action,
                    'effective_action': effective_action,
                    'actor': 'unit-test',
                    'prior_state': prior_state or {},
                    'applied_state': applied_state or {},
                    'applied': {'action': effective_action},
                },
            }
        ),
        status='resolved',
    )


@pytest.mark.asyncio
async def test_reverse_mark_loser_stale_refuses_when_state_diverged():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_id = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='mark_loser_stale',
        loser_unit_id=loser_id,
        prior_state={'loser_unit_status': 'active'},
        applied_state={'loser_unit_status': 'stale'},
    )
    # Current state is 'archived' — diverged from applied 'stale'.
    current_unit = _row(id=loser_id, status='archived', note_id=str(uuid4()))
    session, cm = _make_session_with_rowcounts([proposal, current_unit])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='diverged'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_supersede_loser_note_refuses_when_state_diverged():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_note = str(uuid4())
    winner_note = str(uuid4())
    other_note = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='supersede_loser_note',
        loser_unit_id=str(uuid4()),
        winner_unit_id=str(uuid4()),
        prior_state={'loser_note_id': loser_note, 'loser_note_superseded_by': None},
        applied_state={'loser_note_id': loser_note, 'loser_note_superseded_by': winner_note},
    )
    # Current superseded_by points elsewhere — diverged.
    current_note = _row(id=loser_note, superseded_by=other_note)
    session, cm = _make_session_with_rowcounts([proposal, current_note])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='diverged'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_refine_not_contradict_refuses_when_state_diverged():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    link_id = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='refine_not_contradict',
        loser_unit_id=str(uuid4()),
        winner_unit_id=str(uuid4()),
        link_id=link_id,
        prior_state={'link_id': link_id, 'link_type': 'contradicts'},
        applied_state={'link_id': link_id, 'link_type': 'refines'},
    )
    # Link is now 'supports' — someone else mutated it after apply.
    current_link = _row(
        id=link_id, link_type='supports', from_unit_id=str(uuid4()), to_unit_id=str(uuid4())
    )
    session, cm = _make_session_with_rowcounts([proposal, current_link])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='diverged'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_mark_loser_stale_refuses_when_unit_deleted():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_id = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='mark_loser_stale',
        loser_unit_id=loser_id,
        prior_state={'loser_unit_status': 'active'},
        applied_state={'loser_unit_status': 'stale'},
    )
    # Loader returns None — unit no longer exists.
    session, cm = _make_session_with_rowcounts([proposal, None])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='no longer exists'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_supersede_loser_note_refuses_when_note_deleted():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_note = str(uuid4())
    winner_note = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='supersede_loser_note',
        loser_unit_id=str(uuid4()),
        winner_unit_id=str(uuid4()),
        prior_state={'loser_note_id': loser_note, 'loser_note_superseded_by': None},
        applied_state={'loser_note_id': loser_note, 'loser_note_superseded_by': winner_note},
    )
    # Note loader returns None — note no longer exists.
    session, cm = _make_session_with_rowcounts([proposal, None])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='no longer exists'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_refine_not_contradict_refuses_when_link_deleted():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    link_id = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='refine_not_contradict',
        loser_unit_id=str(uuid4()),
        winner_unit_id=str(uuid4()),
        link_id=link_id,
        prior_state={'link_id': link_id, 'link_type': 'contradicts'},
        applied_state={'link_id': link_id, 'link_type': 'refines'},
    )
    # Link loader returns None — link no longer exists.
    session, cm = _make_session_with_rowcounts([proposal, None])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='no longer exists'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


# ---------------------------------------------------------------------------
# Apply-path rowcount guards — symmetric with the reverse-path guards above.
# Simulate a concurrent DELETE between the row loader (SELECT) and the
# mutating UPDATE inside the same transaction. The UPDATE matches zero rows;
# without the rowcount guard, the apply path would record applied_state and
# flip the finding to resolved as if the mutation succeeded, leaving the
# audit trail lying. With the guard, the apply path raises and the
# transaction rolls back via the existing async context manager.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_mark_loser_stale_refuses_when_unit_deleted_mid_txn():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    proposal = _proposal('mark_loser_stale', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    winner_row = _row(id=winner_id, status='active', note_id=str(uuid4()))
    # SELECT proposal -> SELECT loser unit -> SELECT winner unit ->
    # UPDATE unit status (rowcount=0, concurrent delete after the SELECT).
    rows_and_rowcounts: list = [
        proposal,
        loser_row,
        winner_row,
        (None, 0),
    ]
    session, cm = _make_session_with_rowcounts(rows_and_rowcounts)
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='Concurrent modification'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )
    assert not session.commit.await_count, 'apply must not commit when rowcount=0'


@pytest.mark.asyncio
async def test_apply_supersede_loser_note_refuses_when_note_deleted_mid_txn():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    loser_note = str(uuid4())
    winner_note = str(uuid4())
    proposal = _proposal('supersede_loser_note', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=loser_note)
    winner_row = _row(id=winner_id, status='active', note_id=winner_note)
    note_row = _row(id=loser_note, superseded_by=None)
    # SELECT proposal -> SELECT loser unit -> SELECT winner unit ->
    # SELECT note -> UPDATE note.superseded_by (rowcount=0, concurrent delete).
    rows_and_rowcounts: list = [
        proposal,
        loser_row,
        winner_row,
        note_row,
        (None, 0),
    ]
    session, cm = _make_session_with_rowcounts(rows_and_rowcounts)
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='Concurrent modification'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )
    assert not session.commit.await_count, 'apply must not commit when rowcount=0'


@pytest.mark.asyncio
async def test_apply_refine_not_contradict_refuses_when_link_deleted_mid_txn():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    link_id = str(uuid4())
    proposal = _proposal(
        'refine_not_contradict',
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_id=link_id,
    )
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    winner_row = _row(id=winner_id, status='active', note_id=str(uuid4()))
    link_row = _row(
        id=link_id,
        link_type='contradicts',
        from_unit_id=winner_id,
        to_unit_id=loser_id,
    )
    # SELECT proposal -> SELECT loser unit -> SELECT winner unit ->
    # SELECT link -> UPDATE link.link_type (rowcount=0, concurrent delete).
    rows_and_rowcounts: list = [
        proposal,
        loser_row,
        winner_row,
        link_row,
        (None, 0),
    ]
    session, cm = _make_session_with_rowcounts(rows_and_rowcounts)
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='Concurrent modification'):
        await apply_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )
    assert not session.commit.await_count, 'apply must not commit when rowcount=0'


# ---------------------------------------------------------------------------
# Reverse-path schema-version guard: refuses when applied state predates the
# CAS guard (resolution.schema_version missing or < 1). Apply stamps the
# marker, so any post-V5 proposal carries it; the guard exists to refuse
# hand-written or future-bugged proposals that would otherwise sneak past
# the CAS check by omitting applied_state.
# ---------------------------------------------------------------------------


def _resolved_proposal_without_schema_version(loser_unit_id: str) -> SimpleNamespace:
    return _row(
        id=str(uuid4()),
        vault_id='11111111-1111-1111-1111-111111111111',
        rule_name='propose_contradiction_winner',
        target_type='memory_unit',
        target_id=loser_unit_id,
        evidence=json.dumps(
            {
                'action': 'mark_loser_stale',
                'loser_unit_id': loser_unit_id,
                'resolution': {
                    'action': 'mark_loser_stale',
                    'effective_action': 'mark_loser_stale',
                    'actor': 'unit-test',
                    'prior_state': {'loser_unit_status': 'active'},
                    'applied_state': {'loser_unit_status': 'stale'},
                    'applied': {'action': 'mark_loser_stale'},
                },
            }
        ),
        status='resolved',
    )


@pytest.mark.asyncio
async def test_reverse_refuses_when_schema_version_missing():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_id = str(uuid4())
    proposal = _resolved_proposal_without_schema_version(loser_id)
    session, cm = _make_session_with_rowcounts([proposal])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='CAS guard'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_reverse_refuses_when_schema_version_too_low():
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_id = str(uuid4())
    proposal = _row(
        id=str(uuid4()),
        vault_id='11111111-1111-1111-1111-111111111111',
        rule_name='propose_contradiction_winner',
        target_type='memory_unit',
        target_id=loser_id,
        evidence=json.dumps(
            {
                'action': 'mark_loser_stale',
                'loser_unit_id': loser_id,
                'resolution': {
                    'schema_version': 0,
                    'action': 'mark_loser_stale',
                    'effective_action': 'mark_loser_stale',
                    'actor': 'unit-test',
                    'prior_state': {'loser_unit_status': 'active'},
                    'applied_state': {'loser_unit_status': 'stale'},
                    'applied': {'action': 'mark_loser_stale'},
                },
            }
        ),
        status='resolved',
    )
    session, cm = _make_session_with_rowcounts([proposal])
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='CAS guard'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )


@pytest.mark.asyncio
async def test_concurrent_reverse_loses_second_caller():
    """Two concurrent reverses both observe ``reversed_at=None`` on the
    initial SELECT. Caller A commits first; caller B's evidence UPDATE
    must match zero rows (guarded by the partial WHERE clause on
    ``reversed_at``) and raise ``already been reversed`` so the second
    caller doesn't double-write a reversal audit row."""
    from memex_core.services.contradiction_resolution import reverse_winner_proposal

    loser_id = str(uuid4())
    proposal = _resolved_proposal(
        effective_action='mark_loser_stale',
        loser_unit_id=loser_id,
        prior_state={'loser_unit_status': 'active'},
        applied_state={'loser_unit_status': 'stale'},
    )
    # Loader sees pre-reversal state (concurrent caller A hasn't committed yet);
    # the prior_state restore UPDATE succeeds; but the evidence UPDATE matches
    # zero rows because A's commit landed in between with reversed_at set.
    current_unit = _row(id=loser_id, status='stale', note_id=str(uuid4()))
    rows_and_rowcounts: list = [
        proposal,
        current_unit,
        (None, 1),  # restore unit status UPDATE: succeeds
        (None, 0),  # evidence UPDATE: 0 rows matched — A already reversed.
    ]
    session, cm = _make_session_with_rowcounts(rows_and_rowcounts)
    api = _api_with_session(cm)

    with pytest.raises(ContradictionResolutionError, match='already been reversed'):
        await reverse_winner_proposal(
            api,
            uuid4(),
            vault_id=UUID('11111111-1111-1111-1111-111111111111'),
            actor='unit-test',
        )
    assert not session.commit.await_count, 'reverse must not commit on race loss'


@pytest.mark.asyncio
async def test_apply_records_schema_version_in_resolution():
    loser_id = str(uuid4())
    winner_id = str(uuid4())
    proposal = _proposal('mark_loser_stale', loser_unit_id=loser_id, winner_unit_id=winner_id)
    loser_row = _row(id=loser_id, status='active', note_id=str(uuid4()))
    winner_row = _row(id=winner_id, status='active', note_id=str(uuid4()))
    session, cm = _make_session([proposal, loser_row, winner_row])
    api = _api_with_session(cm)

    await apply_winner_proposal(
        api,
        uuid4(),
        vault_id=UUID('11111111-1111-1111-1111-111111111111'),
        actor='unit-test',
    )

    evidence_writes = [
        params
        for sql, params in session._captured
        if isinstance(params, dict) and 'evidence' in params and isinstance(params['evidence'], str)
    ]
    assert evidence_writes, 'expected at least one evidence UPDATE'
    written = json.loads(evidence_writes[-1]['evidence'])
    assert written['resolution']['schema_version'] == 1
