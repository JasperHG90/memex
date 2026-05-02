"""F26 — Lint dashboard aggregator (RFC-009 §72).

Reads ``maintenance_proposals`` (F6) and pivots by ``(lint_type, status, source)``
plus surfaces the top-5 most-recent pending findings. Used by:
- ``GET /api/v1/diagnostics/lint/{vault_id}`` (full dashboard)
- ``compute_diagnostics_summary`` (just the ``pending_by_type`` slice; replaces
  the F32-core placeholder ``lint_pending_by_type: {}``)

This is intentionally a thin aggregator — F6 owns the rule engine and the
``/lint/findings`` row-listing surface; F8 owns the agent-paginated ``/lint/flags``
surface; F26 only adds the operator/observability *pivot* view.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import desc, func, select

from memex_core.memory._lint_utils import enum_value as _enum_value
from memex_core.memory.sql_models import LintStatus, MaintenanceProposal

if TYPE_CHECKING:
    from memex_core.storage.metastore import MetaStore

logger = logging.getLogger('memex.core.diagnostics.lint_dashboard')


async def aggregate_lint_findings(
    metastore: 'MetaStore',
    vault_id: UUID,
) -> dict[str, Any]:
    """Aggregate MaintenanceProposal rows for a vault into a dashboard payload.

    Returns:
        ``{
            'vault_id': str(vault_id),
            'counts_by_type_status_source': [
                {'lint_type': ..., 'status': ..., 'source': ..., 'count': N},
                ...
            ],
            'pending_by_type': {<lint_type>: <count>, ...},   # status='pending' only
            'top_5_pending': [<row dict>, ...],
        }``

    Two SQL round-trips: one ``GROUP BY`` for the pivot, one ordered LIMIT for
    the top-5. Both filter by ``vault_id`` (NOT NULL semantics — global findings
    with vault_id IS NULL are not surfaced through this per-vault view; that
    matches F32's per-vault diagnostics summary contract).
    """
    # Use explicit `.label()` on each column so rows are accessed by name via
    # SQLAlchemy's Row attribute API, rather than positional unpacking. Keeps
    # the SELECT and the post-fetch loop in sync if the column order ever
    # changes (matches heatmap.py shape; .scalars() is unnecessary here —
    # we don't need ORM instance hydration).
    pivot_stmt = (
        select(
            MaintenanceProposal.lint_type.label('lint_type'),
            MaintenanceProposal.status.label('status'),
            MaintenanceProposal.source.label('source'),
            func.count().label('count'),
        )
        .where(MaintenanceProposal.vault_id == vault_id)
        .group_by(
            MaintenanceProposal.lint_type,
            MaintenanceProposal.status,
            MaintenanceProposal.source,
        )
    )
    top5_stmt = (
        select(
            MaintenanceProposal.id.label('id'),
            MaintenanceProposal.lint_type.label('lint_type'),
            MaintenanceProposal.target_type.label('target_type'),
            MaintenanceProposal.target_id.label('target_id'),
            MaintenanceProposal.rule_name.label('rule_name'),
            MaintenanceProposal.suggested_action.label('suggested_action'),
            MaintenanceProposal.status.label('status'),
            MaintenanceProposal.source.label('source'),
            MaintenanceProposal.created_at.label('created_at'),
        )
        .where(
            MaintenanceProposal.vault_id == vault_id,
            MaintenanceProposal.status == LintStatus.PENDING,
        )
        .order_by(desc(MaintenanceProposal.created_at))
        .limit(5)
    )

    async with metastore.session() as sess:
        pivot_rows = (await sess.exec(pivot_stmt)).all()
        top5_rows = (await sess.exec(top5_stmt)).all()

    counts: list[dict[str, Any]] = []
    pending_by_type: dict[str, int] = {}
    for row in pivot_rows:
        lt = _enum_value(row.lint_type)
        st = _enum_value(row.status)
        src = _enum_value(row.source)
        c = int(row.count)
        counts.append({'lint_type': lt, 'status': st, 'source': src, 'count': c})
        if st == 'pending':
            pending_by_type[lt] = pending_by_type.get(lt, 0) + c

    top5: list[dict[str, Any]] = [
        {
            'id': str(row.id),
            'lint_type': _enum_value(row.lint_type),
            'target_type': row.target_type,
            'target_id': row.target_id,
            'rule_name': row.rule_name,
            'suggested_action': row.suggested_action,
            'status': _enum_value(row.status),
            'source': _enum_value(row.source),
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }
        for row in top5_rows
    ]

    return {
        'vault_id': str(vault_id),
        'counts_by_type_status_source': counts,
        'pending_by_type': pending_by_type,
        'top_5_pending': top5,
    }


async def pending_by_type(
    metastore: 'MetaStore',
    vault_id: UUID,
) -> dict[str, int]:
    """Just the ``pending_by_type`` slice — used by compute_diagnostics_summary
    to populate the ``lint_pending_by_type`` field without paying for the
    top-5 SELECT.
    """
    stmt = (
        select(
            MaintenanceProposal.lint_type.label('lint_type'),
            func.count().label('count'),
        )
        .where(
            MaintenanceProposal.vault_id == vault_id,
            MaintenanceProposal.status == LintStatus.PENDING,
        )
        .group_by(MaintenanceProposal.lint_type)
    )
    async with metastore.session() as sess:
        rows = (await sess.exec(stmt)).all()
    return {_enum_value(row.lint_type): int(row.count) for row in rows}
