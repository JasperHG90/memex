"""Integration tests for the content/system vault distinction (V11).

Covers the load-bearing guarantees against real Postgres:
- create_vault persists kind + policy; kind defaults to content.
- list_vaults(include_system=...) filters; system addressable by id.
- resolve_vault_scope: '*'/None -> content only; include_system_vaults -> +system;
  named system vault resolves regardless of kind.
- Reflection enqueue is skipped for reflect-disabled (system) vaults, and a
  per-vault policy override re-enables it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import ReflectionConfig
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import Entity, Note, ReflectionQueue, Vault

pytestmark = [pytest.mark.integration]


@pytest.fixture
def queue_service() -> ReflectionQueueService:
    return ReflectionQueueService(
        ReflectionConfig(weight_urgency=1.0, weight_importance=0.0, weight_resonance=0.0)
    )


# ── Vault service contract (via the api fixture) ──


@pytest.mark.asyncio
async def test_create_persists_kind_and_policy(api):
    content = await api.create_vault(f'c-{uuid4().hex[:8]}')
    system = await api.create_vault(f's-{uuid4().hex[:8]}', kind='system', policy={'reflect': True})
    assert content.kind == 'content'
    assert content.policy == {}
    assert system.kind == 'system'
    assert system.policy == {'reflect': True}


@pytest.mark.asyncio
async def test_list_vaults_filters_system_but_stays_addressable(api):
    content = await api.create_vault(f'c-{uuid4().hex[:8]}')
    system = await api.create_vault(f's-{uuid4().hex[:8]}', kind='system')

    default_ids = {v.id for v in await api.list_vaults(include_system=False)}
    all_ids = {v.id for v in await api.list_vaults(include_system=True)}

    assert content.id in default_ids
    assert system.id not in default_ids  # silent on the default listing
    assert system.id in all_ids
    # Addressability: a system vault is always resolvable by id.
    assert await api.validate_vault_exists(system.id) is True


@pytest.mark.asyncio
async def test_resolve_vault_scope_semantics(api):
    content = await api.create_vault(f'c-{uuid4().hex[:8]}')
    sys_name = f's-{uuid4().hex[:8]}'
    system = await api.create_vault(sys_name, kind='system')

    wildcard = await api.resolve_vault_scope(['*'])
    assert content.id in wildcard
    assert system.id not in wildcard  # '*' = content only

    wildcard_sys = await api.resolve_vault_scope(['*'], include_system_vaults=True)
    assert system.id in wildcard_sys

    named = await api.resolve_vault_scope([sys_name])
    assert named == [system.id]  # named system vault resolves regardless of kind

    default_scope = await api.resolve_vault_scope(None)
    assert content.id in default_scope
    assert system.id not in default_scope


# ── Default-scope note reads exclude system vaults (regression for find/list) ──


@pytest.mark.asyncio
async def test_default_scope_excludes_system_notes(api, session):
    content = await api.create_vault(f'c-{uuid4().hex[:8]}')
    system = await api.create_vault(f's-{uuid4().hex[:8]}', kind='system')

    tag = uuid4().hex[:8]
    title = f'deploy runbook {tag}'
    session.add_all(
        [
            Note(id=uuid4(), vault_id=content.id, title=title),
            Note(id=uuid4(), vault_id=system.id, title=title),
        ]
    )
    await session.commit()

    # list_notes default scope → content only.
    listed_vaults = {n.vault_id for n in await api.list_notes()}
    assert content.id in listed_vaults
    assert system.id not in listed_vaults

    # find_notes_by_title default scope → content only (regression for the C1 leak).
    found_vaults = {r['vault_id'] for r in await api.find_notes_by_title(title)}
    assert content.id in found_vaults
    assert system.id not in found_vaults

    # Explicitly naming the system vault still reaches it (addressability).
    named = await api.find_notes_by_title(title, vault_ids=[system.id])
    assert {r['vault_id'] for r in named} == {system.id}


# ── Reflection enqueue gating (via the session fixture) ──


@pytest.mark.asyncio
async def test_system_vault_skips_reflection_enqueue(
    session: AsyncSession, queue_service: ReflectionQueueService
):
    content = Vault(name=f'c-{uuid4().hex[:8]}', kind='content')
    system = Vault(name=f's-{uuid4().hex[:8]}', kind='system')
    entity = Entity(canonical_name=f'E-{uuid4().hex[:8]}')
    session.add_all([content, system, entity])
    await session.commit()

    # Content vault enqueues a reflection row.
    await queue_service.handle_extraction_event(session, {entity.id}, vault_id=content.id)
    content_rows = (
        await session.exec(
            select(ReflectionQueue).where(
                col(ReflectionQueue.entity_id) == entity.id,
                col(ReflectionQueue.vault_id) == content.id,
            )
        )
    ).all()
    assert len(content_rows) == 1

    # System vault (reflect disabled by default) enqueues nothing.
    await queue_service.handle_extraction_event(session, {entity.id}, vault_id=system.id)
    system_rows = (
        await session.exec(
            select(ReflectionQueue).where(
                col(ReflectionQueue.entity_id) == entity.id,
                col(ReflectionQueue.vault_id) == system.id,
            )
        )
    ).all()
    assert system_rows == []


@pytest.mark.asyncio
async def test_policy_override_re_enables_reflection(
    session: AsyncSession, queue_service: ReflectionQueueService
):
    system = Vault(name=f's-{uuid4().hex[:8]}', kind='system', policy={'reflect': True})
    entity = Entity(canonical_name=f'E-{uuid4().hex[:8]}')
    session.add_all([system, entity])
    await session.commit()

    await queue_service.handle_extraction_event(session, {entity.id}, vault_id=system.id)
    rows = (
        await session.exec(
            select(ReflectionQueue).where(
                col(ReflectionQueue.entity_id) == entity.id,
                col(ReflectionQueue.vault_id) == system.id,
            )
        )
    ).all()
    assert len(rows) == 1  # policy override re-enables enqueue
