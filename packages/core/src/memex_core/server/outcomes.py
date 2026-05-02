"""F29 — Outcome recording endpoint.

HTTP wire surface for ``MemexAPI.record_outcome`` (F14 ADD-2). Required so
remote clients (the Hermes plugin via ``RemoteMemexAPI``) can train MW
scoring on memory units or procedure KV keys. The MCP tool calls
``api.record_outcome`` in-process; this route gives non-in-process clients
the same surface.

Routes:
- POST /api/v1/outcomes/record  — record an outcome (memory_unit or kv_key mode)
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from memex_common.config import Permission
from memex_common.exceptions import MemexError
from memex_core.api import MemexAPI
from memex_core.server.auth import (
    AuthContext,
    check_vault_access,
    get_auth_context,
    require_write,
)
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.outcomes')

router = APIRouter(prefix='/api/v1/outcomes')


class RecordOutcomeRequest(BaseModel):
    success: bool = Field(
        ...,
        description='True if the task succeeded using these memories or this procedure.',
    )
    unit_ids: list[str] | None = Field(
        default=None,
        description=(
            'memory_unit mode only. UUIDs of memory units that were load-bearing '
            "in the agent's reasoning. Required when target_type='memory_unit'."
        ),
    )
    vault_id: str | None = Field(
        default=None,
        description='Vault UUID or name. Resolved server-side via VaultService.',
    )
    outcome_confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description='Weight for this outcome signal (0.0-1.0). Default 1.0.',
    )
    reason: str | None = Field(
        default=None,
        description='Optional free-text reason (logged, not stored on units).',
    )
    target_type: str = Field(
        default='memory_unit',
        description=(
            "What the outcome scores. 'memory_unit' increments MW counters on "
            "the memory units in unit_ids. 'kv_key' (F14) increments counters "
            'on the procedure_outcomes row for kv_key.'
        ),
    )
    kv_key: str | None = Field(
        default=None,
        description=(
            'kv_key mode only. Procedure KV key (procedure:<verb>:<context-tag>). '
            "Required when target_type='kv_key'."
        ),
    )


@router.post('/record', dependencies=[Depends(require_write)])
async def post_record_outcome(
    body: RecordOutcomeRequest,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Record an outcome for memory units or a procedure key.

    Mirrors :meth:`MemexAPI.record_outcome`; preserves the F14 ADD-2 contract
    (positional ``unit_ids``, ``success`` at the in-process call site).
    Vault is resolved server-side so callers may pass UUID or name.

    Per Wave 0 multi-tenant invariant: when a ``vault_id`` is supplied the
    auth context is gated via :func:`check_vault_access` so a key scoped to
    vault-A cannot record an outcome against vault-B (HIGH-4 sub-finding).
    """
    resolved_vault: str | None = None
    if body.vault_id is not None:
        try:
            resolved_vault = str(await api.resolve_vault_identifier(body.vault_id))
        except (MemexError, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=400, detail=f'Unknown vault: {body.vault_id!r}'
            ) from exc
        await check_vault_access(auth, [resolved_vault], api, permission=Permission.WRITE)

    try:
        return await api.record_outcome(
            body.unit_ids,
            body.success,
            resolved_vault,
            body.outcome_confidence,
            body.reason,
            target_type=body.target_type,
            kv_key=body.kv_key,
        )
    except ValueError as exc:
        # OutcomeService raises ValueError for missing unit_ids / kv_key /
        # vault_id and for invalid target_type. These are caller errors.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (MemexError, KeyError, RuntimeError, OSError) as exc:
        raise _handle_error(exc, 'Failed to record outcome')
