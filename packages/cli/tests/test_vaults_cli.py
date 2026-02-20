from memex_cli.vaults import app
from uuid import uuid4
import httpx


def test_delete_vault_by_name(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()
    vault_name = 'test-vault'

    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.delete_vault.return_value = True

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    # Test deleting by name with --force to skip confirmation
    result = runner.invoke(app, ['delete', vault_name, '--force'])
    assert result.exit_code == 0
    clean_stdout = strip_ansi(result.stdout)
    assert f'Deleting vault: {vault_name} ({vault_uuid})' in clean_stdout
    mock_api.resolve_vault_identifier.assert_called_once_with(vault_name)
    mock_api.delete_vault.assert_called_once_with(vault_uuid)


def test_delete_vault_not_found(runner, mock_api, strip_ansi, monkeypatch):
    vault_name = 'non-existent'

    # Simulate a 404 from the server
    response = httpx.Response(404, json={'detail': f"Vault '{vault_name}' not found"})
    mock_api.resolve_vault_identifier.side_effect = httpx.HTTPStatusError(
        f"Vault '{vault_name}' not found", request=None, response=response
    )

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['delete', vault_name, '--force'])
    assert result.exit_code == 1
    clean_stdout = strip_ansi(result.stdout)
    assert f"Vault '{vault_name}' not found" in clean_stdout


def test_create_vault(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()
    vault_name = 'new-vault'
    vault_desc = 'A new test vault'

    class MockVault:
        id = vault_uuid
        name = vault_name
        description = vault_desc

    mock_api.create_vault.return_value = MockVault()

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['create', vault_name, '--description', vault_desc])

    assert result.exit_code == 0
    clean_stdout = strip_ansi(result.stdout)
    assert f'Vault created successfully! ID: {vault_uuid}' in clean_stdout

    # Verify arguments
    call_args = mock_api.create_vault.call_args[0][0]
    assert call_args.name == vault_name
    assert call_args.description == vault_desc
