"""Lint endpoints (maintenance ledger).

Routes:
- GET    /api/v1/lint/status                          — pending counts (global + per-vault)
- GET    /api/v1/lint/findings                        — list findings (CLI surface, offset paged)
- GET    /api/v1/lint/flags                           — cursor-paginated agent surface
- POST   /api/v1/lint/findings/{finding_id}/dismiss   — flip status to 'dismissed'
- POST   /api/v1/lint/findings/{finding_id}/resolve   — flip status to 'resolved'
- POST   /api/v1/lint/findings/{finding_id}/apply     — apply a winner-proposal action
- POST   /api/v1/lint/findings/{finding_id}/reverse   — reverse a previously applied winner-proposal

The ``findings`` endpoint backs ``memex lint findings`` (CLI). The
``flags`` endpoint is the agent surface — shape-stable returns and
opaque cursor pagination, mirrored by ``memex_get_lint_flags`` MCP.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex_common.config import Permission
from memex_core.api import MemexAPI
from memex_core.context import get_actor
from memex_core.server.auth import (
    AuthContext,
    check_vault_access,
    get_auth_context,
    require_read,
    require_write,
)
from memex_core.server.common import _handle_error, get_api
from memex_core.services.lint import LintSubsystemNotInitializedError

logger = logging.getLogger('memex.core.server.lint')

router = APIRouter(prefix='/api/v1/lint')


@router.get('/status', dependencies=[Depends(require_read)])
async def lint_status(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(None, description='Scope to one vault.'),
    scope: str = Query('all', pattern='^(vault|global|all)$'),
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
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
        await check_vault_access(auth, [vault_id], api, permission=Permission.READ)
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
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """List maintenance findings with optional filters."""
    try:
        if vault_id is not None:
            await check_vault_access(auth, [vault_id], api, permission=Permission.READ)
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
                    'created_at, resolved_at, resolved_by '
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


def _audit_actor() -> str:
    """Return the audit-trail actor label for the current request.

    Reads from :func:`memex_core.context.get_actor`, which is set by
    ``auth_middleware`` for authenticated requests (shape:
    ``f'{key_name} ({key_prefix})'``). When auth is disabled, the
    contextvar default is ``'anonymous'``; we promote that to
    ``'system:auth-disabled'`` so the apply / reverse endpoints stay
    reachable in dev/test/CI, the audit row still identifies the call
    path, and the value cannot be confused with a real principal whose
    key is literally named "system".
    """
    actor = get_actor()
    if not actor or actor == 'anonymous':
        return 'system:auth-disabled'
    return actor


async def _gate_finding_for_write(
    finding_id: UUID,
    api: MemexAPI,
    auth: AuthContext | None,
) -> UUID | None:
    """Defense-in-depth vault-scope helper for finding write endpoints.

    Looks up the finding's vault_id, then gates the auth context against it.
    Returns the resolved vault_id (or ``None`` for global findings) so the
    caller can pass it through to ``LintService.set_status`` for SQL-level
    constraint as well (cross-vault mutation rejected: route + service layered checks).

    Does NOT check the finding's status — the service layer raises a 409
    with the correct status-transition semantics (pending vs. resolved
    constraints differ by endpoint).

    Raises:
      - 404 if the finding does not exist.
      - 403 if the auth context cannot WRITE to the finding's vault.
    """
    found, finding_vault = await api.lint.get_finding_vault_id(finding_id)
    if not found:
        raise HTTPException(status_code=404, detail='Finding not found')
    if finding_vault is not None:
        await check_vault_access(auth, [finding_vault], api, permission=Permission.WRITE)
    return finding_vault


@router.post('/findings/{finding_id}/dismiss', dependencies=[Depends(require_write)])
async def lint_dismiss(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Flip a pending finding to ``dismissed``. Idempotent.

    Per vault-scoping invariant: looks up the finding's vault and
    gates the auth context BEFORE mutating, so a vault-A scoped key with a
    leaked vault-B finding_id cannot dismiss the vault-B row (cross-vault check).
    """
    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    try:
        ok = await api.lint.set_status(finding_id, 'dismissed', vault_id=finding_vault)
    except Exception as e:
        raise _handle_error(e, 'Failed to dismiss finding')
    if not ok:
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return {'finding_id': str(finding_id), 'status': 'dismissed'}


@router.post('/findings/{finding_id}/resolve', dependencies=[Depends(require_write)])
async def lint_resolve(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Flip a pending finding to ``resolved``. Idempotent.

    Per vault-scoping invariant: looks up the finding's vault and
    gates the auth context BEFORE mutating, so a vault-A scoped key with a
    leaked vault-B finding_id cannot resolve the vault-B row (cross-vault check).
    """
    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    try:
        ok = await api.lint.set_status(finding_id, 'resolved', vault_id=finding_vault)
    except Exception as e:
        raise _handle_error(e, 'Failed to resolve finding')
    if not ok:
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return {'finding_id': str(finding_id), 'status': 'resolved'}


@router.post('/findings/{finding_id}/apply', dependencies=[Depends(require_write)])
async def lint_apply(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Apply a winner-proposal finding's recorded action.

    Gates by the finding's vault (same path as resolve/dismiss). The action
    semantics — mark_loser_stale / supersede_loser_note /
    refine_not_contradict / inconclusive — are captured under
    ``evidence.action`` when the finding is emitted; this endpoint dispatches
    on that literal and records ``prior_state`` so the change is reversible.
    """
    from memex_core.services.contradiction_resolution import (
        ContradictionResolutionError,
        apply_winner_proposal,
    )

    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    actor = _audit_actor()
    try:
        return await apply_winner_proposal(api, finding_id, vault_id=finding_vault, actor=actor)
    except ContradictionResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        raise _handle_error(e, 'Failed to apply winner proposal')


@router.post('/findings/{finding_id}/reverse', dependencies=[Depends(require_write)])
async def lint_reverse(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Reverse a previously applied winner-proposal.

    Reads ``evidence.resolution.prior_state`` and atomically restores the
    affected rows. Writes a paired ``propose_contradiction_winner_reversal``
    audit row; the original resolved finding stays resolved so the unique
    partial index on pending findings remains valid.
    """
    from memex_core.services.contradiction_resolution import (
        ContradictionResolutionError,
        reverse_winner_proposal,
    )

    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    actor = _audit_actor()
    try:
        return await reverse_winner_proposal(api, finding_id, vault_id=finding_vault, actor=actor)
    except ContradictionResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        raise _handle_error(e, 'Failed to reverse winner proposal')


@router.post('/run/{vault_id}', dependencies=[Depends(require_write)])
async def lint_run(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Synchronously run the V1 lint rule registry for ``vault_id``.

    The eval suite framework needs deterministic on-demand triggering of
    the lint pass — the periodic scheduler at scheduler.py:129 fires every
    21600s (6h) by default, which is unworkable for tests. This endpoint
    exposes the same ``api.lint.run_rules`` entrypoint the scheduler uses,
    so behavior is identical (modulo the FSFM auto-deprioritize step
    which the scheduler does after lint — that's a separate concern).

    Idempotent at the storage layer: ``_INSERT_FINDING_SQL`` (lint.py:564)
    uses ``ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending' DO NOTHING``, so back-to-back calls don't
    duplicate findings — provided no reviewer dismissed/resolved a prior
    finding in between (in which case the partial-index filter no longer
    matches and the row gets re-inserted).
    """
    await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        summary = await api.lint.run_rules(vault_id)
    except LintSubsystemNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise _handle_error(e, 'lint run failed')
    return {
        'vault_id': str(vault_id),
        'total_findings': summary.total_findings,
        'rules': [
            {
                'name': r.rule_name,
                'lint_type': r.lint_type.value
                if hasattr(r.lint_type, 'value')
                else str(r.lint_type),
                'findings_emitted': r.findings_emitted,
                'duration_seconds': r.duration_seconds,
                'error': r.error,
            }
            for r in summary.rules
        ],
    }


@router.post('/llm/run/{vault_id}', dependencies=[Depends(require_write)])
async def lint_llm_run(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Synchronously run the LLM-gated lint pass for ``vault_id``.

    Mirrors the scheduler's ``periodic_lint_llm_task`` at scheduler.py:201
    — same checks list, same NLI loading sequence. NLI is eager-loaded at
    server startup (see server/__init__.py:151) when polarity.enabled is
    True, so the call is a cache-hit; if startup-load failed, it lazy-loads
    here and gets cached process-wide for subsequent calls.

    Returns 503 when ``lint_llm.enabled=False`` or ``cost_cap_per_24h=0``
    (the cap-zero gate that short-circuits the periodic task).
    """
    from memex_core.memory.lint_llm.checks import (
        make_propose_contradiction_winner_check,
        make_schema_drift_check,
        make_semantic_contradiction_check,
    )
    from memex_core.memory.lint_llm.polarity import (
        PolarityClassifier,
        PolarityRateLimiter,
    )
    from memex_core.memory.models import get_nli_model

    await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)

    settings = api.config.server.memory.lint_llm
    if not settings.enabled or settings.cost_cap_per_24h <= 0:
        raise HTTPException(status_code=503, detail='lint_llm disabled by config')

    polarity_classifier: PolarityClassifier | None = None
    if settings.polarity.enabled:
        try:
            nli_model = await get_nli_model(settings.polarity)
            if nli_model is not None:
                polarity_classifier = PolarityClassifier(
                    nli_model,
                    polarity_threshold=settings.polarity.polarity_threshold,
                    rate_limiter=PolarityRateLimiter(
                        max_per_vault_per_hour=settings.polarity.rate_limit_per_vault_per_hour,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — NLI absence is non-fatal
            logger.warning('NLI load failed; falling back to cosine-only gate: %s', exc)

    checks: list[tuple[str, Any]] = []
    if settings.checks.semantic_contradiction.enabled:
        checks.append(
            (
                'semantic_contradiction',
                make_semantic_contradiction_check(api.lm, k=settings.surprise_k),
            )
        )
    if settings.checks.schema_drift.enabled:
        checks.append(('schema_drift', make_schema_drift_check(api.lm, k=settings.surprise_k)))

    propose_winner_check: Any | None = None
    if settings.checks.propose_contradiction_winner.enabled:
        propose_winner_check = make_propose_contradiction_winner_check(
            api.lm,
            min_confidence=settings.propose_winner_min_confidence,
        )

    if not checks and propose_winner_check is None:
        return {
            'vault_id': str(vault_id),
            'summaries': [],
            'detail': 'no LLM lint checks enabled',
        }

    summaries: list[dict[str, Any]] = []
    for check_name, check in checks:
        try:
            s = await api.lint_llm.tick(
                vault_id,
                run_llm_check=check,
                polarity_classifier=(
                    polarity_classifier if check_name == 'semantic_contradiction' else None
                ),
            )
            summaries.append(
                {
                    'check': check_name,
                    'evaluated': s.candidates_evaluated,
                    'emitted': s.findings_emitted,
                    'deferred': s.deferred,
                    'deferred_processed': s.deferred_processed,
                }
            )
        except Exception as exc:
            logger.warning('lint_llm[%s] failed: %s', check_name, exc)
            summaries.append({'check': check_name, 'error': str(exc)})

    if propose_winner_check is not None:
        try:
            s = await api.lint_llm.tick_propose_winner(
                vault_id,
                run_llm_check=propose_winner_check,
            )
            summaries.append(
                {
                    'check': 'propose_contradiction_winner',
                    'evaluated': s.candidates_evaluated,
                    'emitted': s.findings_emitted,
                    'deferred': s.deferred,
                    'deferred_processed': s.deferred_processed,
                }
            )
        except Exception as exc:
            logger.warning('lint_llm[propose_contradiction_winner] failed: %s', exc)
            summaries.append({'check': 'propose_contradiction_winner', 'error': str(exc)})

    return {'vault_id': str(vault_id), 'summaries': summaries}


@router.get('/flags', dependencies=[Depends(require_read)])
async def lint_flags(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(None, description='Scope to one vault.'),
    lint_type: str | None = Query(None, pattern='^(structural|quality|governance|schema)$'),
    target_type: str | None = Query(None),
    status: str = Query('pending', pattern='^(pending|resolved|dismissed)$'),
    limit: int = Query(20, ge=1, le=200),
    cursor: str | None = Query(None, description='Opaque cursor from a prior page.'),
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Agent surface — shape-stable, cursor-paginated.

    Returns ``{findings: [...], next_cursor: str|null}``. The envelope is
    stable across empty / partial / full pages so agents never need to
    handle a missing key.

    Acceptance criteria path: when the maintenance ledger is missing returns 503
    with the documented initialization-error envelope.
    """
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.READ)
    try:
        page = await api.lint.get_findings(
            vault_id=vault_id,
            lint_type=lint_type,
            target_type=target_type,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    except LintSubsystemNotInitializedError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                'error': 'lint_subsystem_not_initialized',
                'message': str(exc),
                'missing_migration': '025_maintenance_proposals',
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        raise _handle_error(e, 'Failed to query lint flags')

    return {
        'findings': [f.model_dump(mode='json') for f in page.findings],
        'next_cursor': page.next_cursor,
    }
