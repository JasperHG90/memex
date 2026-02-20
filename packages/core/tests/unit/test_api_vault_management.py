import pytest
from unittest.mock import MagicMock
from uuid import uuid4


@pytest.mark.asyncio
async def test_create_vault_success(api, mock_metastore, mock_session):
    from memex_core.memory.sql_models import Vault

    # Mock finding no existing vault
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_session.exec.return_value = mock_result

    result = await api.create_vault(name='new-vault', description='A new vault')

    assert isinstance(result, Vault)
    assert result.name == 'new-vault'
    assert result.description == 'A new vault'

    # Verify we checked for existence
    mock_session.exec.assert_called()
    # Verify we added and committed
    mock_session.add.assert_called_with(result)
    mock_session.commit.assert_called()
    mock_session.refresh.assert_called_with(result)


@pytest.mark.asyncio
async def test_create_vault_duplicate_name(api, mock_session):
    from memex_core.memory.sql_models import Vault

    # Mock finding an existing vault
    existing_vault = Vault(id=uuid4(), name='existing-vault')
    mock_result = MagicMock()
    mock_result.first.return_value = existing_vault
    mock_session.exec.return_value = mock_result

    with pytest.raises(ValueError, match="Vault with name 'existing-vault' already exists"):
        await api.create_vault(name='existing-vault')

    # Verify we did NOT add or commit
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()
