from uuid import uuid4
from memex_cli.notes import app as note_app
from memex_common.schemas import IngestResponse, NoteCreateDTO, NoteDTO, NodeDTO
from datetime import datetime, timezone
import json


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
    assert captured_config.vault.active == 'MyVault'


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


# ---------------------------------------------------------------------------
# Batch: note node (multi-ID)
# ---------------------------------------------------------------------------


def _make_node(**overrides):
    defaults = dict(
        id=uuid4(),
        note_id=uuid4(),
        vault_id=uuid4(),
        title='Section',
        text='body',
        level=1,
        seq=0,
        status='active',
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return NodeDTO(**defaults)


def test_note_node_multi(runner, mock_api, monkeypatch):
    n1, n2 = _make_node(title='Intro'), _make_node(title='Body')
    mock_api.get_nodes.return_value = [n1, n2]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['node', str(n1.id), str(n2.id)])
    assert result.exit_code == 0
    assert 'Intro' in result.stdout
    assert 'Body' in result.stdout
    mock_api.get_nodes.assert_called_once()


def test_note_node_multi_json(runner, mock_api, monkeypatch):
    n1, n2 = _make_node(title='A'), _make_node(title='B')
    mock_api.get_nodes.return_value = [n1, n2]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['node', str(n1.id), str(n2.id), '--json'])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


# ---------------------------------------------------------------------------
# Batch: note page-index (multi-ID)
# ---------------------------------------------------------------------------


def test_note_page_index_multi(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    toc1 = {'toc': [{'level': 1, 'title': 'Ch1', 'children': []}]}
    toc2 = {'toc': [{'level': 1, 'title': 'Ch2', 'children': []}]}
    mock_api.get_note_page_index.side_effect = [toc1, toc2]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(id1), str(id2)])
    assert result.exit_code == 0
    assert 'Ch1' in result.stdout
    assert 'Ch2' in result.stdout


def test_note_page_index_multi_partial_none(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    toc1 = {'toc': [{'level': 1, 'title': 'Exists', 'children': []}]}
    mock_api.get_note_page_index.side_effect = [toc1, None]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(id1), str(id2)])
    assert result.exit_code == 0
    assert 'Exists' in result.stdout
    assert 'no page index' in result.stdout


def test_note_page_index_multi_partial_error(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    toc1 = {'toc': [{'level': 1, 'title': 'Good', 'children': []}]}
    mock_api.get_note_page_index.side_effect = [toc1, RuntimeError('server down')]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(id1), str(id2)])
    assert result.exit_code == 0
    assert 'Good' in result.stdout
    assert 'Error' in result.stdout


def test_note_page_index_multi_json(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    toc1 = {'toc': [{'level': 1, 'title': 'A', 'children': []}]}
    mock_api.get_note_page_index.side_effect = [toc1, None]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['page-index', str(id1), str(id2), '--json'])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Batch: note metadata (multi-ID)
# ---------------------------------------------------------------------------


def test_note_metadata_multi(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    mock_api.get_notes_metadata.return_value = [
        {'note_id': str(id1), 'title': 'Doc A', 'tags': []},
        {'note_id': str(id2), 'title': 'Doc B', 'tags': ['x']},
    ]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['metadata', str(id1), str(id2)])
    assert result.exit_code == 0
    assert 'Doc A' in result.stdout
    assert 'Doc B' in result.stdout
    mock_api.get_notes_metadata.assert_called_once()


def test_note_metadata_multi_json(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    mock_api.get_notes_metadata.return_value = [
        {'note_id': str(id1), 'title': 'A'},
        {'note_id': str(id2), 'title': 'B'},
    ]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['metadata', str(id1), str(id2), '--json'])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_note_metadata_multi_empty(runner, mock_api, monkeypatch):
    id1, id2 = uuid4(), uuid4()
    mock_api.get_notes_metadata.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['metadata', str(id1), str(id2)])
    assert result.exit_code == 0
    assert 'No metadata found' in result.stdout


# ---------------------------------------------------------------------------
# Batch: note get-asset (multi-path)
# ---------------------------------------------------------------------------


def test_get_asset_multi_to_dir(runner, mock_api, mock_config, monkeypatch, tmp_path):
    p1, p2 = 'assets/a/photo.png', 'assets/b/data.csv'
    mock_api.get_resource.side_effect = [b'PNG_BYTES', b'CSV_BYTES']
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['get-asset', p1, p2, '-d', str(tmp_path)], obj=mock_config)
    assert result.exit_code == 0
    assert (tmp_path / 'photo.png').read_bytes() == b'PNG_BYTES'
    assert (tmp_path / 'data.csv').read_bytes() == b'CSV_BYTES'


def test_get_asset_multi_partial_error(runner, mock_api, mock_config, monkeypatch, tmp_path):
    p1, p2 = 'assets/a/good.png', 'assets/b/bad.csv'
    mock_api.get_resource.side_effect = [b'GOOD', FileNotFoundError('missing')]
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['get-asset', p1, p2, '-d', str(tmp_path)], obj=mock_config)
    assert result.exit_code == 0
    assert (tmp_path / 'good.png').read_bytes() == b'GOOD'
    assert not (tmp_path / 'bad.csv').exists()
    assert 'Error' in result.stdout
