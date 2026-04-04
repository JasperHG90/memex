"""Vault summary endpoints."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from memex_common.exceptions import MemexError
from memex_common.schemas import VaultSummaryDTO
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api
from memex_core.api import MemexAPI
from memex_core.memory.sql_models import VaultSummary

logger = logging.getLogger('memex.core.server.vault_summary')

router = APIRouter(prefix='/api/v1')


def _summary_to_dto(summary: VaultSummary) -> VaultSummaryDTO:
    return VaultSummaryDTO(
        id=summary.id,
        vault_id=summary.vault_id,
        summary=summary.summary,
        topics=summary.topics,
        stats=summary.stats,
        version=summary.version,
        notes_incorporated=summary.notes_incorporated,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
    )


@router.get(
    '/vaults/{vault_id}/summary',
    response_model=VaultSummaryDTO,
    dependencies=[Depends(require_read)],
    summary='Get vault summary',
    description='Retrieve the current summary for a vault.',
)
async def get_vault_summary(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> VaultSummaryDTO:
    """Return the current vault summary, or 404 if none exists."""
    try:
        summary = await api.vault_summary.get_summary(vault_id)
        if summary is None:
            raise HTTPException(status_code=404, detail='No summary exists for this vault')
        return _summary_to_dto(summary)
    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get vault summary')


@router.post(
    '/vaults/{vault_id}/summary/regenerate',
    response_model=VaultSummaryDTO,
    dependencies=[Depends(require_write)],
    summary='Regenerate vault summary',
    description='Trigger full regeneration of the vault summary from all notes.',
)
async def regenerate_vault_summary(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> VaultSummaryDTO:
    """Regenerate the vault summary from scratch."""
    try:
        summary = await api.vault_summary.regenerate_summary(vault_id)
        return _summary_to_dto(summary)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to regenerate vault summary')
