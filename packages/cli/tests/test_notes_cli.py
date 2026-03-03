from uuid import uuid4
from memex_cli.notes import app as note_app
from memex_common.schemas import NoteDTO
from datetime import datetime, timezone


def test_note_list(runner, mock_api, monkeypatch):
    mock_api.list_notes.return_value = [
        NoteDTO(id=uuid4(), name='My Note', created_at=datetime.now(timezone.utc), vault_id=uuid4())
    ]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list'])
    assert result.exit_code == 0
    assert 'My Note' in result.stdout
    mock_api.list_notes.assert_called_once_with(limit=50, offset=0, vault_ids=None)


def test_note_list_with_vault(runner, mock_api, monkeypatch):
    """list --vault passes vault_ids to the API."""
    vault_id = str(uuid4())
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list', '--vault', vault_id])
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(limit=50, offset=0, vault_ids=[vault_id])


def test_note_view(runner, mock_api, monkeypatch):
    d_id = uuid4()
    mock_api.get_note.return_value = NoteDTO(
        id=d_id,
        name='Deep Dive',
        created_at=datetime.now(timezone.utc),
        vault_id=uuid4(),
        original_text='# Content Here',
    )
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['view', str(d_id)])
    assert result.exit_code == 0
    assert 'Deep Dive' in result.stdout
    assert 'Content Here' in result.stdout


def test_note_page_index_with_data(runner, mock_api, monkeypatch):
    """page-index command renders a Rich tree when page_index is present."""
    d_id = uuid4()
    mock_api.get_note_page_index.return_value = {
        'toc': [
            {
                'level': 1,
                'title': 'Introduction',
                'token_estimate': 80,
                'summary': {'what': 'An overview section'},
                'children': [],
            }
        ]
    }
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(d_id)])
    assert result.exit_code == 0
    assert 'Introduction' in result.stdout
    assert 'Page Index' in result.stdout


def test_note_page_index_none(runner, mock_api, monkeypatch):
    """page-index command prints a warning when note has no page index."""
    d_id = uuid4()
    mock_api.get_note_page_index.return_value = None
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(d_id)])
    assert result.exit_code == 0
    assert 'no page index' in result.stdout


def test_note_page_index_invalid_uuid(runner, mock_api, monkeypatch):
    """page-index command shows an error for non-UUID arguments."""
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', 'not-a-uuid'])
    assert result.exit_code == 1
    assert 'Invalid UUID' in result.stdout
