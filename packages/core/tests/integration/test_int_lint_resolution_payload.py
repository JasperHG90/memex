"""Integration tests for the resolution payload extension on ``set_status``.

The cockpit needs ``evidence.resolution`` (verdict, actor, decided_at,
optional note, optional followup) written atomically with the status flip
so the audit trail and the status row stay consistent. This file pins
the storage-layer contract against real Postgres — the route-layer
behaviour is covered by service-level tests, and the TUI by the cockpit
smoke test.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    LintSource,
    LintStatus,
    LintType,
    MaintenanceProposal,
    Vault,
)
from memex_core.services.lint import LintService


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_status_writes_resolution_payload_atomically(
    session: AsyncSession,
    metastore,
):
    """Status flip + ``evidence.resolution`` land in the same UPDATE."""
    vault = Vault(name=f'cockpit-resolve-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='cold_low_mw_unit',
        evidence={'mw_score': 0.1, 'success_co_count': 0},
        suggested_action='deprioritize',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    resolution = {
        'verdict': 'accepted',
        'actor': 'test-cockpit',
        'decided_at': '2026-05-23T08:00:00+00:00',
        'note': 'looks safe to suppress',
        'followup': {
            'action': 'deprioritize_unit',
            'params': {'reason': 'cold'},
            'applied_state': {'is_deprioritized': True},
            'prior_state': {'is_deprioritized': False},
            'reversible': True,
        },
    }
    ok = await svc.set_status(
        proposal.id,
        'resolved',
        vault_id=vault.id,
        actor='test-cockpit',
        resolution=resolution,
    )
    assert ok is True

    async with metastore.session() as s:
        row = (
            (
                await s.execute(
                    text(
                        'SELECT status, evidence, resolved_by '
                        'FROM maintenance_proposals WHERE id = :id'
                    ),
                    {'id': str(proposal.id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert str(row['status']).lower().endswith('resolved')
    assert row['resolved_by'] == 'test-cockpit'
    evidence = row['evidence']
    assert evidence['mw_score'] == 0.1  # existing keys preserved
    persisted = evidence['resolution']
    assert persisted['verdict'] == 'accepted'
    assert persisted['note'] == 'looks safe to suppress'
    assert persisted['followup']['action'] == 'deprioritize_unit'
    assert persisted['followup']['prior_state'] == {'is_deprioritized': False}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_status_without_resolution_leaves_evidence_untouched(
    session: AsyncSession,
    metastore,
):
    """When ``resolution=None`` the evidence column is not rewritten."""
    vault = Vault(name=f'cockpit-no-res-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    original_evidence = {'why': 'no resolution intended', 'extra': [1, 2, 3]}
    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.GOVERNANCE,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='sensitive_unreviewed_unit',
        evidence=original_evidence,
        suggested_action='review',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    ok = await svc.set_status(
        proposal.id,
        'dismissed',
        vault_id=vault.id,
        actor='test-cockpit',
    )
    assert ok is True

    async with metastore.session() as s:
        row = (
            (
                await s.execute(
                    text('SELECT evidence FROM maintenance_proposals WHERE id = :id'),
                    {'id': str(proposal.id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row['evidence'] == original_evidence


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_status_resolution_preserves_existing_keys(
    session: AsyncSession,
    metastore,
):
    """``jsonb_set`` updates only the ``resolution`` key; siblings persist."""
    vault = Vault(name=f'cockpit-keep-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='cold_low_mw_unit',
        evidence={'mw_score': 0.05, 'tags': ['a', 'b'], 'nested': {'k': 1}},
        suggested_action='deprio',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    await svc.set_status(
        proposal.id,
        'resolved',
        vault_id=vault.id,
        actor='test-cockpit',
        resolution={'verdict': 'accepted', 'actor': 'test-cockpit'},
    )

    async with metastore.session() as s:
        row = (
            (
                await s.execute(
                    text('SELECT evidence FROM maintenance_proposals WHERE id = :id'),
                    {'id': str(proposal.id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    evidence = row['evidence']
    assert evidence['mw_score'] == 0.05
    assert evidence['tags'] == ['a', 'b']
    assert evidence['nested'] == {'k': 1}
    assert evidence['resolution']['verdict'] == 'accepted'
