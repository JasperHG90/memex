from uuid import uuid4
from memex_cli.notes import app as note_app
from memex_common.schemas import IngestResponse, NoteCreateDTO, NoteDTO
from datetime import datetime, timezone


def test_note_list(runner, mock_api, mock_config, monkeypatch):
    mock_api.list_notes.return_value = [
        NoteDTO(id=uuid4(), name='My Note', created_at=datetime.now(timezone.utc), vault_id=uuid4())
    ]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list'], obj=mock_config)
    assert result.exit_code == 0
    assert 'My Note' in result.stdout
    mock_api.list_notes.assert_called_once_with(
        limit=50,
        offset=0,
        vault_ids=mock_config.read_vaults,
        after=None,
        before=None,
    )


def test_note_list_with_vault(runner, mock_api, mock_config, monkeypatch):
    """list --vault passes vault_ids to the API."""
    vault_id = str(uuid4())
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['list', '--vault', vault_id], obj=mock_config)
    assert result.exit_code == 0
    mock_api.list_notes.assert_called_once_with(
        limit=50,
        offset=0,
        vault_ids=[vault_id],
        after=None,
        before=None,
    )


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


# ---------------------------------------------------------------------------
# note add tests
# ---------------------------------------------------------------------------


def test_add_note_text(runner, mock_api, mock_config, monkeypatch):
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['add', 'Hello world'], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Note' in result.stdout
    assert 'Note added successfully!' in result.stdout

    mock_api.ingest.assert_called_once()
    note = mock_api.ingest.call_args[0][0]
    assert isinstance(note, NoteCreateDTO)
    assert note.content == b'SGVsbG8gd29ybGQ='


def test_add_note_file(tmp_path, runner, mock_api, mock_config, monkeypatch):
    note_file = tmp_path / 'test_note.md'
    note_file.write_text('# Test Note')

    mock_api.ingest_upload.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['add', '--file', str(note_file)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Note' in result.stdout
    assert 'Note added successfully!' in result.stdout

    mock_api.ingest_upload.assert_called_once()
    kwargs = mock_api.ingest_upload.call_args.kwargs
    files = kwargs['files']
    assert len(files) == 1
    assert files[0][1][0] == 'test_note.md'
    assert files[0][1][1] == b'# Test Note'


def test_add_note_directory(tmp_path, runner, mock_api, mock_config, monkeypatch):
    note_dir = tmp_path / 'my_note'
    note_dir.mkdir()
    (note_dir / 'NOTE.md').write_text('# Main')
    (note_dir / 'image.png').write_bytes(b'png')

    mock_api.ingest_upload.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['add', '--file', str(note_dir)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Note' in result.stdout
    assert 'Note added successfully!' in result.stdout

    mock_api.ingest_upload.assert_called_once()
    kwargs = mock_api.ingest_upload.call_args.kwargs
    files = kwargs['files']
    assert len(files) == 2
    filenames = [f[1][0] for f in files]
    assert 'NOTE.md' in filenames
    assert 'image.png' in filenames


def test_add_note_file_not_exists(runner):
    result = runner.invoke(note_app, ['add', '--file', 'non_existent.md'])
    assert result.exit_code == 1
    assert 'Error: Path does not exist' in result.stdout


def test_add_note_with_vault(runner, mock_api, mock_config, monkeypatch):
    captured_config = None

    def mock_get_api_context(config):
        nonlocal captured_config
        captured_config = config
        return mock_api

    mock_api.ingest.return_value = IngestResponse(
        status='success', note_id='test-uuid', unit_ids=[uuid4()]
    )
    monkeypatch.setattr('memex_cli.notes.get_api_context', mock_get_api_context)

    result = runner.invoke(note_app, ['add', 'test', '--vault', 'MyVault'], obj=mock_config)
    assert result.exit_code == 0
    assert captured_config is not None
    assert captured_config.server.active_vault == 'MyVault'


def test_add_note_with_key(runner, mock_api, mock_config, monkeypatch):
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app, ['add', 'Hello world', '--key', 'my-stable-key'], obj=mock_config
    )
    assert result.exit_code == 0

    mock_api.ingest.assert_called_once()
    note = mock_api.ingest.call_args[0][0]
    assert isinstance(note, NoteCreateDTO)
    assert note.note_key == 'my-stable-key'
