"""Functional tests for `memex procedure list` and `memex procedure view`."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from memex_cli.procedural import app
from memex_common.procedural_schemas import ProceduralEntryDTO


def _entry_dto(**overrides) -> ProceduralEntryDTO:
    defaults = {
        'id': uuid4(),
        'vault_id': uuid4(),
        'kind': 'procedure',
        'scope': 'global',
        'verb': 'deploy',
        'context': 'staging',
        'title': 'Deploy to staging',
        'summary': 'Deploy the service to staging.',
        'body': '## Steps\n\n1. Roll the canary.',
        'trigger': 'deploying to staging',
        'tags': [],
        'status': 'published',
        'origin': 'derived',
        'created_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return ProceduralEntryDTO(**defaults)


def test_procedure_list_forwards_sort_and_renders_created_at(
    runner, mock_api, mock_config, monkeypatch
):
    entry = _entry_dto()
    mock_api.procedural_list.return_value = [entry]
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        app,
        ['list', '--sort', 'created_at'],
        obj=mock_config,
    )
    assert result.exit_code == 0, result.output
    mock_api.procedural_list.assert_called_once()
    kwargs = mock_api.procedural_list.call_args.kwargs
    assert kwargs['sort'] == 'created_at'
    assert 'Created At' in result.stdout
    # Rich truncates cell contents at the default runner width; assert that
    # the date is present in the JSON output instead of the rendered table.
    json_result = runner.invoke(app, ['list', '--sort', 'created_at', '--json'], obj=mock_config)
    data = __import__('json').loads(json_result.stdout)
    assert data[0]['created_at'][:19] == entry.created_at.isoformat()[:19]


def test_procedure_view_renders_entry(runner, mock_api, mock_config, monkeypatch):
    entry = _entry_dto()
    mock_api.procedural_get.return_value = entry
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(entry.id)], obj=mock_config)
    assert result.exit_code == 0, result.output
    mock_api.procedural_get.assert_called_once_with(entry.id)
    assert entry.title in result.stdout
    assert 'procedure/global' in result.stdout


def test_procedure_get_alias_still_works(runner, mock_api, mock_config, monkeypatch):
    entry = _entry_dto()
    mock_api.procedural_get.return_value = entry
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['get', str(entry.id)], obj=mock_config)
    assert result.exit_code == 0, result.output
    mock_api.procedural_get.assert_called_once_with(entry.id)
    assert entry.title in result.stdout or 'Deploy to staging' in result.stdout


def test_procedure_view_json_output(runner, mock_api, mock_config, monkeypatch):
    entry = _entry_dto()
    mock_api.procedural_get.return_value = entry
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(entry.id), '--json'], obj=mock_config)
    assert result.exit_code == 0, result.output
    data = __import__('json').loads(result.stdout)
    assert data['id'] == str(entry.id)
