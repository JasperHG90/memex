import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, patch
from memex_dashboard.pages.settings import SettingsState
from memex_common.schemas import VaultDTO


@pytest.mark.asyncio
async def test_load_vaults():
    """Test loading vaults into state."""
    state = SettingsState()

    mock_vaults = [
        VaultDTO(
            id=uuid4(), name='Test Vault', description='Desc', created_at='now', updated_at='now'
        ),
        VaultDTO(
            id=uuid4(), name='Vault 2', description='Desc 2', created_at='now', updated_at='now'
        ),
    ]

    with patch(
        'memex_dashboard.pages.settings.api_client.api.list_vaults', new_callable=AsyncMock
    ) as mock_list:
        mock_list.return_value = mock_vaults

        await state.load_vaults()

        assert len(state.vaults) == 2
        assert state.vaults[0]['name'] == 'Test Vault'
        assert state.is_loading is False


@pytest.mark.asyncio
async def test_create_vault_success():
    """Test successful vault creation."""
    state = SettingsState()
    state.new_vault_name = 'New Vault'
    state.new_vault_description = 'Description'

    with (
        patch(
            'memex_dashboard.pages.settings.api_client.api.create_vault', new_callable=AsyncMock
        ) as mock_create,
        patch(
            'memex_dashboard.pages.settings.api_client.api.list_vaults', new_callable=AsyncMock
        ) as mock_list,
        patch('reflex.toast.success') as mock_toast,
    ):
        await state.create_vault()

        mock_create.assert_called_once()
        # Should reload vaults
        mock_list.assert_called_once()
        # Should close modal
        assert state.is_create_modal_open is False
        mock_toast.assert_called()


@pytest.mark.asyncio
async def test_create_vault_validation():
    """Test validation failure (empty name)."""
    state = SettingsState()
    state.new_vault_name = ''

    with patch('reflex.toast.error') as mock_toast:
        await state.create_vault()

        mock_toast.assert_called_with('Vault name is required.')
