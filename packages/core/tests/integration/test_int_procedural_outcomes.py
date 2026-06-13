"""Integration: Phase 2 outcome tracking on procedural entries (§18.5).

Counters live on the entry; the write paths are (a) an explicit
``procedure_report`` (report_outcome) and (b) case assignment (the case IS
the outcome report). Ranking boosts by the Beta-Bernoulli posterior.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from memex_common.procedural_schemas import ProceduralEntryCreate

pytestmark = [pytest.mark.integration]


async def _mk_vault(api) -> uuid.UUID:
    vid = uuid.uuid4()
    async with api.metastore.session() as session:
        await session.execute(
            text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
            {'id': str(vid), 'name': f'outc_{vid.hex[:8]}'},
        )
        await session.commit()
    return vid


async def _mk_procedure(api, vault_id, *, verb, context):
    return await api.procedural.create(
        ProceduralEntryCreate(
            vault_id=vault_id,
            kind='procedure',
            scope='global',
            verb=verb,
            context=context,
            title=f'{verb}:{context}',
            summary='s',
            body='b',
            trigger=f'when {verb} {context}',
            status='published',
            origin='manual',
        )
    )


async def test_report_outcome_increments_counters(api):
    vault = await _mk_vault(api)
    p = await _mk_procedure(api, vault, verb='deploy', context='nomad')
    assert p.success_count == 0 and p.uses == 0 and p.last_used_at is None

    p1 = await api.procedural.report_outcome(p.id, 'success')
    assert p1.success_count == 1 and p1.uses == 1 and p1.last_used_at is not None

    p2 = await api.procedural.report_outcome(p.id, 'failure')
    assert p2.failure_count == 1 and p2.uses == 2

    p3 = await api.procedural.report_outcome(p.id, 'mixed')
    assert p3.mixed_count == 1 and p3.uses == 3 and p3.success_count == 1

    with pytest.raises(Exception):
        await api.procedural.report_outcome(p.id, 'bogus')


async def test_case_assignment_records_outcome(api):
    """A case assigned with case_of + outcome bumps the target's counter."""
    from memex_core.memory.sql_models import Note
    from memex_core.services.case_service import CaseService

    vault = await _mk_vault(api)
    p = await _mk_procedure(api, vault, verb='release', context='monorepo')

    # A case note stamped with outcome=success in its metadata.
    note_id = uuid.uuid4()
    async with api.metastore.session() as session:
        session.add(
            Note(
                id=note_id,
                vault_id=vault,
                session_id='test-outcome',
                status='active',
                original_text='release monorepo case',
                role='case',
                doc_metadata={'outcome': 'success', 'case_of': str(p.id)},
            )
        )
        await session.commit()

    await CaseService(api).apply_assignment(note_id=note_id, entry_id=p.id)

    after = await api.procedural.get(p.id)
    assert after.success_count == 1  # the case recorded its outcome
    assert after.uses == 1


async def test_mw_boost_ranks_proven_procedure_higher(api):
    """A well-worn procedure outranks an unproven peer on the same trigger."""
    vault = await _mk_vault(api)
    proven = await _mk_procedure(api, vault, verb='scale', context='up')
    unproven = await _mk_procedure(api, vault, verb='scale', context='out')

    # Give `proven` a strong success record.
    for _ in range(8):
        await api.procedural.report_outcome(proven.id, 'success')

    from memex_common.procedural_schemas import ProceduralSearchRequest

    resp = await api.procedural.search(
        ProceduralSearchRequest(query='scale', scope='global', limit=10, status='published')
    )
    ids = [h.entry.id for h in resp.hits]
    assert proven.id in ids and unproven.id in ids
    # Proven ranks at or above the unproven peer (MW boost, §18.5).
    assert ids.index(proven.id) <= ids.index(unproven.id)


async def test_create_case_reverse_unwinds_draft_assignment(api):
    """create_case.reverse archives the case note, detaches the provenance
    edge, and deprecates a draft anchor the assignment minted (the
    reversible-on-review contract)."""
    from sqlmodel import col, select

    from memex_common.procedural_schemas import ProceduralSourceCreate
    from memex_core.memory.sql_models import Note, ProceduralSource
    from memex_core.services.proposal_actions import get_action

    vault = await _mk_vault(api)
    # A draft anchor as if a clean `new_procedure` verdict had just minted it.
    draft = await api.procedural.create(
        ProceduralEntryCreate(
            vault_id=vault,
            kind='procedure',
            scope='global',
            verb='deploy',
            context='edge',
            title='deploy:edge',
            summary='s',
            body='',
            trigger='when deploy edge',
            status='draft',
            origin='derived',
        )
    )
    # The case note + the provenance edge the assignment created.
    note_id = uuid.uuid4()
    async with api.metastore.session() as session:
        session.add(
            Note(
                id=note_id,
                vault_id=vault,
                session_id='test-create-case',
                status='active',
                original_text='deploy edge case body',
                role='case',
                doc_metadata={'outcome': 'success'},
            )
        )
        await session.commit()
    await api._procedural_repo.add_source(
        draft.id, ProceduralSourceCreate(source_note_id=note_id, role='provenance')
    )

    action = get_action('create_case')
    result = await action.reverse(
        api,
        {},
        {
            'case_note_id': str(note_id),
            'assignment_mode': 'new_procedure_draft',
            'entry_id': str(draft.id),
            'finding_id': None,
        },
        {},
        target_id=str(uuid.uuid4()),
        vault_id=vault,
        actor='reviewer',
    )

    # Provenance edge detached.
    async with api.metastore.session() as session:
        edges = (
            await session.exec(
                select(ProceduralSource).where(col(ProceduralSource.source_note_id) == note_id)
            )
        ).all()
    assert edges == []
    assert result.restored_state['detached'] == 1

    # Draft anchor deprecated (not deleted — ledger/audit intact).
    assert (await api.procedural.get(draft.id)).status == 'deprecated'
    assert result.restored_state['deprecated_draft'] == str(draft.id)

    # Case note archived (non-destructive: archived_at stamped, row intact).
    async with api.metastore.session() as session:
        note = await session.get(Note, note_id)
    assert note is not None
    assert note.archived_at is not None
