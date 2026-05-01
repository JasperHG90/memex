"""F20 — FSRS-5 revisitation HTTP endpoints.

HTTP wire surface for the F20 revisit verbs so non-in-process clients
(the Hermes plugin via ``RemoteMemexAPI``) can call them. The MCP tool
calls the in-process API directly; this file gives remote callers parity.

Routes:
- GET  /api/v1/memory/due_for_review  — list units due for revisit in a vault
- POST /api/v1/memory/review          — record a review outcome (FSRS-5 + audit)

Vault scoping (Wave 0 invariant): both routes require ``vault_id`` and
the service rejects cross-vault calls with PermissionError. The route
maps PermissionError → HTTP 403 so the Hermes client can surface a
structured tool_error to the agent.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field

from memex_common.exceptions import MemexError
from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.revisit')

router = APIRouter(prefix='/api/v1/memory')


def _reject_bool_quality(v: Any) -> Any:
    """Pydantic BeforeValidator that blocks ``bool`` from being coerced into
    ``int`` for the F20 ``quality`` field. Mirrors the same guard at the MCP
    boundary; without this, ``True`` would silently route to ``Quality.AGAIN``
    because ``bool ⊂ int`` in Python.
    """
    if isinstance(v, bool):
        raise ValueError(
            f"bool is not a valid quality (got {v!r}); use 1/2/3/4 or 'again'/'hard'/'good'/'easy'."
        )
    return v


class ReviewMemoryRequest(BaseModel):
    unit_id: str = Field(..., description='Memory unit UUID being reviewed.')
    quality: Annotated[
        int | str,
        BeforeValidator(_reject_bool_quality),
        Field(
            description=(
                'Review rating. Accepts the FSRS-5 IntEnum value (1=again, '
                '2=hard, 3=good, 4=easy) or the case-insensitive string '
                "('again' / 'hard' / 'good' / 'easy'). AGAIN/HARD record a "
                'failure outcome; GOOD/EASY record a success outcome.'
            ),
        ),
    ]
    vault_id: str = Field(
        ...,
        description=(
            'Vault UUID or name the memory unit belongs to. REQUIRED — '
            'the service rejects cross-vault review (Wave 0 vault-scoping invariant).'
        ),
    )


@router.get('/due_for_review', dependencies=[Depends(require_read)])
async def get_due_for_review(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: Annotated[
        str,
        Query(description='Vault UUID or name (REQUIRED).'),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=200, description='Maximum due units to return.'),
    ] = 20,
) -> list[dict[str, Any]]:
    """List memory units due for FSRS-5 revisit in a vault.

    Returns units whose ``revisit_due_at <= now()`` AND that pass the
    5-gate eligibility predicate. READ verb — does NOT advance any
    schedule.
    """
    try:
        resolved_vault = await api.resolve_vault_identifier(vault_id)
    except (MemexError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f'Unknown vault: {vault_id!r}') from exc

    try:
        due = await api.get_due_for_review(resolved_vault, limit=limit)
    except (MemexError, KeyError, RuntimeError, OSError) as exc:
        raise _handle_error(exc, 'Failed to list due-for-review units')

    return [
        {
            'unit_id': str(d.unit_id),
            'text_preview': d.text_preview,
            'revisit_due_at': d.revisit_due_at.isoformat(),
            'intent_class': d.intent_class,
        }
        for d in due
    ]


@router.post('/review', dependencies=[Depends(require_write)])
async def post_review(
    body: ReviewMemoryRequest,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> dict[str, Any]:
    """Record a review outcome on a memory unit.

    Advances the FSRS-5 schedule, increments outcome counters, maintains
    the sticky-deprioritize streak, and writes an audit row — all in a
    single transaction. Cross-vault rejection: if the unit's vault does
    not match the supplied ``vault_id``, the service raises
    PermissionError which surfaces here as HTTP 403.
    """
    from memex_core.memory.revisit import Quality

    try:
        unit_uuid = UUID(body.unit_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f'Invalid memory unit UUID: {body.unit_id!r}'
        ) from exc

    if isinstance(body.quality, int):
        try:
            quality_enum = Quality(body.quality)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f'Invalid quality {body.quality!r}; must be 1 (again), '
                    '2 (hard), 3 (good), or 4 (easy).'
                ),
            ) from exc
    else:
        try:
            quality_enum = Quality[body.quality.strip().upper()]
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f'Invalid quality {body.quality!r}; must be one of '
                    "'again', 'hard', 'good', 'easy'."
                ),
            ) from exc

    try:
        resolved_vault = await api.resolve_vault_identifier(body.vault_id)
    except (MemexError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f'Unknown vault: {body.vault_id!r}') from exc

    try:
        return await api.review_memory_unit(unit_uuid, quality_enum, vault_id=resolved_vault)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MemexError, KeyError, RuntimeError, OSError) as exc:
        raise _handle_error(exc, 'Failed to review memory unit')
