"""Tests for the '*' wildcard vault shorthand in CLI commands."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_cli.notes import app as note_app
from memex_common.schemas import NoteDTO


def test_note_list_wildcard_vault(runner, mock_api, mock_config, monkeypatch):
    """list --vault '*' should pass vault_ids=None (all vaults)."""
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list', '--vault', '*'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(
        limit=50,
        offset=0,
        vault_ids=None,
        after=None,
        before=None,
        template=None,
        date_field='created_at',
    )


def test_note_recent_wildcard_vault(runner, mock_api, mock_config, monkeypatch):
    """recent --vault '*' should pass vault_ids=None (all vaults)."""
    mock_api.get_recent_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['recent', '--vault', '*'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.get_recent_notes.assert_called_once_with(
        limit=10,
        vault_ids=None,
        after=None,
        before=None,
        date_field='created_at',
    )


def test_note_find_wildcard_vault(runner, mock_api, mock_config, monkeypatch):
    """find --vault '*' should pass vault_ids=None (all vaults)."""
    mock_api.find_notes_by_title.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['find', 'test query', '--vault', '*'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.find_notes_by_title.assert_called_once_with(
        query='test query',
        vault_ids=None,
        limit=5,
    )


def test_note_export_wildcard_vault(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """export --vault '*' should pass vault_ids=None (all vaults)."""
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = str(tmp_path / 'export')
    result = runner.invoke(
        note_app, ['export', '--vault', '*', '--output', out_dir], obj=mock_config
    )
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(limit=10000, vault_ids=None)


def test_note_list_specific_vault_still_works(runner, mock_api, mock_config, monkeypatch):
    """Ensure normal --vault usage is not broken by the wildcard feature."""
    mock_api.list_notes.return_value = [
        NoteDTO(
            id=uuid4(),
            name='Note',
            created_at=datetime.now(timezone.utc),
            vault_id=uuid4(),
        )
    ]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list', '--vault', 'my-vault'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(
        limit=50,
        offset=0,
        vault_ids=['my-vault'],
        after=None,
        before=None,
        template=None,
        date_field='created_at',
    )


def test_note_list_no_vault_uses_config_default(runner, mock_api, mock_config, monkeypatch):
    """Without --vault, should fall back to config.read_vaults."""
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(
        limit=50,
        offset=0,
        vault_ids=mock_config.read_vaults,
        after=None,
        before=None,
        template=None,
        date_field='created_at',
    )
