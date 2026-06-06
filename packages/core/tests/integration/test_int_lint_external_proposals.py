"""Integration tests for external lint proposals against real Postgres.

Covers the insert-layer contract (create / dedup / cooldown / source CHECK)
and the entity-merge machinery the new catalogue actions bind to. The
HTTP-route contract (batch semantics, gates, destructive resolve) lives in
``test_int_lint_proposal_routes.py``; the live-server lifecycle runs in the
maintenance_cockpit eval suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import Entity, Vault
from memex_core.services.entities import EntityService
from memex_core.services.lint_external import (
    ExternalProposalRequest,
    insert_external_proposal,
)


def _fake_api(metastore, *, cooldown_days: int = 30):
    """api-shaped object: just the config + metastore the insert path reads."""
    external = SimpleNamespace(cooldown_days=cooldown_days, max_batch=100, require_vault=True)
    lint = SimpleNamespace(external_proposals=external)
    memory = SimpleNamespace(lint=lint)
    server = SimpleNamespace(memory=memory)
    return SimpleNamespace(metastore=metastore, config=SimpleNamespace(server=server))


def _routing_request(vault_id, *, rule_name: str = 'skill-misroute', target_id=None):
    return ExternalProposalRequest(
        vault_id=str(vault_id),
        rule_name=rule_name,
        lint_type='routing',
        target_type='note',
        target_id=str(target_id or uuid4()),
        description=f'classifier was confident but wrong {uuid4()}',
        suggested_action='route the note to the agentic vault',
        evidence={'confidence': 0.93},
        proposed_action={
            'action_name': 'route_note_to_vault',
            'params': {'target_vault_id': str(uuid4())},
        },
    )


async def _make_vault(session: AsyncSession) -> Vault:
    vault = Vault(name=f'ext-proposals-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_creates_pending_external_row(session: AsyncSession, metastore):
    vault = await _make_vault(session)
    api = _fake_api(metastore)
    req = _routing_request(vault.id)

    status, finding_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    assert status == 'created'
    assert finding_id is not None

    async with metastore.session() as s:
        row = (
            (
                await s.execute(
                    text(
                        'SELECT source, lint_type, rule_name, evidence, suggested_action '
                        'FROM maintenance_proposals WHERE id = :id'
                    ),
                    {'id': str(finding_id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row['source'] == 'external'
    assert row['lint_type'] == 'routing'
    assert row['rule_name'] == 'skill-misroute'
    evidence = row['evidence']
    assert evidence['confidence'] == 0.93
    assert evidence['rule_metadata']['submitted_by'] == 'skill:triage-inbox'
    assert evidence['rule_metadata']['description'] == req.description
    assert evidence['proposed_action']['action_name'] == 'route_note_to_vault'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_with_sql_metacharacters_round_trips_safely(
    session: AsyncSession, metastore
):
    """Evidence values are BOUND parameters, never inlined SQL. A single quote /
    injection-looking payload must round-trip verbatim — proving the Core insert
    (cast(literal(json.dumps(evidence)), JSONB)) binds rather than splices.
    If it inlined, this would raise a syntax error or corrupt the table."""
    vault = await _make_vault(session)
    api = _fake_api(metastore)
    evil = {
        'note': "O'Brien'); DROP TABLE maintenance_proposals;--",
        'quote': "it's a 'quoted' value",
        'list': ["a'b", 'c;d'],
    }
    req = ExternalProposalRequest(
        vault_id=str(vault.id),
        rule_name='skill-sqlsafe',
        lint_type='routing',
        target_type='note',
        target_id=str(uuid4()),
        description=f'evidence sql-safety {uuid4()}',
        suggested_action='review',
        evidence=evil,
    )

    status, finding_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:test'
    )
    assert status == 'created'
    assert finding_id is not None

    async with metastore.session() as s:
        row = (
            (
                await s.execute(
                    text('SELECT evidence FROM maintenance_proposals WHERE id = :id'),
                    {'id': str(finding_id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    ev = row['evidence']
    # Verbatim round-trip — the metacharacters were data, not SQL.
    assert ev['note'] == evil['note']
    assert ev['quote'] == evil['quote']
    assert ev['list'] == evil['list']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resubmission_deduplicates_to_existing_row(session: AsyncSession, metastore):
    vault = await _make_vault(session)
    api = _fake_api(metastore)
    req = _routing_request(vault.id)

    first_status, first_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    second_status, second_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    assert first_status == 'created'
    assert second_status == 'deduplicated'
    assert second_id == first_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cooldown_suppresses_after_human_dismissal(session: AsyncSession, metastore):
    vault = await _make_vault(session)
    api = _fake_api(metastore)
    req = _routing_request(vault.id)

    _, finding_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    async with metastore.session() as s:
        await s.execute(
            text(
                "UPDATE maintenance_proposals SET status = 'dismissed', resolved_at = :ts "
                'WHERE id = :id'
            ),
            {'id': str(finding_id), 'ts': datetime.now(timezone.utc)},
        )
        await s.commit()

    status, suppressed_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    assert status == 'cooldown_suppressed'
    assert suppressed_id is None

    # Zero-day cooldown disables the guard — the same submission lands fresh.
    relaxed = _fake_api(metastore, cooldown_days=0)
    status_again, fresh_id = await insert_external_proposal(
        relaxed, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    assert status_again == 'created'
    assert fresh_id is not None and fresh_id != finding_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cooldown_expires_after_window(session: AsyncSession, metastore):
    vault = await _make_vault(session)
    api = _fake_api(metastore, cooldown_days=30)
    req = _routing_request(vault.id)

    _, finding_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    stale = datetime.now(timezone.utc) - timedelta(days=31)
    async with metastore.session() as s:
        await s.execute(
            text(
                "UPDATE maintenance_proposals SET status = 'resolved', resolved_at = :ts "
                'WHERE id = :id'
            ),
            {'id': str(finding_id), 'ts': stale},
        )
        await s.commit()

    status, new_id = await insert_external_proposal(
        api, req, vault_id=vault.id, actor='skill:triage-inbox'
    )
    assert status == 'created'
    assert new_id is not None and new_id != finding_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_check_constraint_accepts_external_only(session: AsyncSession):
    """Migration 057 widened the CHECK to rule/llm/external — nothing else."""
    vault = await _make_vault(session)
    for source in ('rule', 'llm', 'external'):
        await session.execute(
            text(
                'INSERT INTO maintenance_proposals '
                '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                ' suggested_action, status, source) '
                "VALUES (:vault_id, 'quality', 'memory_unit', :target_id, :rule_name, "
                "'{}'::jsonb, 'noop', 'pending', :source)"
            ),
            {
                'vault_id': str(vault.id),
                'target_id': str(uuid4()),
                'rule_name': f'src-check-{source}-{uuid4().hex[:6]}',
                'source': source,
            },
        )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                'INSERT INTO maintenance_proposals '
                '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                ' suggested_action, status, source) '
                "VALUES (:vault_id, 'quality', 'memory_unit', :target_id, 'bad-source-rule', "
                "'{}'::jsonb, 'noop', 'pending', 'martian')"
            ),
            {'vault_id': str(vault.id), 'target_id': str(uuid4())},
        )
    await session.rollback()


def _entity_service(metastore) -> EntityService:
    return EntityService(metastore=metastore, filestore=MagicMock(), config=MagicMock())


async def _seed_entities(session: AsyncSession, names: list[str]) -> list[Entity]:
    entities = [Entity(canonical_name=name, mention_count=i + 1) for i, name in enumerate(names)]
    session.add_all(entities)
    await session.commit()
    for entity in entities:
        await session.refresh(entity)
    return entities


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_into_new_entity_end_to_end(session: AsyncSession, metastore):
    suffix = uuid4().hex[:8]
    members = await _seed_entities(
        session, [f'ACME Corp {suffix}', f'Acme Corporation {suffix}', f'acme inc {suffix}']
    )
    svc = _entity_service(metastore)

    summary = await svc.collapse_into_new_entity(
        member_ids=[e.id for e in members],
        new_canonical_name=f'Acme {suffix}',
        actor='test:cockpit',
    )
    created_id = summary['created_entity_id']
    assert summary['created_canonical_name'] == f'Acme {suffix}'

    async with metastore.session() as s:
        survivor = (
            (
                await s.execute(
                    text('SELECT canonical_name, mention_count FROM entities WHERE id = :id'),
                    {'id': created_id},
                )
            )
            .mappings()
            .first()
        )
        remaining = (
            await s.execute(
                text('SELECT count(*) FROM entities WHERE id = ANY(CAST(:ids AS uuid[]))'),
                {'ids': [str(e.id) for e in members]},
            )
        ).scalar()
    assert survivor is not None
    assert survivor['canonical_name'] == f'Acme {suffix}'
    # Bare survivor starts at 0 mentions; collapse sums the members exactly.
    assert survivor['mention_count'] == sum(e.mention_count for e in members)
    assert remaining == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collapse_into_new_entity_rejects_existing_name(session: AsyncSession, metastore):
    suffix = uuid4().hex[:8]
    existing, member_a, member_b = await _seed_entities(
        session, [f'Taken {suffix}', f'Dup A {suffix}', f'Dup B {suffix}']
    )
    svc = _entity_service(metastore)
    with pytest.raises(ValueError, match='already exists'):
        await svc.collapse_into_new_entity(
            member_ids=[member_a.id, member_b.id],
            new_canonical_name=f'Taken {suffix}',
            actor='test:cockpit',
        )
    async with metastore.session() as s:
        count = (
            await s.execute(
                text('SELECT count(*) FROM entities WHERE id = ANY(CAST(:ids AS uuid[]))'),
                {'ids': [str(existing.id), str(member_a.id), str(member_b.id)]},
            )
        ).scalar()
    assert count == 3  # nothing merged, nothing deleted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_entities_action_resolves_through_registry(session: AsyncSession, metastore):
    """The registry action drives the same audited collapse the carveout uses."""
    from memex_core.services.proposal_actions import get_action

    suffix = uuid4().hex[:8]
    winner, loser = await _seed_entities(session, [f'Winner {suffix}', f'Loser {suffix}'])
    api = SimpleNamespace(entities=_entity_service(metastore))

    action = get_action('merge_entities')
    params = {'winner_id': str(winner.id), 'member_ids': [str(winner.id), str(loser.id)]}
    action.validate(params, target_type='entity', target_id=str(winner.id))
    result = await action.execute(
        api, params, target_id=str(winner.id), vault_id=None, actor='test:cockpit'
    )
    assert result.applied_state['winner_id'] == str(winner.id)

    async with metastore.session() as s:
        rows = (
            await s.execute(
                text('SELECT id, mention_count FROM entities WHERE id = ANY(CAST(:ids AS uuid[]))'),
                {'ids': [str(winner.id), str(loser.id)]},
            )
        ).mappings()
        alive = {str(r['id']): r['mention_count'] for r in rows}
    assert set(alive) == {str(winner.id)}
    assert alive[str(winner.id)] == winner.mention_count + loser.mention_count
