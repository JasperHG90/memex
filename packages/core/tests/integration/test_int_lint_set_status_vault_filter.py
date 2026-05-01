"""Service-layer defense-in-depth for ``LintService.set_status`` (HIGH-4 sub).

The route ``/api/v1/lint/findings/{id}/{dismiss,resolve}`` already gates the
caller against the finding's vault before calling ``set_status``. The service
also takes a ``vault_id`` parameter and adds it to the UPDATE WHERE clause —
so even if the route is somehow bypassed (e.g. an in-process caller that
forgets to pass the auth context) a mismatched ``(finding_id, vault_id)``
pair leaves the row untouched.

This integration test asserts that contract against a real Postgres:

  - matching vault_id  → row updated, returns True
  - mismatched vault_id → row UNCHANGED, returns False (0 rowcount sentinel)
  - vault_id=None       → row updated unconditionally (legacy callers)
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
async def test_set_status_with_mismatched_vault_leaves_row_unchanged(
    session: AsyncSession,
    metastore,
):
    """Mismatched (finding_id, vault_id) → 0 rows affected, status stays pending."""
    vault_a = Vault(name=f'F8-vault-a-{uuid4().hex[:8]}')
    vault_b = Vault(name=f'F8-vault-b-{uuid4().hex[:8]}')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()
    await session.refresh(vault_a)
    await session.refresh(vault_b)

    proposal = MaintenanceProposal(
        vault_id=vault_a.id,
        lint_type=LintType.STRUCTURAL,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='rule_a',
        evidence={'why': 'test'},
        suggested_action='look at it',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    finding_id = proposal.id

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())

    ok = await svc.set_status(finding_id, 'dismissed', vault_id=vault_b.id)
    assert ok is False, 'Mismatched vault must report 0 rows affected'

    async with metastore.session() as s:
        row = await s.execute(
            text('SELECT status FROM maintenance_proposals WHERE id = :id'),
            {'id': str(finding_id)},
        )
        status_value = row.scalar()
    assert str(status_value).lower().endswith('pending'), (
        f'Row status must remain pending after vault-mismatched set_status; got {status_value!r}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_status_with_matching_vault_updates_row(
    session: AsyncSession,
    metastore,
):
    """Matching (finding_id, vault_id) → 1 row affected, status flipped."""
    vault = Vault(name=f'F8-vault-match-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='rule_b',
        evidence={'why': 'test'},
        suggested_action='look at it',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    finding_id = proposal.id

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())

    ok = await svc.set_status(finding_id, 'resolved', vault_id=vault.id)
    assert ok is True

    async with metastore.session() as s:
        row = await s.execute(
            text('SELECT status FROM maintenance_proposals WHERE id = :id'),
            {'id': str(finding_id)},
        )
        status_value = str(row.scalar()).lower()
    assert status_value.endswith('resolved'), f'Expected resolved, got {status_value!r}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_status_without_vault_id_preserves_legacy_behavior(
    session: AsyncSession,
    metastore,
):
    """``vault_id=None`` (legacy in-process callers) → row updated regardless."""
    vault = Vault(name=f'F8-vault-legacy-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.GOVERNANCE,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='rule_c',
        evidence={'why': 'test'},
        suggested_action='look at it',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    finding_id = proposal.id

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())

    ok = await svc.set_status(finding_id, 'dismissed')
    assert ok is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_finding_vault_id_returns_tuple(
    session: AsyncSession,
    metastore,
):
    """``get_finding_vault_id`` returns ``(found, vault_id)`` so the route
    can distinguish 404 from a global finding (vault_id NULL)."""
    vault = Vault(name=f'F8-vault-lookup-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    proposal = MaintenanceProposal(
        vault_id=vault.id,
        lint_type=LintType.STRUCTURAL,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name='rule_lookup',
        evidence={'why': 'test'},
        suggested_action='look at it',
        status=LintStatus.PENDING,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    svc = LintService(metastore=metastore, filestore=MagicMock(), config=MagicMock())

    found, vault_id = await svc.get_finding_vault_id(proposal.id)
    assert found is True
    assert vault_id == vault.id

    found, vault_id = await svc.get_finding_vault_id(uuid4())
    assert found is False
    assert vault_id is None
