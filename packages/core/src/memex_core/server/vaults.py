"""Vault endpoints."""

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import CreateVaultRequest, VaultDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server.vaults')

router = APIRouter(prefix='/api/v1')


@router.get(
    '/vaults',
    response_class=StreamingResponse,
    responses=ndjson_openapi(VaultDTO, 'Stream of vaults.'),
)
async def list_vaults(
    api: Annotated[MemexAPI, Depends(get_api)],
    state: Literal['active'] | None = Query(
        None, description='Filter by state: "active" for active vault'
    ),
    is_default: bool | None = Query(None, description='Filter by default status'),
):
    """
    List vaults.

    Query params:
    - state: Optional filter by state. Use 'active' for the active vault.
    - is_default: Optional filter by default status. True for default vaults.
    """
    try:
        if state == 'active':
            active_vault_name = api.config.server.default_active_vault
            vault = await api.get_vault_by_name(active_vault_name)
            if not vault:
                raise HTTPException(
                    status_code=404,
                    detail=f'Active vault "{active_vault_name}" not found',
                )
            return ndjson_response(
                [VaultDTO(id=vault.id, name=vault.name, description=vault.description)]
            )

        if is_default:
            # Resolve the active vault
            active_vault_name = api.config.server.default_active_vault
            active = await api.get_vault_by_name(active_vault_name)
            if not active:
                raise HTTPException(
                    status_code=404,
                    detail=f'Active vault "{active_vault_name}" not found',
                )
            active_dto = VaultDTO(id=active.id, name=active.name, description=active.description)

            # Resolve default reader vault (if different from active)
            reader_name = api.config.server.default_reader_vault
            dtos: list[VaultDTO] = [active_dto]
            if reader_name != active_vault_name:
                try:
                    reader = await api.get_vault_by_name(reader_name)
                    if reader:
                        dtos.append(
                            VaultDTO(
                                id=reader.id,
                                name=reader.name,
                                description=reader.description,
                            )
                        )
                    else:
                        logger.warning('Reader vault "%s" not found, skipping', reader_name)
                except (MemexError, OSError) as e:
                    logger.warning(
                        'Failed to resolve reader vault "%s", skipping: %s',
                        reader_name,
                        e,
                    )

            return ndjson_response(dtos)

        # Default: list all vaults with note counts
        rows = await api.list_vaults_with_counts()
        active_vault_id = await api.resolve_vault_identifier(api.config.server.default_active_vault)
        return ndjson_response(
            [
                VaultDTO(
                    id=row['vault'].id,
                    name=row['vault'].name,
                    description=row['vault'].description,
                    is_active=(row['vault'].id == active_vault_id),
                    note_count=row['note_count'],
                )
                for row in rows
            ]
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list vaults')


@router.post('/vaults', response_model=VaultDTO)
async def create_vault(
    request: Annotated[CreateVaultRequest, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    """Create a new vault."""
    try:
        vault = await api.create_vault(name=request.name, description=request.description)
        return VaultDTO(id=vault.id, name=vault.name, description=vault.description)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to create vault')


@router.get('/vaults/{identifier}', response_model=dict[str, Any])
async def get_or_resolve_vault(identifier: str, api: Annotated[MemexAPI, Depends(get_api)]):
    """
    Get a vault by ID or resolve by name.

    If the identifier is a valid UUID, returns the vault with that ID.
    Otherwise, treats it as a name and resolves to the vault's ID.
    """
    try:
        # Check if identifier is a valid UUID
        try:
            vault_id = UUID(identifier)
            # It's a UUID, verify it exists
            vaults = await api.list_vaults()
            if any(v.id == vault_id for v in vaults):
                return {'id': vault_id}
            raise HTTPException(status_code=404, detail=f'Vault with ID {identifier} not found')
        except ValueError:
            # Not a UUID, treat as a name and resolve
            vault_id = await api.resolve_vault_identifier(identifier)
            return {'id': vault_id}
    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Vault lookup failed')


@router.delete('/vaults/{vault_id}')
async def delete_vault(vault_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a vault."""
    try:
        success = await api.delete_vault(vault_id)
        if success:
            return {'status': 'success'}
        raise HTTPException(status_code=404, detail='Vault not found or could not be deleted')
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Vault deletion failed')


@router.post('/vaults/{identifier}/set-writer')
async def set_writer_vault(identifier: str, api: Annotated[MemexAPI, Depends(get_api)]):
    """
    Set the active (writer) vault for the current server session.
    This is a runtime override — on restart, config file values apply again.
    """
    try:
        vault_id = await api.resolve_vault_identifier(identifier)
        api.config.server.default_active_vault = identifier
        return {'status': 'success', 'active_vault': str(vault_id)}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to set writer vault')


@router.post('/vaults/{identifier}/set-reader')
async def set_reader_vault(
    identifier: str,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """
    Set the default reader vault for search/retrieval.
    This is a runtime override — on restart, config file values apply again.
    """
    try:
        vault_id = await api.resolve_vault_identifier(identifier)
        api.config.server.default_reader_vault = identifier
        return {
            'status': 'success',
            'default_reader_vault': str(vault_id),
        }
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to set reader vault')
