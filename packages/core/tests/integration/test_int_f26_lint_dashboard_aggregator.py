"""F26 — Lint dashboard aggregator integration test.

Seeds MaintenanceProposal rows and asserts the
``aggregate_lint_findings`` pivot + ``pending_by_type`` slice + top-5
ordering match the seeded shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.diagnostics.lint_dashboard import (
    aggregate_lint_findings,
    pending_by_type,
)
from memex_core.memory.sql_models import (
    LintSource,
    LintStatus,
    LintType,
    MaintenanceProposal,
    Vault,
)


def _proposal(
    vault_id,
    *,
    lint_type: LintType,
    status: LintStatus,
    source: LintSource = LintSource.RULE,
    target_id: str | None = None,
    rule_name: str = 'rule_a',
    created_at: datetime | None = None,
) -> MaintenanceProposal:
    return MaintenanceProposal(
        vault_id=vault_id,
        lint_type=lint_type,
        target_type='memory_unit',
        target_id=target_id or str(uuid4()),
        rule_name=rule_name,
        evidence={'why': 'test'},
        suggested_action='look at it',
        status=status,
        source=source,
        created_at=created_at,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pivot_counts_by_type_status_source(
    session: AsyncSession,
    metastore,
):
    """Seed a fixed mix of proposals; assert pivot rows match expected counts."""
    vault = Vault(name=f'F26-pivot-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    rows = [
        # 3 structural/pending/rule
        _proposal(vault.id, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
        _proposal(vault.id, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
        _proposal(vault.id, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
        # 2 quality/pending/llm
        _proposal(
            vault.id,
            lint_type=LintType.QUALITY,
            status=LintStatus.PENDING,
            source=LintSource.LLM,
        ),
        _proposal(
            vault.id,
            lint_type=LintType.QUALITY,
            status=LintStatus.PENDING,
            source=LintSource.LLM,
        ),
        # 1 quality/resolved/rule
        _proposal(vault.id, lint_type=LintType.QUALITY, status=LintStatus.RESOLVED),
        # 1 governance/dismissed/rule
        _proposal(vault.id, lint_type=LintType.GOVERNANCE, status=LintStatus.DISMISSED),
    ]
    session.add_all(rows)
    await session.commit()

    result = await aggregate_lint_findings(metastore, vault.id)

    assert result['vault_id'] == str(vault.id)
    counts = {
        (r['lint_type'], r['status'], r['source']): r['count']
        for r in result['counts_by_type_status_source']
    }
    assert counts == {
        ('structural', 'pending', 'rule'): 3,
        ('quality', 'pending', 'llm'): 2,
        ('quality', 'resolved', 'rule'): 1,
        ('governance', 'dismissed', 'rule'): 1,
    }

    # pending_by_type: only pending rows, summed by lint_type.
    assert result['pending_by_type'] == {'structural': 3, 'quality': 2}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_top_5_pending_ordered_desc_by_created_at(
    session: AsyncSession,
    metastore,
):
    """Seed 8 pending proposals with varying created_at; expect 5 most-recent."""
    vault = Vault(name=f'F26-top5-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    base = datetime.now(timezone.utc)
    seeded = []
    for i in range(8):
        # i=0 oldest, i=7 newest
        p = _proposal(
            vault.id,
            lint_type=LintType.STRUCTURAL,
            status=LintStatus.PENDING,
            target_id=f'unit-{i:02d}',
            rule_name=f'rule-{i:02d}',
            created_at=base - timedelta(hours=8 - i),
        )
        seeded.append(p)
    session.add_all(seeded)
    await session.commit()

    result = await aggregate_lint_findings(metastore, vault.id)
    top5 = result['top_5_pending']
    assert len(top5) == 5
    # Newest first → target_ids unit-07 .. unit-03
    assert [r['target_id'] for r in top5] == [f'unit-{i:02d}' for i in (7, 6, 5, 4, 3)]
    for row in top5:
        assert row['lint_type'] == 'structural'
        assert row['status'] == 'pending'
        assert row['source'] == 'rule'
        assert row['created_at'] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_vault_returns_empty_pivots(
    session: AsyncSession,
    metastore,
):
    """A vault with no MaintenanceProposal rows yields empty pivots, not an error."""
    vault = Vault(name=f'F26-empty-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    result = await aggregate_lint_findings(metastore, vault.id)
    assert result['vault_id'] == str(vault.id)
    assert result['counts_by_type_status_source'] == []
    assert result['pending_by_type'] == {}
    assert result['top_5_pending'] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_global_findings_excluded_from_per_vault_view(
    session: AsyncSession,
    metastore,
):
    """vault_id IS NULL findings (Tier B globals) must NOT surface in a per-vault dashboard.

    Locks the contract that F26 = per-vault diagnostics; global findings have
    their own surface (F6 /lint/status?scope=global).
    """
    vault = Vault(name=f'F26-vault-scope-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    # 1 vault-scoped pending row + 1 global (vault_id=NULL) pending row.
    session.add_all(
        [
            _proposal(vault.id, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
            _proposal(None, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
        ]
    )
    await session.commit()

    result = await aggregate_lint_findings(metastore, vault.id)
    # Only the vault-scoped row counts.
    assert result['pending_by_type'] == {'structural': 1}
    assert sum(r['count'] for r in result['counts_by_type_status_source']) == 1


# ---------------------------------------------------------------------------
# pending_by_type slice (used by compute_diagnostics_summary)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_by_type_slice_matches_aggregator(
    session: AsyncSession,
    metastore,
):
    """The shortcut slice used by summary.py must agree with the full aggregator."""
    vault = Vault(name=f'F26-slice-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    session.add_all(
        [
            _proposal(vault.id, lint_type=LintType.STRUCTURAL, status=LintStatus.PENDING),
            _proposal(vault.id, lint_type=LintType.QUALITY, status=LintStatus.PENDING),
            _proposal(vault.id, lint_type=LintType.QUALITY, status=LintStatus.RESOLVED),
        ]
    )
    await session.commit()

    full = await aggregate_lint_findings(metastore, vault.id)
    slice_only = await pending_by_type(metastore, vault.id)

    assert slice_only == full['pending_by_type']
    assert slice_only == {'structural': 1, 'quality': 1}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_deferred_rows_excluded_from_dashboard(
    session: AsyncSession,
    metastore,
):
    """Internal ``llm_deferred`` bookkeeping must not inflate the dashboard
    pivot / pending_by_type / top-5 — it is hidden from the findings list and
    pending count, so the dashboard must agree (no inconsistent badge)."""
    vault = Vault(name=f'F26-deferred-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    session.add_all(
        [
            _proposal(
                vault.id,
                lint_type=LintType.QUALITY,
                status=LintStatus.PENDING,
                rule_name='cold_low_mw_unit',
            ),
            _proposal(
                vault.id,
                lint_type=LintType.QUALITY,
                status=LintStatus.PENDING,
                rule_name='llm_deferred',
            ),
        ]
    )
    await session.commit()

    agg = await aggregate_lint_findings(metastore, vault.id)
    assert agg['pending_by_type'] == {'quality': 1}
    assert await pending_by_type(metastore, vault.id) == {'quality': 1}
    assert all(r['rule_name'] != 'llm_deferred' for r in agg['top_5_pending'])
    # The deferred row is also absent from the type/status/source pivot counts.
    quality_pending = [
        c
        for c in agg['counts_by_type_status_source']
        if c['lint_type'] == 'quality' and c['status'] == 'pending'
    ]
    assert sum(c['count'] for c in quality_pending) == 1
