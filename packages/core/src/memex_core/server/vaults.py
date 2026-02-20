"""Vault endpoints."""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from memex_common.schemas import CreateVaultRequest, DefaultVaultsResponse, VaultDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server.vaults')

router = APIRouter(prefix='/api/v1')


@router.get('/vaults/defaults', response_model=DefaultVaultsResponse)
async def get_default_vaults(api: Annotated[MemexAPI, Depends(get_api)]):
    """Return the active (writer) vault and any attached read-only vaults."""
    try:
        # Resolve the active vault
        active_vault_name = api.config.server.active_vault
        active = await api.get_vault_by_name(active_vault_name)
        if not active:
            raise HTTPException(
                status_code=404, detail=f'Active vault "{active_vault_name}" not found'
            )
        active_dto = VaultDTO(id=active.id, name=active.name, description=active.description)

        # Resolve attached vaults (skip any that fail)
        attached_dtos: list[VaultDTO] = []
        for vault_name in api.config.server.attached_vaults:
            try:
                vault = await api.get_vault_by_name(vault_name)
                if vault:
                    attached_dtos.append(
                        VaultDTO(id=vault.id, name=vault.name, description=vault.description)
                    )
                else:
                    logger.warning('Attached vault "%s" not found, skipping', vault_name)
            except Exception:
                logger.warning('Failed to resolve attached vault "%s", skipping', vault_name)

        return DefaultVaultsResponse(active_vault=active_dto, attached_vaults=attached_dtos)
    except Exception as e:
        raise _handle_error(e, 'Failed to retrieve default vaults')


@router.get('/vaults/active', response_model=VaultDTO)
async def get_active_vault(api: Annotated[MemexAPI, Depends(get_api)]):
    try:
        active_vault_name = api.config.server.active_vault
        vault = await api.get_vault_by_name(active_vault_name)
        if not vault:
            raise HTTPException(
                status_code=404, detail=f'Active vault "{active_vault_name}" not found'
            )
        return VaultDTO(id=vault.id, name=vault.name, description=vault.description)
    except Exception as e:
        raise _handle_error(e, 'Failed to retrieve active vault')


@router.get(
    '/vaults',
    response_class=StreamingResponse,
    responses=ndjson_openapi(VaultDTO, 'Stream of vaults.'),
)
async def list_vaults(api: Annotated[MemexAPI, Depends(get_api)]):
    try:
        vaults = await api.list_vaults()
        return ndjson_response(
            [VaultDTO(id=v.id, name=v.name, description=v.description) for v in vaults]
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to list vaults')


@router.post('/vaults', response_model=VaultDTO)
async def create_vault(
    request: Annotated[CreateVaultRequest, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    try:
        vault = await api.create_vault(name=request.name, description=request.description)
        return VaultDTO(id=vault.id, name=vault.name, description=vault.description)
    except Exception as e:
        raise _handle_error(e, 'Failed to create vault')


@router.get('/vaults/resolve/{identifier}', response_model=dict[str, Any])
async def resolve_vault(identifier: str, api: Annotated[MemexAPI, Depends(get_api)]):
    try:
        vault_id = await api.resolve_vault_identifier(identifier)
        return {'id': vault_id}
    except Exception as e:
        raise _handle_error(e, 'Vault resolution failed')


@router.delete('/vaults/{vault_id}')
async def delete_vault(vault_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a vault."""
    try:
        success = await api.delete_vault(vault_id)
        if success:
            return {'status': 'success'}
        raise HTTPException(status_code=404, detail='Vault not found or could not be deleted')
    except Exception as e:
        raise _handle_error(e, 'Vault deletion failed')
