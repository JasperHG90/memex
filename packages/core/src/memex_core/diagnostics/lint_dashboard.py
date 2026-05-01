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
    # Column-positional select (not select(MaintenanceProposal)) matches heatmap.py —
    # SQLModel's session.exec(select(Model)) returns Row tuples wrapping the model
    # under async paths; switch to .scalars() only if relationship traversal is needed.
    pivot_stmt = (
        select(
            MaintenanceProposal.lint_type,
            MaintenanceProposal.status,
            MaintenanceProposal.source,
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
            MaintenanceProposal.id,
            MaintenanceProposal.lint_type,
            MaintenanceProposal.target_type,
            MaintenanceProposal.target_id,
            MaintenanceProposal.rule_name,
            MaintenanceProposal.suggested_action,
            MaintenanceProposal.status,
            MaintenanceProposal.source,
            MaintenanceProposal.created_at,
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
    for lint_type, status, source, count in pivot_rows:
        lt = _enum_value(lint_type)
        st = _enum_value(status)
        src = _enum_value(source)
        c = int(count)
        counts.append({'lint_type': lt, 'status': st, 'source': src, 'count': c})
        if st == 'pending':
            pending_by_type[lt] = pending_by_type.get(lt, 0) + c

    top5: list[dict[str, Any]] = [
        {
            'id': str(rid),
            'lint_type': _enum_value(rlt),
            'target_type': rtt,
            'target_id': rti,
            'rule_name': rn,
            'suggested_action': ra,
            'status': _enum_value(rs),
            'source': _enum_value(rsrc),
            'created_at': rca.isoformat() if rca else None,
        }
        for rid, rlt, rtt, rti, rn, ra, rs, rsrc, rca in top5_rows
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
        select(MaintenanceProposal.lint_type, func.count().label('count'))
        .where(
            MaintenanceProposal.vault_id == vault_id,
            MaintenanceProposal.status == LintStatus.PENDING,
        )
        .group_by(MaintenanceProposal.lint_type)
    )
    async with metastore.session() as sess:
        rows = (await sess.exec(stmt)).all()
    return {_enum_value(lint_type): int(count) for lint_type, count in rows}


def _enum_value(v: Any) -> str:
    """Normalise SQLModel enum field reads to plain strings.

    F6 stores lint_type/status/source as Text columns but the SQLModel
    declarations type them as enums (LintType/LintStatus/LintSource). Reads
    can come back as either the enum instance or the raw string depending
    on driver path; this collapses both to the string form the dashboard
    JSON expects.
    """
    return v.value if hasattr(v, 'value') else str(v)
