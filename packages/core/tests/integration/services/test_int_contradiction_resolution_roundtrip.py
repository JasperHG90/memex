"""Integration test — apply/reverse roundtrip for winner-proposal findings.

For each ``action`` literal:
1. Seed a pending ``propose_contradiction_winner`` finding with the
   action's required evidence payload.
2. Apply via :func:`apply_winner_proposal`; assert the target mutation
   is persisted.
3. Reverse via :func:`reverse_winner_proposal`; assert the mutation is
   undone and a paired audit row is written.

Requires Docker/Postgres via the standard ``api`` / ``session`` fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    MemoryLink,
    MemoryUnit,
    Note,
    Vault,
)
from memex_core.services.contradiction_resolution import (
    apply_winner_proposal,
    reverse_winner_proposal,
)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_two_units(
    session: AsyncSession,
    *,
    same_note: bool = False,
) -> tuple[tuple[UUID, UUID, UUID, UUID, UUID], dict[str, str]]:
    """Seed two units (winner=A, loser=B) and an A→B contradicts link.

    Returns
    -------
    ``((vault_id, winner_unit_id, loser_unit_id, winner_note_id, loser_note_id), link_key)``
    where ``link_key`` is the composite primary key of ``memory_links``
    ``{from_unit_id, to_unit_id, link_type}``.
    """
    vault = Vault(name=f'V5-int-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    winner_note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='winner',
    )
    loser_note = (
        winner_note
        if same_note
        else Note(
            id=uuid4(),
            vault_id=vault.id,
            content_hash=f'hash-{uuid4().hex[:8]}',
            original_text='loser',
        )
    )
    session.add(winner_note)
    if not same_note:
        session.add(loser_note)
    await session.commit()

    winner = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=winner_note.id,
        text='canonical winner fact',
        fact_type=FactTypes.WORLD,
        status='active',
        risk_class='none',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    loser = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=loser_note.id,
        text='outdated loser fact',
        fact_type=FactTypes.WORLD,
        status='active',
        risk_class='none',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add_all([winner, loser])
    await session.commit()

    link = MemoryLink(
        vault_id=vault.id,
        from_unit_id=winner.id,
        to_unit_id=loser.id,
        link_type='contradicts',
        weight=0.9,
    )
    session.add(link)
    await session.commit()

    link_key = {
        'from_unit_id': str(winner.id),
        'to_unit_id': str(loser.id),
        'link_type': 'contradicts',
    }
    return (vault.id, winner.id, loser.id, winner_note.id, loser_note.id), link_key


async def _seed_proposal(
    session: AsyncSession,
    *,
    vault_id: UUID,
    loser_unit_id: UUID,
    winner_unit_id: UUID,
    link_key: dict[str, str] | None,
    action: str,
) -> UUID:
    evidence = {
        'check_type': 'propose_contradiction_winner',
        'action': action,
        'winner_id': 'unit_a',
        'loser_id': 'unit_b',
        'winner_unit_id': str(winner_unit_id),
        'loser_unit_id': str(loser_unit_id),
        'peer_unit_id': str(winner_unit_id),
        'link_key': link_key,
        'linked_to_finding': str(uuid4()),
        'flag_reason': 'low_credibility_contradiction_only',
        'confidence': 0.85,
        'rationale': 'integration-test seed',
    }
    finding_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO maintenance_proposals (
                id, vault_id, lint_type, target_type, target_id,
                rule_name, evidence, suggested_action, status, source
            )
            VALUES (
                :id, :vault_id, 'quality', 'memory_unit', :target_id,
                'propose_contradiction_winner', CAST(:evidence AS jsonb),
                'integration test', 'pending', 'llm'
            )
        """),
        {
            'id': str(finding_id),
            'vault_id': str(vault_id),
            'target_id': str(loser_unit_id),
            'evidence': json.dumps(evidence),
        },
    )
    await session.commit()
    return finding_id


async def _read_proposal_evidence(session: AsyncSession, finding_id: UUID) -> dict:
    row = (
        await session.execute(
            text('SELECT evidence FROM maintenance_proposals WHERE id = :id'),
            {'id': str(finding_id)},
        )
    ).first()
    return dict(row.evidence) if row else {}


async def _read_unit_status(session: AsyncSession, unit_id: UUID) -> str:
    row = (
        await session.execute(
            text('SELECT status FROM memory_units WHERE id = :id'),
            {'id': str(unit_id)},
        )
    ).first()
    return row.status if row else ''


async def _read_link_type(session: AsyncSession, *, from_unit_id: str, to_unit_id: str) -> str:
    """Read the live link_type for an (from, to) pair regardless of current type.

    ``memory_links`` has composite PK ``(from_unit_id, to_unit_id, link_type)``;
    apply/reverse rewrite ``link_type`` so we cannot key the lookup on the
    original literal. The seed creates exactly one (from, to) link per test
    so this returns at most one row.
    """
    row = (
        await session.execute(
            text(
                'SELECT link_type FROM memory_links '
                'WHERE from_unit_id = :from_id AND to_unit_id = :to_id'
            ),
            {'from_id': from_unit_id, 'to_id': to_unit_id},
        )
    ).first()
    return row.link_type if row else ''


async def _read_note_superseded_by(session: AsyncSession, note_id: UUID):
    row = (
        await session.execute(
            text('SELECT superseded_by FROM notes WHERE id = :id'),
            {'id': str(note_id)},
        )
    ).first()
    return row.superseded_by if row else None


async def test_mark_loser_stale_roundtrip(session: AsyncSession, api) -> None:
    (vault_id, winner_id, loser_id, _, _), link_key = await _seed_two_units(session)
    finding_id = await _seed_proposal(
        session,
        vault_id=vault_id,
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_key=link_key,
        action='mark_loser_stale',
    )

    await apply_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_unit_status(session, loser_id) == 'stale'

    await reverse_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_unit_status(session, loser_id) == 'active'

    reversal = (
        await session.execute(
            text("""
                SELECT id FROM maintenance_proposals
                WHERE rule_name = 'propose_contradiction_winner_reversal'
                  AND evidence ->> 'reverses_finding_id' = :fid
            """),
            {'fid': str(finding_id)},
        )
    ).first()
    assert reversal is not None


async def test_supersede_loser_note_roundtrip(session: AsyncSession, api) -> None:
    (vault_id, winner_id, loser_id, winner_note, loser_note), link_key = await _seed_two_units(
        session
    )
    finding_id = await _seed_proposal(
        session,
        vault_id=vault_id,
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_key=link_key,
        action='supersede_loser_note',
    )

    await apply_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_note_superseded_by(session, loser_note) == winner_note

    await reverse_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_note_superseded_by(session, loser_note) is None


async def test_supersede_loser_note_shared_parent_falls_back(session: AsyncSession, api) -> None:
    (vault_id, winner_id, loser_id, _, shared_note), link_key = await _seed_two_units(
        session, same_note=True
    )
    finding_id = await _seed_proposal(
        session,
        vault_id=vault_id,
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_key=link_key,
        action='supersede_loser_note',
    )

    result = await apply_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert result['effective_action'] == 'mark_loser_stale'
    assert result['fallback_reason'] == 'shared_parent_note'
    assert await _read_unit_status(session, loser_id) == 'stale'

    evidence = await _read_proposal_evidence(session, finding_id)
    assert evidence['resolution']['fallback_reason'] == 'shared_parent_note'


async def test_refine_not_contradict_roundtrip(session: AsyncSession, api) -> None:
    (vault_id, winner_id, loser_id, _, _), link_key = await _seed_two_units(session)
    finding_id = await _seed_proposal(
        session,
        vault_id=vault_id,
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_key=link_key,
        action='refine_not_contradict',
    )

    await apply_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert (
        await _read_link_type(
            session,
            from_unit_id=link_key['from_unit_id'],
            to_unit_id=link_key['to_unit_id'],
        )
        == 'refines'
    )

    await reverse_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert (
        await _read_link_type(
            session,
            from_unit_id=link_key['from_unit_id'],
            to_unit_id=link_key['to_unit_id'],
        )
        == 'contradicts'
    )


async def test_inconclusive_is_noop(session: AsyncSession, api) -> None:
    (vault_id, winner_id, loser_id, _, _), link_key = await _seed_two_units(session)
    finding_id = await _seed_proposal(
        session,
        vault_id=vault_id,
        loser_unit_id=loser_id,
        winner_unit_id=winner_id,
        link_key=link_key,
        action='inconclusive',
    )

    await apply_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_unit_status(session, loser_id) == 'active'
    assert (
        await _read_link_type(
            session,
            from_unit_id=link_key['from_unit_id'],
            to_unit_id=link_key['to_unit_id'],
        )
        == 'contradicts'
    )

    # Reversal is also a no-op write but still produces a paired audit row.
    await reverse_winner_proposal(api, finding_id, vault_id=vault_id, actor='itest')
    assert await _read_unit_status(session, loser_id) == 'active'
