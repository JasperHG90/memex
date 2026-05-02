"""F6 — Lint endpoints (maintenance ledger).

Routes:
- GET    /api/v1/lint/status                          — pending counts (global + per-vault)
- GET    /api/v1/lint/findings                        — list findings with optional filters
- POST   /api/v1/lint/findings/{finding_id}/dismiss   — flip status to 'dismissed'
- POST   /api/v1/lint/findings/{finding_id}/resolve   — flip status to 'resolved'

The maintenance ledger is read-only from the agent surface (F8 ships the
MCP tool); these endpoints back the human-facing CLI (``memex lint ...``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.lint')

router = APIRouter(prefix='/api/v1/lint')


@router.get('/status', dependencies=[Depends(require_read)])
async def lint_status(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(None, description='Scope to one vault.'),
    scope: str = Query('all', pattern='^(vault|global|all)$'),
) -> dict[str, Any]:
    """Pending finding counts.

    - ``scope=all`` (default): total across every vault and global.
    - ``scope=vault``: count for ``vault_id``; required when scope=vault.
    - ``scope=global``: count for findings with vault_id NULL.
    """
    try:
        if scope == 'global':
            count = await api.lint.count_pending(None)
            return {'scope': 'global', 'pending': count}
        if scope == 'all':
            async with api.metastore.session() as session:
                row = await session.execute(
                    text("SELECT count(*) FROM maintenance_proposals WHERE status = 'pending'")
                )
                return {'scope': 'all', 'pending': int(row.scalar() or 0)}
        if vault_id is None:
            raise HTTPException(
                status_code=400,
                detail='vault_id is required when scope=vault',
            )
        count = await api.lint.count_pending(vault_id)
        return {'scope': 'vault', 'vault_id': str(vault_id), 'pending': count}
    except HTTPException:
        raise
    except Exception as e:
        raise _handle_error(e, 'Failed to read lint status')


@router.get('/findings', dependencies=[Depends(require_read)])
async def lint_findings(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(None, description='Scope to one vault.'),
    lint_type: str | None = Query(None, pattern='^(structural|quality|governance|schema)$'),
    status: str = Query('pending', pattern='^(pending|resolved|dismissed)$'),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List maintenance findings with optional filters."""
    try:
        # `clauses` only contains hard-coded predicate fragments (no user input);
        # the column/operator strings are trusted constants and all values are
        # bound via :named parameters. SQLAlchemy Core constructs would be more
        # idiomatic but offer no additional safety here.
        clauses = ['status = :status']
        params: dict[str, Any] = {'status': status}
        if vault_id is not None:
            clauses.append('vault_id = :vault_id')
            params['vault_id'] = str(vault_id)
        if lint_type is not None:
            clauses.append('lint_type = :lint_type')
            params['lint_type'] = lint_type

        where = ' AND '.join(clauses)
        params['limit'] = limit
        params['offset'] = offset

        async with api.metastore.session() as session:
            result = await session.execute(
                text(
                    'SELECT id::text, vault_id::text, lint_type, target_type, target_id, '
                    'rule_name, evidence, suggested_action, status, source, '
                    'created_at, resolved_at '
                    f'FROM maintenance_proposals WHERE {where} '  # noqa: S608
                    'ORDER BY created_at DESC '
                    'LIMIT :limit OFFSET :offset'
                ),
                params,
            )
            rows = [dict(row) for row in result.mappings().all()]
        return {'count': len(rows), 'findings': rows}
    except Exception as e:
        raise _handle_error(e, 'Failed to list lint findings')


@router.post('/findings/{finding_id}/dismiss', dependencies=[Depends(require_write)])
async def lint_dismiss(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> dict[str, Any]:
    """Flip a pending finding to ``dismissed``. Idempotent."""
    try:
        ok = await api.lint.set_status(finding_id, 'dismissed')
    except Exception as e:
        raise _handle_error(e, 'Failed to dismiss finding')
    if not ok:
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return {'finding_id': str(finding_id), 'status': 'dismissed'}


@router.post('/findings/{finding_id}/resolve', dependencies=[Depends(require_write)])
async def lint_resolve(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> dict[str, Any]:
    """Flip a pending finding to ``resolved``. Idempotent."""
    try:
        ok = await api.lint.set_status(finding_id, 'resolved')
    except Exception as e:
        raise _handle_error(e, 'Failed to resolve finding')
    if not ok:
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return {'finding_id': str(finding_id), 'status': 'resolved'}
