from memex_cli.vaults import app
from uuid import uuid4
import httpx
from unittest.mock import MagicMock


def test_create_vault_passes_name_and_description_positionally(
    runner, mock_api, strip_ansi, monkeypatch
):
    """Unit pin: CLI calls `api.create_vault(name, description)` with positional
    args matching `MemexAPI.create_vault(self, name: str, description: str | None = None)`.
    Regression guard against the V4 signature-drift bug fixed in 2af755b4."""
    vault_uuid = uuid4()
    vault = MagicMock()
    vault.id = vault_uuid
    mock_api.create_vault.return_value = vault

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['create', 'my-vault', '--description', 'docs'])
    assert result.exit_code == 0, result.stdout
    mock_api.create_vault.assert_called_once_with('my-vault', 'docs', kind='content', policy=None)

    clean_stdout = strip_ansi(result.stdout)
    assert 'Creating vault: my-vault' in clean_stdout
    assert str(vault_uuid) in clean_stdout


def test_create_vault_with_default_description(runner, mock_api, monkeypatch):
    """When --description is omitted, the CLI must pass None (not a missing arg)."""
    mock_api.create_vault.return_value = MagicMock(id=uuid4())
    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['create', 'bare-vault'])
    assert result.exit_code == 0, result.stdout
    mock_api.create_vault.assert_called_once_with('bare-vault', None, kind='content', policy=None)


def test_create_vault_positive_reflect_and_summarize_flags(runner, mock_api, monkeypatch):
    """--reflect and --summarize must produce a policy dict with True values.

    Regression guard for the V11 review finding: a system vault that wants
    synthesis on had no positive CLI flag, so the policy stayed empty and
    the kind default (False) won.
    """
    mock_api.create_vault.return_value = MagicMock(id=uuid4())
    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        app,
        [
            'create',
            'case-vault',
            '--kind',
            'system',
            '--reflect',
            '--summarize',
            '--force',
        ],
    )
    assert result.exit_code == 0, result.stdout
    mock_api.create_vault.assert_called_once_with(
        'case-vault',
        None,
        kind='system',
        policy={'reflect': True, 'summarize': True},
    )


def test_create_vault_mutually_exclusive_reflect_flags(runner, mock_api, monkeypatch, strip_ansi):
    """--reflect and --no-reflect must not both be accepted (silent policy)."""
    mock_api.create_vault.return_value = MagicMock(id=uuid4())
    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        app,
        [
            'create',
            'case-vault',
            '--kind',
            'system',
            '--reflect',
            '--no-reflect',
            '--force',
        ],
    )
    assert result.exit_code == 1
    assert '--reflect and --no-reflect' in strip_ansi(result.stdout)
    mock_api.create_vault.assert_not_called()


def test_create_vault_mutually_exclusive_summarize_flags(runner, mock_api, monkeypatch, strip_ansi):
    """--summarize and --no-summarize must not both be accepted."""
    mock_api.create_vault.return_value = MagicMock(id=uuid4())
    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        app,
        [
            'create',
            'case-vault',
            '--kind',
            'system',
            '--summarize',
            '--no-summarize',
            '--force',
        ],
    )
    assert result.exit_code == 1
    assert '--summarize and --no-summarize' in strip_ansi(result.stdout)
    mock_api.create_vault.assert_not_called()


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


def _mock_stats(notes=5, memories=20, entities=10, reflection_queue=3):
    """Create a mock SystemStatsCountsDTO."""
    s = MagicMock()
    s.notes = notes
    s.memories = memories
    s.entities = entities
    s.reflection_queue = reflection_queue
    return s


def test_truncate_vault_with_force(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()
    vault_name = 'test-vault'

    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_stats_counts.return_value = _mock_stats()
    mock_api.truncate_vault.return_value = {
        'notes': 5,
        'memory_units': 20,
        'entities': 10,
        'mental_models': 3,
        'reflection_queue': 3,
    }

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['clear', vault_name, '--force'])
    assert result.exit_code == 0
    clean = strip_ansi(result.stdout)
    assert 'Vault cleared' in clean
    mock_api.truncate_vault.assert_called_once_with(vault_uuid)


def test_truncate_vault_shows_stats(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()
    vault_name = 'test-vault'

    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_stats_counts.return_value = _mock_stats(notes=12, memories=50)
    mock_api.truncate_vault.return_value = {'notes': 12, 'memory_units': 50}

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['clear', vault_name, '--force'])
    assert result.exit_code == 0
    clean = strip_ansi(result.stdout)
    assert '12' in clean  # notes count shown
    assert '50' in clean  # memories count shown


def test_truncate_vault_aborted_without_force(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()

    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_stats_counts.return_value = _mock_stats()

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['clear', 'test-vault'], input='n\n')
    assert result.exit_code == 0
    clean = strip_ansi(result.stdout)
    assert 'Aborted' in clean
    mock_api.truncate_vault.assert_not_called()


def test_truncate_empty_vault(runner, mock_api, strip_ansi, monkeypatch):
    vault_uuid = uuid4()

    mock_api.resolve_vault_identifier.return_value = vault_uuid
    mock_api.get_stats_counts.return_value = _mock_stats(
        notes=0, memories=0, entities=0, reflection_queue=0
    )

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['clear', 'empty-vault', '--force'])
    assert result.exit_code == 0
    clean = strip_ansi(result.stdout)
    assert 'already empty' in clean
    mock_api.truncate_vault.assert_not_called()


def test_truncate_vault_not_found(runner, mock_api, strip_ansi, monkeypatch):
    vault_name = 'non-existent'

    response = httpx.Response(404, json={'detail': f"Vault '{vault_name}' not found"})
    mock_api.resolve_vault_identifier.side_effect = httpx.HTTPStatusError(
        f"Vault '{vault_name}' not found", request=None, response=response
    )

    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['clear', vault_name, '--force'])
    assert result.exit_code == 1
    clean = strip_ansi(result.stdout)
    assert f"Vault '{vault_name}' not found" in clean


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

    # Verify arguments — CLI passes (name, description) positionally to
    # match MemexAPI.create_vault, plus the kind/policy kwargs (default content).
    mock_api.create_vault.assert_called_once_with(
        vault_name, vault_desc, kind='content', policy=None
    )
