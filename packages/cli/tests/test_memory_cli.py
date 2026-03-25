import json
from memex_cli.memory import app
from memex_common.schemas import IngestResponse, MemoryUnitDTO, NoteCreateDTO
from uuid import uuid4


def test_add_memory_text(runner, mock_api, mock_config, monkeypatch):
    # Mock get_api_context
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    # Test adding text
    result = runner.invoke(app, ['add', 'Hello world'], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Memory' in result.stdout
    assert 'Memory added successfully!' in result.stdout

    # Verify call
    mock_api.ingest.assert_called_once()
    note = mock_api.ingest.call_args[0][0]
    assert isinstance(note, NoteCreateDTO)
    # NoteCreateDTO.content now stores Base64 encoded bytes
    assert note.content == b'SGVsbG8gd29ybGQ='


def test_add_memory_file(tmp_path, runner, mock_api, mock_config, monkeypatch):
    # Create a dummy file
    note_file = tmp_path / 'test_note.md'
    note_file.write_text('# Test Note')

    mock_api.ingest_upload.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    # Test adding file
    result = runner.invoke(app, ['add', '--file', str(note_file)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Memory' in result.stdout
    assert 'Memory added successfully!' in result.stdout

    # Verify call
    mock_api.ingest_upload.assert_called_once()
    kwargs = mock_api.ingest_upload.call_args.kwargs
    files = kwargs['files']
    assert len(files) == 1
    assert files[0][1][0] == 'test_note.md'
    assert files[0][1][1] == b'# Test Note'


def test_add_memory_directory(tmp_path, runner, mock_api, mock_config, monkeypatch):
    # Setup a dummy directory
    note_dir = tmp_path / 'my_note'
    note_dir.mkdir()
    (note_dir / 'NOTE.md').write_text('# Main')
    (note_dir / 'image.png').write_bytes(b'png')

    mock_api.ingest_upload.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    # Test adding directory
    result = runner.invoke(app, ['add', '--file', str(note_dir)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Adding Memory' in result.stdout
    assert 'Memory added successfully!' in result.stdout

    # Verify call
    mock_api.ingest_upload.assert_called_once()
    kwargs = mock_api.ingest_upload.call_args.kwargs
    files = kwargs['files']
    assert len(files) == 2
    filenames = [f[1][0] for f in files]
    assert 'NOTE.md' in filenames
    assert 'image.png' in filenames


def test_add_memory_file_not_exists(runner):
    # Test adding non-existent file
    result = runner.invoke(app, ['add', '--file', 'non_existent.md'])
    assert result.exit_code == 1
    assert 'Error: Path does not exist' in result.stdout


def test_add_memory_with_vault(runner, mock_api, mock_config, monkeypatch):
    # Capture config
    captured_config = None

    def mock_get_api_context(config):
        nonlocal captured_config
        captured_config = config
        return mock_api

    mock_api.ingest.return_value = IngestResponse(
        status='success', note_id='test-uuid', unit_ids=[uuid4()]
    )
    monkeypatch.setattr('memex_cli.memory.get_api_context', mock_get_api_context)

    result = runner.invoke(app, ['add', 'test', '--vault', 'MyVault'], obj=mock_config)
    assert result.exit_code == 0
    assert captured_config is not None
    assert captured_config.vault.active == 'MyVault'


def test_add_memory_with_key(runner, mock_api, mock_config, monkeypatch):
    # Mock get_api_context
    mock_api.ingest.return_value = IngestResponse(status='success', note_id='test-uuid')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    # Test adding text with key
    result = runner.invoke(app, ['add', 'Hello world', '--key', 'my-stable-key'], obj=mock_config)
    assert result.exit_code == 0

    # Verify call
    mock_api.ingest.assert_called_once()
    note = mock_api.ingest.call_args[0][0]
    assert isinstance(note, NoteCreateDTO)
    assert note.note_key == 'my-stable-key'


# ---------------------------------------------------------------------------
# memory view
# ---------------------------------------------------------------------------


def test_memory_view_single(runner, mock_api, mock_config, monkeypatch):
    uid = uuid4()
    mock_api.get_memory_unit.return_value = MemoryUnitDTO(
        id=uid, text='Python is a language', fact_type='world'
    )
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(uid)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Python is a language' in result.stdout
    assert str(uid) in result.stdout


def test_memory_view_single_json(runner, mock_api, mock_config, monkeypatch):
    uid = uuid4()
    mock_api.get_memory_unit.return_value = MemoryUnitDTO(id=uid, text='A fact', fact_type='world')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(uid), '--json'], obj=mock_config)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data['id'] == str(uid)


def test_memory_view_multi(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='Fact one', fact_type='world'),
        MemoryUnitDTO(id=u2, text='Fact two', fact_type='event'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Fact one' in result.stdout
    assert 'Fact two' in result.stdout


def test_memory_view_multi_json(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='A', fact_type='world'),
        MemoryUnitDTO(id=u2, text='B', fact_type='event'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2), '--json'], obj=mock_config)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_memory_view_multi_partial_error(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='Good one', fact_type='world'),
        RuntimeError('not found'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Good one' in result.stdout
    assert 'Error' in result.stdout
