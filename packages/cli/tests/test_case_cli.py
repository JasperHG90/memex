"""Functional tests for `memex case list` and `memex case view`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from memex_cli.procedural import case_app
from memex_common.schemas import NoteDTO, NoteListItemDTO


def _note_dto(**overrides) -> NoteDTO:
    defaults = {
        'id': uuid4(),
        'title': 'A case',
        'name': 'a-case',
        'original_text': '## Trigger\nX\n\n## Outcome / Lesson\nsuccess.',
        'created_at': datetime.now(timezone.utc),
        'vault_id': uuid4(),
        'vault_name': 'procedural',
        'description': 'Case (success): X',
        'doc_metadata': {
            'outcome': 'success',
            'project': 'demo',
            'submitted_by': 'memex-cli',
            'case_of': str(uuid4()),
            'tags': ['case', 'demo'],
        },
        'status': 'active',
    }
    defaults.update(overrides)
    return NoteDTO(**defaults)


def _list_item_dto(**overrides) -> NoteListItemDTO:
    defaults = {
        'id': uuid4(),
        'title': 'A case',
        'name': 'a-case',
        'created_at': datetime.now(timezone.utc),
        'vault_id': uuid4(),
        'vault_name': 'procedural',
        'description': 'Case (success): X',
        'doc_metadata': {
            'outcome': 'success',
            'project': 'demo',
            'submitted_by': 'memex-cli',
        },
        'status': 'active',
    }
    defaults.update(overrides)
    return NoteListItemDTO(**defaults)


def test_case_list_forwards_filters_and_renders_table(runner, mock_api, mock_config, monkeypatch):
    item = _list_item_dto(name='stale-cache', doc_metadata={'outcome': 'failure'})
    mock_api.case_list.return_value = [item]
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        case_app,
        ['list', '--outcome', 'failure', '--tag', 'cache', '--project-id', 'p1'],
        obj=mock_config,
    )
    assert result.exit_code == 0, result.output
    mock_api.case_list.assert_called_once()
    kwargs = mock_api.case_list.call_args.kwargs
    assert kwargs['outcome'] == 'failure'
    assert kwargs['tags'] == ['cache']
    assert kwargs['project_id'] == 'p1'
    assert 'stale-cache' in result.stdout
    assert 'failure' in result.stdout


def test_case_list_json_output(runner, mock_api, mock_config, monkeypatch):
    item = _list_item_dto()
    mock_api.case_list.return_value = [item]
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(case_app, ['list', '--json'], obj=mock_config)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert data[0]['id'] == str(item.id)


def test_case_list_compact_output(runner, mock_api, mock_config, monkeypatch):
    item = _list_item_dto(name='compact-case')
    mock_api.case_list.return_value = [item]
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(case_app, ['list', '--compact'], obj=mock_config)
    assert result.exit_code == 0, result.output
    assert str(item.id) in result.stdout
    assert 'compact-case' in result.stdout


def test_case_list_rejects_bad_outcome(runner, mock_config):
    result = runner.invoke(case_app, ['list', '--outcome', 'boom'], obj=mock_config)
    assert result.exit_code == 2
    assert 'success|failure|mixed' in result.stdout


def test_case_view_renders_note(runner, mock_api, mock_config, monkeypatch):
    note = _note_dto()
    mock_api.case_get.return_value = note
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(case_app, ['view', str(note.id)], obj=mock_config)
    assert result.exit_code == 0, result.output
    mock_api.case_get.assert_called_once_with(note.id)
    assert note.name in result.stdout
    assert 'success' in result.stdout
    assert '## Trigger' in result.stdout


def test_case_view_json_output(runner, mock_api, mock_config, monkeypatch):
    note = _note_dto()
    mock_api.case_get.return_value = note
    monkeypatch.setattr('memex_cli.procedural.get_api_context', lambda config: mock_api)

    result = runner.invoke(case_app, ['view', str(note.id), '--json'], obj=mock_config)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data['id'] == str(note.id)
    assert data['doc_metadata']['outcome'] == 'success'


def test_case_view_rejects_invalid_uuid(runner, mock_config):
    result = runner.invoke(case_app, ['view', 'not-a-uuid'], obj=mock_config)
    assert result.exit_code == 2
    assert 'not a valid UUID' in result.stdout
