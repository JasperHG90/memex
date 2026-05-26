"""Lint endpoints (maintenance ledger).

Routes:
- GET    /api/v1/lint/status                          — pending counts (global + per-vault)
- GET    /api/v1/lint/findings                        — list findings (CLI surface, offset paged)
- GET    /api/v1/lint/flags                           — cursor-paginated agent surface
- POST   /api/v1/lint/findings/{finding_id}/dismiss   — flip status to 'dismissed' (optional note)
- POST   /api/v1/lint/findings/{finding_id}/resolve   — flip status to 'resolved' (optional canned action + note)
- POST   /api/v1/lint/findings/{finding_id}/apply     — DEPRECATED: alias for the winner-proposal apply path
- POST   /api/v1/lint/findings/{finding_id}/reverse   — reverse a previously applied resolution

The ``findings`` endpoint backs ``memex lint findings`` (CLI). The
``flags`` endpoint is the agent surface — shape-stable returns and
opaque cursor pagination, mirrored by ``memex_get_lint_flags`` MCP.

``/resolve`` accepts a structured payload from the cockpit:
``{action?, params?, note?}``. When ``action`` is supplied the server
looks the action_id up in ``services.proposal_actions``, runs
``execute(...)``, captures ``prior_state`` + ``applied_state`` under
``evidence.resolution.followup``, and atomically flips status to
``resolved`` in a single ``UPDATE``. When ``action`` is omitted the
endpoint preserves its legacy pure-status-flip behaviour (plus the
historical ``entity_collapse_cluster`` carveout). All canned-action
mutations gate on :func:`_require_attended_mode` exactly like
``/apply`` does today — they are equally destructive.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
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
from memex_core.services.proposal_actions import (
    ActionValidationError,
    ProposalActionError,
    get_action,
)

logger = logging.getLogger('memex.core.server.lint')

_UNATTENDED_OPT_IN_ENV = 'MEMEX_LINT_ALLOW_UNATTENDED_APPLY'


def _require_attended_mode(api: MemexAPI) -> None:
    """Block destructive lint mutations when auth is disabled.

    The apply / reverse endpoints mutate memory units, notes, and link
    typing — the audit row identifies the call path but does not gate the
    write. When ``server.auth.enabled=False`` no human principal is on the
    request, so an unattended LLM driver could end-to-end drive both calls
    without review. Refuse unless the operator explicitly opts in via
    ``MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1`` (or ``true`` / ``yes``).
    """
    if api.config.server.auth.enabled:
        return
    opt_in = os.environ.get(_UNATTENDED_OPT_IN_ENV, '').strip().lower()
    if opt_in in ('1', 'true', 'yes'):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            'Destructive lint mutations require auth enabled. '
            f'Set {_UNATTENDED_OPT_IN_ENV}=1 to override (e.g. for trusted CI).'
        ),
    )


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
                    'SELECT mp.id::text, mp.vault_id::text, mp.lint_type, mp.target_type, '
                    'mp.target_id, mp.rule_name, mp.evidence, mp.suggested_action, mp.status, '
                    'mp.source, mp.created_at, mp.resolved_at, mp.resolved_by, '
                    '(SELECT mu.text FROM memory_units mu '
                    "WHERE mp.target_type = 'memory_unit' "
                    'AND mu.id::text = mp.target_id) AS target_text '
                    f'FROM maintenance_proposals mp WHERE {where} '  # noqa: S608
                    'ORDER BY mp.created_at DESC '
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
    payload: Annotated[dict[str, Any] | None, Body(embed=False)] = None,
) -> dict[str, Any]:
    """Flip a pending finding to ``dismissed``. Idempotent.

    Per vault-scoping invariant: looks up the finding's vault and
    gates the auth context BEFORE mutating, so a vault-A scoped key with a
    leaked vault-B finding_id cannot dismiss the vault-B row (cross-vault check).

    Optional ``{note: str}`` payload — captured at
    ``evidence.resolution.note`` for the audit trail. Dismiss is
    non-destructive; no attended-mode gate.
    """
    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    actor = _audit_actor()
    resolution = _build_resolution_payload(
        verdict='dismissed',
        actor=actor,
        note=_extract_note(payload),
        followup=None,
    )
    try:
        ok = await api.lint.set_status(
            finding_id,
            'dismissed',
            vault_id=finding_vault,
            actor=actor,
            resolution=resolution,
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to dismiss finding')
    if not ok:
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return {'finding_id': str(finding_id), 'status': 'dismissed', 'resolution': resolution}


@router.post('/findings/{finding_id}/resolve', dependencies=[Depends(require_write)])
async def lint_resolve(
    finding_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    payload: Annotated[dict[str, Any] | None, Body(embed=False)] = None,
) -> dict[str, Any]:
    """Flip a pending finding to ``resolved``. Idempotent.

    Per vault-scoping invariant: looks up the finding's vault and
    gates the auth context BEFORE mutating, so a vault-A scoped key with a
    leaked vault-B finding_id cannot resolve the vault-B row (cross-vault check).

    Payload (all optional):

    - ``action`` (str): a registered proposal action_id (e.g.
      ``deprioritize_unit``, ``archive_mental_model``, ``no_op``). When
      present, the server validates against ``target_type``, runs
      ``execute(...)``, stamps ``evidence.resolution.followup`` with
      ``{action, params, applied_state, prior_state, applied_at}``, and
      flips status atomically. The endpoint gates on
      :func:`_require_attended_mode` whenever ``action`` is supplied — a
      canned action is destructive by definition.
    - ``params`` (dict): forwarded verbatim to ``action.execute``. The
      action's ``validate(params, ...)`` runs before any side effect.
    - ``note`` (str): reviewer's free-form justification; stored at
      ``evidence.resolution.note``.

    Legacy carveout: ``entity_collapse_cluster`` findings still accept
    ``{"winner_id": ...}`` / ``{"winner_canonical_name": ...}`` and run
    the cluster collapse. The carveout fires only when ``action`` is
    absent from the payload.
    """
    payload = payload or {}
    finding = await _load_finding_or_404(finding_id, api)

    action_id_raw = payload.get('action')
    if action_id_raw is None and finding['rule_name'] == 'entity_collapse_cluster':
        return await _resolve_entity_collapse_cluster(
            finding=finding, api=api, auth=auth, params=payload
        )

    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    actor = _audit_actor()
    note = _extract_note(payload)

    if action_id_raw is None:
        # Pure status flip + note. Non-destructive — no attended-mode gate.
        resolution = _build_resolution_payload(
            verdict='accepted', actor=actor, note=note, followup=None
        )
        try:
            ok = await api.lint.set_status(
                finding_id,
                'resolved',
                vault_id=finding_vault,
                actor=actor,
                resolution=resolution,
            )
        except Exception as e:
            raise _handle_error(e, 'Failed to resolve finding')
        if not ok:
            raise HTTPException(status_code=404, detail='Finding not found or not pending')
        return {
            'finding_id': str(finding_id),
            'status': 'resolved',
            'resolution': resolution,
        }

    # Canned-action path — destructive; gate as such.
    _require_attended_mode(api)
    action_id = str(action_id_raw)
    try:
        action = get_action(action_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action_params = payload.get('params') or {}
    if not isinstance(action_params, dict):
        raise HTTPException(status_code=400, detail='`params` must be an object')

    target_type = str(finding['target_type'])
    target_id = str(finding['target_id'])
    if target_type not in action.applicable_target_types:
        raise HTTPException(
            status_code=400,
            detail=(
                f'action {action_id!r} does not apply to target_type {target_type!r}; '
                f'applicable types are {list(action.applicable_target_types)}'
            ),
        )
    try:
        action.validate(action_params, target_type=target_type, target_id=target_id)
    except ActionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        execute_result = await action.execute(
            api,
            action_params,
            target_id=target_id,
            vault_id=finding_vault,
            actor=actor,
        )
    except ActionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProposalActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise _handle_error(exc, f'Failed to execute proposal action {action_id}')

    followup = {
        'action': action_id,
        'params': action_params,
        'applied_at': datetime.now(timezone.utc).isoformat(),
        'applied_state': execute_result.applied_state,
        'prior_state': execute_result.prior_state,
        'reversible': action.reversible,
    }
    resolution = _build_resolution_payload(
        verdict='accepted', actor=actor, note=note, followup=followup
    )
    try:
        ok = await api.lint.set_status(
            finding_id,
            'resolved',
            vault_id=finding_vault,
            actor=actor,
            resolution=resolution,
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to flip finding status after action.execute')

    if not ok:
        # The action ran but the status flip failed (e.g. row deleted, status
        # already non-pending). Log a structured warning so operators can
        # reconcile the leaked side effect, then surface 409 to the caller.
        logger.warning(
            'lint.resolve.side_effect_without_status_flip',
            extra={
                'finding_id': str(finding_id),
                'action_id': action_id,
                'target_type': target_type,
                'target_id': target_id,
                'applied_state': execute_result.applied_state,
                'actor': actor,
            },
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f'Action {action_id} executed but finding status flip failed '
                '(finding not pending). The side effect is real; the proposal '
                'row may need manual reconciliation.'
            ),
        )

    return {
        'finding_id': str(finding_id),
        'status': 'resolved',
        'resolution': resolution,
    }


def _extract_note(payload: dict[str, Any] | None) -> str | None:
    """Pull and validate the optional ``note`` field from a verdict payload."""
    if not payload:
        return None
    raw = payload.get('note')
    if raw is None:
        return None
    note = str(raw).strip()
    if not note:
        return None
    if len(note) > 4000:
        raise HTTPException(
            status_code=400,
            detail='resolution `note` must be 4000 characters or fewer',
        )
    return note


def _build_resolution_payload(
    *,
    verdict: str,
    actor: str,
    note: str | None,
    followup: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the JSON object stored under ``evidence.resolution``.

    Stable shape across dismiss / resolve / resolve-with-action so the
    cockpit can render any of them with the same template.
    """
    body: dict[str, Any] = {
        'verdict': verdict,
        'actor': actor,
        'decided_at': datetime.now(timezone.utc).isoformat(),
    }
    if note is not None:
        body['note'] = note
    if followup is not None:
        body['followup'] = followup
    return body


async def _load_finding_or_404(finding_id: UUID, api: MemexAPI) -> dict[str, Any]:
    async with api.metastore.session() as session:
        row = (
            (
                await session.execute(
                    text(
                        'SELECT id::text AS id, vault_id, rule_name, '
                        'target_type, target_id, evidence, status '
                        'FROM maintenance_proposals WHERE id = :id'
                    ),
                    {'id': str(finding_id)},
                )
            )
            .mappings()
            .first()
        )
    if row is None or row['status'] != 'pending':
        raise HTTPException(status_code=404, detail='Finding not found or not pending')
    return dict(row)


async def _resolve_entity_collapse_cluster(
    *,
    finding: dict[str, Any],
    api: MemexAPI,
    auth: AuthContext | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Apply an entity-cluster collapse and flip the finding to resolved."""
    from uuid import UUID as PyUUID

    evidence = finding.get('evidence') or {}
    cluster_members = [str(m) for m in (evidence.get('cluster_members') or [])]
    suggested_winner = str(evidence.get('suggested_winner_id') or finding['target_id'])
    vaults_affected = [str(v) for v in (evidence.get('vaults_affected') or [])]

    if not vaults_affected:
        raise HTTPException(
            status_code=400,
            detail=(
                'Cluster has no vaults_affected; refusing to collapse without '
                'scope. Re-run the scan after the cluster has unit-entity '
                'links, or escalate manually.'
            ),
        )

    vault_uuids = [PyUUID(v) for v in vaults_affected]
    try:
        await check_vault_access(auth, vault_uuids, api, permission=Permission.WRITE)
    except HTTPException as exc:
        logger.error(
            'entity.collapse_cluster auth_denied actor=%s vaults=%d',
            getattr(auth, 'api_key_id', None) if auth else None,
            len(vault_uuids),
        )
        raise exc

    # Winner resolution / validation
    winner_param = params.get('winner_id') or params.get('winner_canonical_name')
    if winner_param is None:
        winner_id = suggested_winner
    else:
        winner_id = await _resolve_winner_id(winner_param, cluster_members, api)

    if winner_id not in cluster_members:
        raise HTTPException(
            status_code=400,
            detail=(
                'winner must be a member of the cluster; non-member overrides are '
                'not allowed in this version.'
            ),
        )

    losers = [m for m in cluster_members if m != winner_id]
    if not losers:
        raise HTTPException(status_code=400, detail='cluster has no losers to collapse')

    actor_id = getattr(auth, 'api_key_id', None) if auth else None
    try:
        summary = await api.entities.collapse_cluster(
            winner_id=PyUUID(winner_id),
            loser_ids=[PyUUID(lid) for lid in losers],
            actor=actor_id,
        )
    except Exception as exc:
        raise _handle_error(exc, 'Failed to apply entity cluster collapse')

    try:
        ok = await api.lint.set_status(
            PyUUID(str(finding['id'])),
            'resolved',
            actor=actor_id,
            vault_id=None,
        )
    except Exception as exc:
        raise _handle_error(exc, 'Failed to mark finding resolved')
    if not ok:
        raise HTTPException(status_code=409, detail='Finding state changed during apply')

    return {
        'finding_id': str(finding['id']),
        'status': 'resolved',
        'rule_name': 'entity_collapse_cluster',
        'winner_id': winner_id,
        'winner_overridden': winner_id != suggested_winner,
        'summary': summary,
    }


async def _resolve_winner_id(winner_param: str, cluster_members: list[str], api: MemexAPI) -> str:
    """Resolve ``winner_param`` to a UUID string that MUST belong to the cluster.

    Accepts either a UUID (returned as-is after membership check) or a
    case-sensitive ``canonical_name`` match against the cluster members.
    """
    from uuid import UUID as PyUUID

    try:
        as_uuid = PyUUID(winner_param)
        return str(as_uuid)
    except (ValueError, TypeError):
        pass

    member_uuids = [PyUUID(m) for m in cluster_members]
    async with api.metastore.session() as session:
        rows = (
            (
                await session.execute(
                    text(
                        'SELECT id::text AS id FROM entities '
                        'WHERE id = ANY(CAST(:ids AS uuid[])) '
                        'AND canonical_name = :name'
                    ),
                    {'ids': [str(u) for u in member_uuids], 'name': winner_param},
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        raise HTTPException(
            status_code=400,
            detail=(
                f'winner "{winner_param}" not found among cluster members '
                '(case-sensitive canonical_name match required).'
            ),
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f'winner "{winner_param}" is ambiguous: multiple cluster members '
                'share that canonical_name. Provide the winner UUID to disambiguate.'
            ),
        )
    return rows[0]['id']


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

    _require_attended_mode(api)
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
    """Reverse a previously applied resolution.

    Two paths, decided by inspecting the resolved finding's evidence:

    1. New shape — ``evidence.resolution.followup.action`` is present. The
       server looks up the action via
       :func:`memex_core.services.proposal_actions.get_action`, checks
       ``action.reversible``, and dispatches to ``action.reverse(...)``
       with the captured ``prior_state`` / ``applied_state``. Forward-only
       actions short-circuit to 409 ``{reason: 'forward_only'}`` with no
       audit row.
    2. Legacy shape — winner-proposal rows resolved before the new
       cockpit landed still carry ``evidence.action`` and an apply
       receipt under ``evidence.resolution.prior_state``. These route
       to ``reverse_winner_proposal``, preserving back-compat for
       in-flight rows.

    Either path keeps the original finding ``resolved``; new shape writes
    ``evidence.resolution.reversal``, legacy writes the
    ``propose_contradiction_winner_reversal`` audit row.
    """
    _require_attended_mode(api)
    finding_vault = await _gate_finding_for_write(finding_id, api, auth)
    actor = _audit_actor()
    finding = await _load_resolved_finding_or_404(finding_id, api)

    evidence = finding.get('evidence') or {}
    if not isinstance(evidence, dict):
        evidence = {}
    resolution = evidence.get('resolution') or {}
    followup = resolution.get('followup') if isinstance(resolution, dict) else None

    if isinstance(followup, dict) and followup.get('action'):
        return await _reverse_via_registry(
            api=api,
            finding_id=finding_id,
            finding_vault=finding_vault,
            actor=actor,
            finding=finding,
            evidence=evidence,
            resolution=resolution,
            followup=followup,
        )

    # Legacy fallback: existing winner-proposal reverse path.
    from memex_core.services.contradiction_resolution import (
        ContradictionResolutionError,
        reverse_winner_proposal,
    )

    try:
        return await reverse_winner_proposal(api, finding_id, vault_id=finding_vault, actor=actor)
    except ContradictionResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        raise _handle_error(e, 'Failed to reverse winner proposal')


async def _reverse_via_registry(
    *,
    api: MemexAPI,
    finding_id: UUID,
    finding_vault: UUID | None,
    actor: str,
    finding: dict[str, Any],
    evidence: dict[str, Any],
    resolution: dict[str, Any],
    followup: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a reverse via the proposal_actions registry."""
    import json as _json

    if resolution.get('reversal') is not None:
        raise HTTPException(
            status_code=409,
            detail='Resolution already reversed; cannot reverse twice.',
        )

    action_id = str(followup.get('action') or '')
    try:
        action = get_action(action_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not action.reversible:
        raise HTTPException(
            status_code=409,
            detail={
                'reason': 'forward_only',
                'action_id': action_id,
                'remedy': (
                    'This action is forward-only by design; reversal would not '
                    'restore the pre-execute state. Run the inverse operation '
                    'manually if a rollback is needed.'
                ),
            },
        )

    params = followup.get('params') or {}
    applied_state = followup.get('applied_state') or {}
    prior_state = followup.get('prior_state') or {}
    target_id = str(finding['target_id'])

    try:
        result = await action.reverse(
            api,
            params,
            applied_state,
            prior_state,
            target_id=target_id,
            vault_id=finding_vault,
            actor=actor,
        )
    except ActionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProposalActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise _handle_error(exc, f'Failed to reverse proposal action {action_id}')

    reversal_block = {
        'reversed_at': datetime.now(timezone.utc).isoformat(),
        'actor': actor,
        'restored_state': result.restored_state,
    }
    new_resolution = dict(resolution)
    new_resolution['reversal'] = reversal_block
    # CAS-guarded UPDATE: refuse if `resolution.reversal` was filled in
    # between our load and write (concurrent reverse caller).
    async with api.metastore.session() as session:
        update_result = await session.execute(
            text(
                """
                UPDATE maintenance_proposals
                SET evidence = jsonb_set(
                    COALESCE(evidence, '{}'::jsonb),
                    '{resolution}',
                    CAST(:resolution_json AS jsonb),
                    true
                )
                WHERE id = :id
                  AND status = 'resolved'
                  AND (evidence -> 'resolution' -> 'reversal') IS NULL
                """
            ),
            {
                'id': str(finding_id),
                'resolution_json': _json.dumps(new_resolution),
            },
        )
        await session.commit()
    if update_result.rowcount == 0:
        # The reversal already succeeded between our read and write — surface
        # the conflict so the caller can refetch.
        raise HTTPException(
            status_code=409,
            detail='Resolution state changed during reverse (concurrent update).',
        )

    return {
        'finding_id': str(finding_id),
        'status': 'resolved',
        'action_id': action_id,
        'reversal': reversal_block,
    }


async def _load_resolved_finding_or_404(finding_id: UUID, api: MemexAPI) -> dict[str, Any]:
    async with api.metastore.session() as session:
        row = (
            (
                await session.execute(
                    text(
                        'SELECT id::text AS id, vault_id, rule_name, target_type, '
                        'target_id, evidence, status '
                        'FROM maintenance_proposals WHERE id = :id'
                    ),
                    {'id': str(finding_id)},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail='Finding not found')
    if row['status'] != 'resolved':
        raise HTTPException(
            status_code=409,
            detail=f'Finding status is {row["status"]!r}; only resolved findings can be reversed.',
        )
    return dict(row)


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
                skip_quota=True,
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


# ---------------------------------------------------------------------------
# Lint auto-learning loop — Layer 2: telemetry rollups (L1-lint-auto-learning).
# ---------------------------------------------------------------------------


def _telemetry_to_json(dto: Any) -> dict[str, Any]:
    """Render a ``LintRuleTelemetryDTO`` for HTTP responses.

    Adds derived fields (``accept_rate``, ``total_count``, ``labelled_count``)
    that the cockpit and CLI render without recomputing.
    """
    return {
        'rule_name': dto.rule_name,
        'vault_id': str(dto.vault_id) if dto.vault_id is not None else None,
        'window_start': dto.window_start.isoformat() if dto.window_start else None,
        'window_end': dto.window_end.isoformat() if dto.window_end else None,
        'accept_count': dto.accept_count,
        'no_op_count': dto.no_op_count,
        'dismiss_count': dto.dismiss_count,
        'legacy_count': dto.legacy_count,
        'total_count': dto.total_count,
        'labelled_count': dto.labelled_count,
        'accept_rate': dto.accept_rate,
        'median_surprise': dto.median_surprise,
        'median_time_to_resolve_seconds': dto.median_time_to_resolve_seconds,
        'refreshed_at': dto.refreshed_at.isoformat() if dto.refreshed_at else None,
    }


@router.get('/calibration/telemetry', dependencies=[Depends(require_read)])
async def lint_calibration_telemetry(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str | None, Query(description='Filter to one rule_name.')] = None,
    vault_id: Annotated[
        UUID | None,
        Query(description='Vault scope; omit + include_global=true for the global rollup.'),
    ] = None,
    include_global: Annotated[
        bool,
        Query(description='When vault_id is omitted, include the cross-vault rollup row.'),
    ] = True,
) -> dict[str, Any]:
    """Return per-rule telemetry rows used by ``memex lint stats``.

    Layer 2 of the auto-learning loop. Read-only; never mutates rollups.
    Vault-scope auth gates per-vault rows; the global rollup
    (``vault_id IS NULL``) requires no vault scope but still requires READ.
    """
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.READ)
    try:
        rows = await api.lint_learning.get_telemetry(
            rule_name=rule,
            vault_id=vault_id,
            include_global=include_global,
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch lint telemetry')
    return {'rows': [_telemetry_to_json(r) for r in rows]}


@router.post('/calibration/refresh', dependencies=[Depends(require_write)])
async def lint_calibration_refresh(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    vault_id: Annotated[
        UUID | None,
        Query(description='Vault to rollup; omit for global-only refresh.'),
    ] = None,
    window_days: Annotated[
        int,
        Query(ge=1, le=365, description='Rolling window length. Defaults to 30.'),
    ] = 30,
) -> dict[str, Any]:
    """Recompute ``lint_rule_telemetry`` for the trailing window.

    Idempotent: running twice with the same window produces the same
    rollup. Vault-scoped refreshes also refresh the global (vault_id NULL)
    rollup. Gated by ``require_write`` — recomputing telemetry is
    cheap but it does write rows.
    """
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        result = await api.lint_learning.refresh_telemetry(
            vault_id=vault_id,
            window_days=window_days,
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to refresh lint telemetry')
    return {
        'rows_written': result.rows_written,
        'rules_seen': result.rules_seen,
        'proposals_aggregated': result.proposals_aggregated,
        'window_start': result.window_start.isoformat(),
        'window_end': result.window_end.isoformat(),
        'vault_id': str(result.vault_id) if result.vault_id else None,
    }


# ---------------------------------------------------------------------------
# Layer 3 — Threshold calibration
# ---------------------------------------------------------------------------


@router.get('/calibration/thresholds', dependencies=[Depends(require_read)])
async def lint_calibration_thresholds(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str | None, Query(description='Filter to one rule_name.')] = None,
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
) -> dict[str, Any]:
    """List calibration rows — versioned per-rule thresholds learned from verdicts."""
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.READ)
    try:
        rows = await api.lint_learning.get_calibrations(  # type: ignore[attr-defined]
            rule_name=rule, vault_id=vault_id
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch calibrations')
    return {
        'rows': [
            {
                'id': str(r.id),
                'rule_name': r.rule_name,
                'vault_id': str(r.vault_id) if r.vault_id else None,
                'version': r.version,
                'surprise_threshold': r.surprise_threshold,
                'polarity_threshold': r.polarity_threshold,
                'learned_at': r.learned_at.isoformat() if r.learned_at else None,
                'superseded_by_version': r.superseded_by_version,
                'frozen': r.frozen,
                'rationale': r.rationale,
            }
            for r in rows
        ]
    }


@router.post('/calibration/calibrate', dependencies=[Depends(require_write)])
async def lint_calibration_calibrate(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
) -> dict[str, Any]:
    """Run the threshold calibration job now.

    Reads telemetry, computes new thresholds, writes versioned calibration
    rows. Idempotent: if telemetry hasn't changed, no new rows are written.
    """
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        result = await api.lint_learning.calibrate_thresholds(  # type: ignore[attr-defined]
            vault_id=vault_id
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to calibrate thresholds')
    return {
        'rules_calibrated': result.rules_calibrated,
        'rules_skipped_frozen': result.rules_skipped_frozen,
        'rules_skipped_insufficient_data': result.rules_skipped_insufficient_data,
        'rules_unchanged': result.rules_unchanged,
        'details': result.details,
    }


@router.post('/calibration/freeze', dependencies=[Depends(require_write)])
async def lint_calibration_freeze(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str, Query(description='Rule to freeze/unfreeze.')] = '',
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
    frozen: Annotated[bool, Query(description='true to freeze, false to unfreeze.')] = True,
) -> dict[str, Any]:
    """Freeze or unfreeze auto-calibration for a specific rule."""
    if not rule:
        raise HTTPException(status_code=400, detail='rule is required')
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        ok = await api.lint_learning.freeze_rule(  # type: ignore[attr-defined]
            rule, vault_id=vault_id, frozen=frozen
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to freeze/unfreeze calibration')
    return {'rule': rule, 'frozen': frozen, 'updated': ok}


@router.post('/calibration/rollback', dependencies=[Depends(require_write)])
async def lint_calibration_rollback(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str, Query(description='Rule to rollback.')] = '',
    version: Annotated[int, Query(description='Version to rollback to.')] = 0,
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
) -> dict[str, Any]:
    """Rollback a rule's calibration to a specific version."""
    if not rule or version <= 0:
        raise HTTPException(status_code=400, detail='rule and version (>0) are required')
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        ok = await api.lint_learning.rollback_calibration(  # type: ignore[attr-defined]
            rule, version, vault_id=vault_id
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to rollback calibration')
    return {'rule': rule, 'version': version, 'rolled_back': ok}


# ---------------------------------------------------------------------------
# Layer 4 — DSPy signature optimization
# ---------------------------------------------------------------------------


@router.post('/optimize/run', dependencies=[Depends(require_write)])
async def lint_optimize_run(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str, Query(description='Rule to compile a signature for.')] = '',
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
) -> dict[str, Any]:
    """Trigger a DSPy signature compile for a specific rule.

    Pulls labelled verdicts, compiles via BootstrapFewShot, validates against
    the current champion, and promotes the winner.
    """
    _require_attended_mode(api)
    if not rule:
        raise HTTPException(status_code=400, detail='rule is required')
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        result = await api.lint_optimizer.compile(rule, vault_id=vault_id)
    except Exception as e:
        raise _handle_error(e, 'Failed to run lint optimizer')
    return {
        'rule_name': result.rule_name,
        'vault_id': str(result.vault_id) if result.vault_id else None,
        'status': result.status,
        'new_version': result.new_version,
        'validation_score': result.validation_score,
        'champion_score': result.champion_score,
        'examples_used': result.examples_used,
        'message': result.message,
        'warnings': result.warnings,
    }


@router.get('/optimize/history', dependencies=[Depends(require_read)])
async def lint_optimize_history(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str | None, Query(description='Filter to one rule_name.')] = None,
) -> dict[str, Any]:
    """List signature versions — compiled DSPy programs with validation scores."""
    try:
        sigs = await api.lint_optimizer.list_signatures(rule_name=rule)
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch signature history')
    return {
        'signatures': [
            {
                'id': str(s.id),
                'rule_name': s.rule_name,
                'vault_id': str(s.vault_id) if s.vault_id else None,
                'version': s.version,
                'base_model': s.base_model,
                'validation_score': s.validation_score,
                'validation_examples': s.validation_examples,
                'promoted_at': s.promoted_at.isoformat() if s.promoted_at else None,
                'promoted_by': s.promoted_by,
                'superseded': s.superseded_by_version is not None,
            }
            for s in sigs
        ]
    }


@router.post('/optimize/rollback', dependencies=[Depends(require_write)])
async def lint_optimize_rollback(
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
    rule: Annotated[str, Query(description='Rule to rollback.')] = '',
    version: Annotated[int, Query(description='Version to rollback to.')] = 0,
    vault_id: Annotated[UUID | None, Query(description='Vault scope.')] = None,
) -> dict[str, Any]:
    """Rollback a rule's DSPy signature to a specific version."""
    _require_attended_mode(api)
    if not rule or version <= 0:
        raise HTTPException(status_code=400, detail='rule and version (>0) are required')
    if vault_id is not None:
        await check_vault_access(auth, [vault_id], api, permission=Permission.WRITE)
    try:
        ok = await api.lint_optimizer.rollback_signature(rule, version, vault_id=vault_id)
    except Exception as e:
        raise _handle_error(e, 'Failed to rollback signature')
    return {'rule': rule, 'version': version, 'rolled_back': ok}
