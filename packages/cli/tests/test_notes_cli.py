import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

from memex_cli.notes import app as note_app
from memex_common.schemas import (
    IngestResponse,
    NoteAppendRequest,
    NoteAppendResponse,
    NoteCreateDTO,
    NoteDTO,
    NodeDTO,
)


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
        template=None,
        date_field='created_at',
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
        template=None,
        date_field='created_at',
    )


def test_note_list_date_by_publish_date(runner, mock_api, mock_config, monkeypatch):
    """--date-by publish_date forwards to the API as date_field."""
    mock_api.list_notes.return_value = []
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        ['list', '--after', '2026-04-23', '--date-by', 'publish_date'],
        obj=mock_config,
    )
    assert result.exit_code == 0
    call_kwargs = mock_api.list_notes.call_args.kwargs
    assert call_kwargs['date_field'] == 'publish_date'


def test_note_list_invalid_date_by_rejected(runner, mock_api, mock_config, monkeypatch):
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)
    result = runner.invoke(note_app, ['list', '--date-by', 'banana'], obj=mock_config)
    assert result.exit_code != 0
    assert 'Invalid --date-by' in result.stdout


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


def test_add_note_with_metadata(runner, mock_api, mock_config, monkeypatch):
    """All metadata flags populate NoteCreateDTO and inject frontmatter."""
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        [
            'add',
            'Hello world',
            '--title',
            'My Title',
            '--author',
            'alice',
            '--tag',
            'research',
            '--tag',
            'ml',
            '--date',
            '2026-01-15',
            '--description',
            'A test note',
        ],
        obj=mock_config,
    )
    assert result.exit_code == 0

    mock_api.ingest.assert_called_once()
    note = mock_api.ingest.call_args[0][0]
    assert isinstance(note, NoteCreateDTO)
    assert note.name == 'My Title'
    assert note.description == 'A test note'
    assert note.author == 'alice'
    assert note.tags == ['research', 'ml', 'cli', 'quick-note']

    decoded = base64.b64decode(note.content).decode('utf-8')
    assert 'Hello world' in decoded
    assert 'title: My Title' in decoded
    assert 'date: ' in decoded
    assert '2026-01-15' in decoded
    assert 'author: alice' in decoded


def test_add_note_metadata_defaults(runner, mock_api, mock_config, monkeypatch):
    """No metadata flags preserves existing defaults (no frontmatter added)."""
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(note_app, ['add', 'Hello world'], obj=mock_config)
    assert result.exit_code == 0

    note = mock_api.ingest.call_args[0][0]
    assert note.name == 'Quick Note'
    assert note.description == 'Added via CLI'
    assert note.author is None
    assert note.tags == ['cli', 'quick-note']
    # No frontmatter when no metadata flags
    assert note.content == b'SGVsbG8gd29ybGQ='


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
# Batch: note assets get (multi-path)
# ---------------------------------------------------------------------------


def test_get_asset_multi_to_dir(runner, mock_api, mock_config, monkeypatch, tmp_path):
    p1, p2 = 'assets/a/photo.png', 'assets/b/data.csv'
    mock_api.get_resource.side_effect = [b'PNG_BYTES', b'CSV_BYTES']
    monkeypatch.setattr('memex_cli.assets.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app, ['assets', 'get', p1, p2, '-d', str(tmp_path)], obj=mock_config
    )
    assert result.exit_code == 0
    assert (tmp_path / 'photo.png').read_bytes() == b'PNG_BYTES'
    assert (tmp_path / 'data.csv').read_bytes() == b'CSV_BYTES'


def test_get_asset_multi_partial_error(runner, mock_api, mock_config, monkeypatch, tmp_path):
    p1, p2 = 'assets/a/good.png', 'assets/b/bad.csv'
    mock_api.get_resource.side_effect = [b'GOOD', FileNotFoundError('missing')]
    monkeypatch.setattr('memex_cli.assets.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app, ['assets', 'get', p1, p2, '-d', str(tmp_path)], obj=mock_config
    )
    assert result.exit_code == 0
    assert (tmp_path / 'good.png').read_bytes() == b'GOOD'
    assert not (tmp_path / 'bad.csv').exists()
    assert 'Error' in result.stdout


# ---------------------------------------------------------------------------
# `memex note append` command
# ---------------------------------------------------------------------------


def _append_response(note_id, append_id, *, status='success', new_units=0):
    return NoteAppendResponse(
        status=status,
        note_id=note_id,
        append_id=append_id,
        content_hash='abcdef',
        delta_bytes=10,
        new_unit_ids=[uuid4() for _ in range(new_units)],
    )


def test_note_append_with_delta_flag(runner, mock_api, mock_config, monkeypatch):
    """`memex note append <id> --delta X` builds a NoteAppendRequest and calls the API."""
    note_id = uuid4()
    append_id = uuid4()
    mock_api.append_to_note.return_value = _append_response(note_id, append_id, new_units=2)
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        ['append', str(note_id), '--delta', 'continued thought', '--append-id', str(append_id)],
        obj=mock_config,
    )
    assert result.exit_code == 0, result.stdout
    mock_api.append_to_note.assert_called_once()
    request = mock_api.append_to_note.call_args.args[0]
    assert isinstance(request, NoteAppendRequest)
    assert request.note_id == note_id
    assert request.delta == 'continued thought'
    assert request.append_id == append_id
    assert request.joiner == 'paragraph'


def test_note_append_from_delta_file(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """--delta-file is read and passed as the delta."""
    note_id = uuid4()
    append_id = uuid4()
    delta_path = tmp_path / 'snippet.md'
    delta_path.write_text('payload from file')

    mock_api.append_to_note.return_value = _append_response(note_id, append_id)
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        [
            'append',
            str(note_id),
            '--delta-file',
            str(delta_path),
            '--append-id',
            str(append_id),
        ],
        obj=mock_config,
    )
    assert result.exit_code == 0, result.stdout
    request = mock_api.append_to_note.call_args.args[0]
    assert request.delta == 'payload from file'


def test_note_append_by_key_with_vault(runner, mock_api, mock_config, monkeypatch):
    """`--key` + `--vault` resolves to (note_key, vault_id) on the request."""
    append_id = uuid4()
    mock_api.append_to_note.return_value = _append_response(uuid4(), append_id)
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        [
            'append',
            '--key',
            'session-2026-04-26',
            '--vault',
            'global',
            '--delta',
            'progress note',
            '--append-id',
            str(append_id),
        ],
        obj=mock_config,
    )
    assert result.exit_code == 0, result.stdout
    request = mock_api.append_to_note.call_args.args[0]
    assert request.note_key == 'session-2026-04-26'
    assert request.vault_id == 'global'
    assert request.note_id is None


def test_note_append_auto_generates_append_id(runner, mock_api, mock_config, monkeypatch):
    """Two calls without --append-id produce different append_ids."""
    note_id = uuid4()
    mock_api.append_to_note.return_value = _append_response(note_id, uuid4())
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result1 = runner.invoke(note_app, ['append', str(note_id), '--delta', 'one'], obj=mock_config)
    result2 = runner.invoke(note_app, ['append', str(note_id), '--delta', 'two'], obj=mock_config)
    assert result1.exit_code == 0 and result2.exit_code == 0

    calls = mock_api.append_to_note.call_args_list
    assert len(calls) == 2
    id1 = calls[0].args[0].append_id
    id2 = calls[1].args[0].append_id
    assert id1 != id2


def test_note_append_quiet_prints_unit_count(runner, mock_api, mock_config, monkeypatch):
    """--quiet outputs only the new-unit count."""
    note_id = uuid4()
    mock_api.append_to_note.return_value = _append_response(note_id, uuid4(), new_units=3)
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app, ['append', str(note_id), '--delta', 'x', '--quiet'], obj=mock_config
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == '3'


def test_note_append_requires_some_identifier(runner, mock_api, mock_config, monkeypatch):
    """No note_id and no --key → error."""
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)
    result = runner.invoke(note_app, ['append', '--delta', 'x'], obj=mock_config)
    assert result.exit_code != 0
    mock_api.append_to_note.assert_not_called()


def test_note_append_key_without_vault_errors(runner, mock_api, mock_config, monkeypatch):
    """`--key` requires `--vault`."""
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)
    result = runner.invoke(note_app, ['append', '--key', 'k', '--delta', 'x'], obj=mock_config)
    assert result.exit_code != 0
    mock_api.append_to_note.assert_not_called()


def test_note_append_rejects_both_delta_and_file(
    runner, mock_api, mock_config, monkeypatch, tmp_path
):
    """--delta and --delta-file are mutually exclusive."""
    note_id = uuid4()
    delta_path = tmp_path / 'd.md'
    delta_path.write_text('body')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        ['append', str(note_id), '--delta', 'inline', '--delta-file', str(delta_path)],
        obj=mock_config,
    )
    assert result.exit_code != 0
    mock_api.append_to_note.assert_not_called()


def test_note_append_rejects_both_note_id_and_key(runner, mock_api, mock_config, monkeypatch):
    """Positional note_id + --key together → loud error, no API call.

    Without this check the schema would silently let note_id win, which is
    hostile when the user actually meant the --key.
    """
    note_id = uuid4()
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        note_app,
        [
            'append',
            str(note_id),
            '--key',
            'session-key',
            '--vault',
            'global',
            '--delta',
            'x',
        ],
        obj=mock_config,
    )
    assert result.exit_code != 0
    mock_api.append_to_note.assert_not_called()
