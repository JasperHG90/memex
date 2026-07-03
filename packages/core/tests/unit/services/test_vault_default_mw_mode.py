"""Unit tests for VaultService wiring the configured Memory Worth default mode.

`OutcomesConfig.mw_mode_default` is the operator dial for new vaults. The
service must read it on `create_vault` and pass it to the `Vault` constructor
so the default flip from 'stationary' to 'ema' actually applies; per-vault
overrides via `set_mw_mode` continue to work unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_core.memory.sql_models import MWMode
from memex_core.services.vaults import VaultService


@pytest.fixture
def vault_service(mock_metastore, mock_filestore, mock_config):
    svc = VaultService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )
    audit = MagicMock()
    audit.log = MagicMock()
    svc._audit_service = audit
    return svc


@pytest.mark.asyncio
async def test_create_vault_defaults_to_configured_mw_mode_ema(
    vault_service, mock_session, mock_config
):
    """Default config (mw_mode_default='ema') applies to new vaults."""
    mock_config.server.memory.outcomes.mw_mode_default = 'ema'

    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec = AsyncMock(return_value=mock_result)
    mock_session.refresh = AsyncMock()

    await vault_service.create_vault('vault-ema')

    added = mock_session.add.call_args.args[0]
    assert added.mw_mode == MWMode.EMA


@pytest.mark.asyncio
async def test_create_vault_honours_stationary_override(vault_service, mock_session, mock_config):
    """Operators can still pin new vaults to stationary mode."""
    mock_config.server.memory.outcomes.mw_mode_default = 'stationary'

    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec = AsyncMock(return_value=mock_result)
    mock_session.refresh = AsyncMock()

    await vault_service.create_vault('vault-stationary')

    added = mock_session.add.call_args.args[0]
    assert added.mw_mode == MWMode.STATIONARY


@pytest.mark.asyncio
async def test_create_vault_rejects_invalid_default_mode(vault_service, mock_session, mock_config):
    """Unknown mw_mode_default values must surface a ValueError."""
    mock_config.server.memory.outcomes.mw_mode_default = 'bogus'

    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec = AsyncMock(return_value=mock_result)
    mock_session.refresh = AsyncMock()

    with pytest.raises(ValueError, match='mw_mode_default'):
        await vault_service.create_vault('vault-bogus')
