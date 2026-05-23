"""Integration tests for ``LintLearningService.refresh_telemetry``.

Seeds resolved proposals across two vaults with a mix of verdicts, runs
the rollup, and asserts the resulting ``lint_rule_telemetry`` rows
match. Pins the SQL aggregate against real Postgres so JSONB-shape /
asyncpg behaviour drift surfaces in CI rather than at first deploy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

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
from memex_core.services.lint_learning import LintLearningService


def _resolution_followup(action: str) -> dict[str, object]:
    """Build the evidence.resolution block the cockpit writes on resolve."""
    return {
        'verdict': 'accepted',
        'actor': 'test',
        'decided_at': datetime.now(timezone.utc).isoformat(),
        'followup': {
            'action': action,
            'params': {},
            'applied_at': datetime.now(timezone.utc).isoformat(),
            'applied_state': {},
            'prior_state': {},
            'reversible': True,
        },
    }


async def _seed(
    session: AsyncSession,
    *,
    rule_name: str,
    vault_id: UUID,
    status: LintStatus,
    evidence: dict[str, object] | None = None,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
    surprise_score: float | None = None,
) -> UUID:
    payload: dict[str, object] = {**(evidence or {})}
    if surprise_score is not None:
        payload['surprise_score'] = surprise_score
    proposal = MaintenanceProposal(
        vault_id=vault_id,
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        target_id=str(uuid4()),
        rule_name=rule_name,
        evidence=payload,
        suggested_action='do something',
        status=status,
        source=LintSource.RULE,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    # Set timestamps explicitly so the rollup window picks them up.
    if created_at is not None or resolved_at is not None:
        await session.execute(
            text(
                'UPDATE maintenance_proposals '
                'SET created_at = COALESCE(:created_at, created_at), '
                '    resolved_at = COALESCE(:resolved_at, resolved_at) '
                'WHERE id = :id'
            ),
            {
                'id': str(proposal.id),
                'created_at': created_at,
                'resolved_at': resolved_at,
            },
        )
        await session.commit()
    return proposal.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_telemetry_aggregates_per_rule_and_vault(
    session: AsyncSession,
    metastore,
) -> None:
    """Mix of verdicts on one vault rolls up into the expected counters."""
    vault = Vault(name=f'lint-learn-vault-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    now = datetime.now(timezone.utc)
    inside_window = now - timedelta(days=5)

    # 3 accepts on cold_low_mw_unit
    for _ in range(3):
        await _seed(
            session,
            rule_name='cold_low_mw_unit',
            vault_id=vault.id,
            status=LintStatus.RESOLVED,
            evidence={'resolution': _resolution_followup('deprioritize_unit')},
            created_at=inside_window,
            resolved_at=inside_window + timedelta(minutes=2),
            surprise_score=0.6,
        )
    # 1 no_op on cold_low_mw_unit
    await _seed(
        session,
        rule_name='cold_low_mw_unit',
        vault_id=vault.id,
        status=LintStatus.RESOLVED,
        evidence={'resolution': _resolution_followup('no_op')},
        created_at=inside_window,
        resolved_at=inside_window + timedelta(minutes=5),
        surprise_score=0.65,
    )
    # 2 dismisses on cold_low_mw_unit
    for _ in range(2):
        await _seed(
            session,
            rule_name='cold_low_mw_unit',
            vault_id=vault.id,
            status=LintStatus.DISMISSED,
            evidence={'surprise_score': 0.7},
            created_at=inside_window,
            resolved_at=inside_window + timedelta(minutes=10),
        )
    # 1 legacy resolve (no resolution block) on llm_schema_drift
    await _seed(
        session,
        rule_name='llm_schema_drift',
        vault_id=vault.id,
        status=LintStatus.RESOLVED,
        evidence={'surprise_score': 0.8},
        created_at=inside_window,
        resolved_at=inside_window + timedelta(minutes=20),
    )

    svc = LintLearningService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    result = await svc.refresh_telemetry(vault_id=vault.id, window_days=30, now=now)

    assert result.proposals_aggregated == 7
    assert result.rules_seen == 2  # cold_low_mw_unit, llm_schema_drift
    # Two scopes (vault + global) × two rules = 4 rows written.
    assert result.rows_written == 4

    # Read back the vault-scoped cold_low_mw_unit row.
    rows = await svc.get_telemetry(rule_name='cold_low_mw_unit', vault_id=vault.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.accept_count == 3
    assert row.no_op_count == 1
    assert row.dismiss_count == 2
    assert row.legacy_count == 0
    assert row.accept_rate is not None
    assert abs(row.accept_rate - 3 / 6) < 1e-9
    assert row.median_surprise is not None
    assert abs(row.median_surprise - 0.6) < 0.1
    assert row.median_time_to_resolve_seconds is not None

    # Read back the legacy-only row for llm_schema_drift.
    legacy_rows = await svc.get_telemetry(rule_name='llm_schema_drift', vault_id=vault.id)
    assert len(legacy_rows) == 1
    assert legacy_rows[0].accept_count == 0
    assert legacy_rows[0].legacy_count == 1
    assert legacy_rows[0].accept_rate is None  # zero labelled denominator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_is_idempotent(
    session: AsyncSession,
    metastore,
) -> None:
    """Running the refresh twice produces the same rollup contents."""
    vault = Vault(name=f'lint-learn-idemp-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    now = datetime.now(timezone.utc)
    inside_window = now - timedelta(days=3)
    await _seed(
        session,
        rule_name='claim_too_aggressive',
        vault_id=vault.id,
        status=LintStatus.RESOLVED,
        evidence={'resolution': _resolution_followup('no_op')},
        created_at=inside_window,
        resolved_at=inside_window + timedelta(minutes=1),
    )

    svc = LintLearningService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    first = await svc.refresh_telemetry(vault_id=vault.id, window_days=30, now=now)
    second = await svc.refresh_telemetry(vault_id=vault.id, window_days=30, now=now)
    assert first.rows_written == second.rows_written
    assert first.rules_seen == second.rules_seen

    rows = await svc.get_telemetry(vault_id=vault.id, include_global=False)
    rows_by_rule = {r.rule_name: r for r in rows}
    assert 'claim_too_aggressive' in rows_by_rule
    assert rows_by_rule['claim_too_aggressive'].no_op_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_global_rollup_aggregates_across_vaults(
    session: AsyncSession,
    metastore,
) -> None:
    """The vault_id-IS-NULL row sums verdicts from every vault."""
    vault_a = Vault(name=f'lint-learn-global-a-{uuid4().hex[:8]}')
    vault_b = Vault(name=f'lint-learn-global-b-{uuid4().hex[:8]}')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()
    await session.refresh(vault_a)
    await session.refresh(vault_b)

    now = datetime.now(timezone.utc)
    inside_window = now - timedelta(days=1)
    await _seed(
        session,
        rule_name='llm_semantic_contradiction',
        vault_id=vault_a.id,
        status=LintStatus.RESOLVED,
        evidence={'resolution': _resolution_followup('deprioritize_unit')},
        created_at=inside_window,
        resolved_at=inside_window + timedelta(minutes=1),
    )
    await _seed(
        session,
        rule_name='llm_semantic_contradiction',
        vault_id=vault_b.id,
        status=LintStatus.DISMISSED,
        evidence={'surprise_score': 0.5},
        created_at=inside_window,
        resolved_at=inside_window + timedelta(minutes=1),
    )

    svc = LintLearningService(metastore=metastore, filestore=MagicMock(), config=MagicMock())
    # Refresh from vault_a's perspective; global row should still aggregate
    # both vaults because the rollup scan is window-bounded, not vault-bounded.
    await svc.refresh_telemetry(vault_id=vault_a.id, window_days=30, now=now)

    global_rows = await svc.get_telemetry(rule_name='llm_semantic_contradiction', vault_id=None)
    assert len(global_rows) == 1
    g = global_rows[0]
    assert g.vault_id is None
    assert g.accept_count == 1  # vault_a's accept
    assert g.dismiss_count == 1  # vault_b's dismiss
