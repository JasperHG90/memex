"""Integration: EVENT-cluster case auto-promotion (§8 / §18.6.2).

A content-vault note whose extraction yielded a cluster of EVENT memory
units is proposed as a case via the lint surface (drafts requiring
confirmation, never silent); the set_note_role action stamps role='case'
on confirm.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from memex_core.memory.sql_models import EMBEDDING_DIMENSION, MaintenanceProposal, MemoryUnit, Note
from memex_core.services.proposal_actions import get_action

pytestmark = [pytest.mark.integration]


async def _mk_content_vault(api) -> uuid.UUID:
    vid = uuid.uuid4()
    async with api.metastore.session() as s:
        await s.execute(
            text("INSERT INTO vaults (id, name, kind) VALUES (:id, :n, 'content')"),
            {'id': str(vid), 'n': f'ec_{vid.hex[:8]}'},
        )
        await s.commit()
    return vid


async def _mk_note(api, vault_id, *, n_events: int) -> uuid.UUID:
    nid = uuid.uuid4()
    async with api.metastore.session() as s:
        s.add(
            Note(
                id=nid,
                vault_id=vault_id,
                session_id='ec-test',
                status='active',
                original_text='a multi-step worked episode',
                title='Worked episode',
            )
        )
        for _ in range(n_events):
            s.add(
                MemoryUnit(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    note_id=nid,
                    text='did a step',
                    fact_type='event',
                    embedding=[0.0] * EMBEDDING_DIMENSION,
                    event_date=datetime.now(timezone.utc),
                )
            )
        await s.commit()
    return nid


async def test_event_cluster_note_proposed_and_set_note_role_promotes(api):
    vault = await _mk_content_vault(api)
    note_id = await _mk_note(api, vault, n_events=3)

    proposed = await api.propose_event_cases(limit=20, min_events=3)
    assert note_id in proposed

    # A proposal targeting the note was filed, pre-selecting set_note_role.
    async with api.metastore.session() as s:
        props = (
            await s.exec(
                select(MaintenanceProposal)
                .where(col(MaintenanceProposal.target_type) == 'note')
                .where(col(MaintenanceProposal.target_id) == str(note_id))
            )
        ).all()
    assert len(props) == 1
    assert props[0].rule_name == 'event_case_promotion'

    # Confirm: set_note_role stamps role='case'.
    action = get_action('set_note_role')
    action.validate({'role': 'case'}, target_type='note', target_id=str(note_id))
    result = await action.execute(
        api, {'role': 'case'}, target_id=str(note_id), vault_id=vault, actor='reviewer'
    )
    async with api.metastore.session() as s:
        assert (await s.get(Note, note_id)).role == 'case'

    # Reverse restores the prior (null) role.
    await action.reverse(
        api,
        {},
        result.applied_state,
        result.prior_state,
        target_id=str(note_id),
        vault_id=vault,
        actor='reviewer',
    )
    async with api.metastore.session() as s:
        assert (await s.get(Note, note_id)).role is None


async def test_below_threshold_and_existing_case_not_proposed(api):
    vault = await _mk_content_vault(api)
    thin = await _mk_note(api, vault, n_events=2)  # < 3 events
    already = await _mk_note(api, vault, n_events=4)
    async with api.metastore.session() as s:
        note = await s.get(Note, already)
        note.role = 'case'
        s.add(note)
        await s.commit()

    proposed = await api.propose_event_cases(limit=20, min_events=3)
    assert thin not in proposed  # too few events
    assert already not in proposed  # already a case
