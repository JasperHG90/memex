"""Tests for the `memex note export` command."""

import datetime as dt
import json
from uuid import uuid4

import pytest
from memex_common.schemas import NoteDTO
from memex_cli.notes import app


@pytest.fixture
def note_no_assets() -> NoteDTO:
    return NoteDTO(
        id=uuid4(),
        title='My Plain Note',
        name='My Plain Note',
        original_text='# Hello\n\nSome content here.',
        created_at=dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
        assets=[],
        doc_metadata={'author': 'test'},
    )


@pytest.fixture
def note_with_assets() -> NoteDTO:
    note_id = uuid4()
    vault_id = uuid4()
    return NoteDTO(
        id=note_id,
        title='Asset Note',
        name='Asset Note',
        original_text='# With Image\n\n![photo](photo.png)',
        created_at=dt.datetime(2025, 7, 1, tzinfo=dt.timezone.utc),
        vault_id=vault_id,
        assets=[
            f'assets/memex/{note_id}/photo.png',
            f'assets/memex/{note_id}/data.csv',
        ],
        doc_metadata={'source': 'upload'},
    )


def test_export_single_note_no_assets(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Export a single note without assets."""
    note = NoteDTO(
        id=uuid4(),
        title='My Plain Note',
        name='My Plain Note',
        original_text='# Hello\n\nSome content here.',
        created_at=dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
        assets=[],
        doc_metadata={'author': 'test'},
    )
    mock_api.get_note.return_value = note
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', str(note.id), '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert 'Exported 1 note(s) and 0 asset(s)' in result.stdout

    # Find the note directory
    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1

    note_dir = dirs[0]
    assert (note_dir / 'note.md').exists()
    assert (note_dir / 'note.md').read_text() == '# Hello\n\nSome content here.'
    assert (note_dir / 'metadata.json').exists()

    meta = json.loads((note_dir / 'metadata.json').read_text())
    assert meta['id'] == str(note.id)
    assert meta['title'] == 'My Plain Note'

    # No assets directory
    assert not (note_dir / 'assets').exists()


def test_export_single_note_with_assets(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Export a note that has attached assets (image + csv)."""
    note_id = uuid4()
    vault_id = uuid4()
    note = NoteDTO(
        id=note_id,
        title='Asset Note',
        name='Asset Note',
        original_text='# With Image\n\n![photo](photo.png)',
        created_at=dt.datetime(2025, 7, 1, tzinfo=dt.timezone.utc),
        vault_id=vault_id,
        assets=[
            f'assets/memex/{note_id}/photo.png',
            f'assets/memex/{note_id}/data.csv',
        ],
        doc_metadata={'source': 'upload'},
    )

    mock_api.get_note.return_value = note

    # Mock get_resource to return different bytes per asset
    png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    csv_bytes = b'col1,col2\nval1,val2\n'

    async def mock_get_resource(path: str) -> bytes:
        if 'photo.png' in path:
            return png_bytes
        if 'data.csv' in path:
            return csv_bytes
        raise FileNotFoundError(path)

    mock_api.get_resource.side_effect = mock_get_resource
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', str(note.id), '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert 'Exported 1 note(s) and 2 asset(s)' in result.stdout

    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    note_dir = dirs[0]

    # Check assets
    assets_dir = note_dir / 'assets'
    assert assets_dir.exists()
    assert (assets_dir / 'photo.png').read_bytes() == png_bytes
    assert (assets_dir / 'data.csv').read_bytes() == csv_bytes


def test_export_all_notes(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Export all notes when no note_id is given."""
    notes = [
        NoteDTO(
            id=uuid4(),
            title=f'Note {i}',
            name=f'Note {i}',
            original_text=f'Content {i}',
            created_at=dt.datetime(2025, 1, i + 1, tzinfo=dt.timezone.utc),
            vault_id=uuid4(),
            assets=[],
            doc_metadata={},
        )
        for i in range(3)
    ]
    mock_api.list_notes.return_value = notes
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert 'Exported 3 note(s) and 0 asset(s)' in result.stdout

    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 3


def test_export_asset_failure_is_non_fatal(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """If an asset fails to download, the note is still exported with a warning."""
    note_id = uuid4()
    note = NoteDTO(
        id=note_id,
        title='Broken Asset',
        name='Broken Asset',
        original_text='# Content',
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
        assets=[f'assets/memex/{note_id}/missing.bin'],
        doc_metadata={},
    )
    mock_api.get_note.return_value = note
    mock_api.get_resource.side_effect = Exception('Connection refused')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', str(note.id), '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert 'Warning' in result.stdout
    assert 'Exported 1 note(s) and 0 asset(s)' in result.stdout

    # Note content should still be written
    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    assert (dirs[0] / 'note.md').exists()


def test_export_note_without_content(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Notes with None original_text export an empty markdown file."""
    note = NoteDTO(
        id=uuid4(),
        title='Empty Note',
        name='Empty Note',
        original_text=None,
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
        assets=[],
        doc_metadata={},
    )
    mock_api.get_note.return_value = note
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', str(note.id), '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert (dirs[0] / 'note.md').read_text() == ''


def test_export_note_title_sanitization(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Unsafe characters in titles are sanitized for directory names."""
    note = NoteDTO(
        id=uuid4(),
        title='My/Note: <With> "Special" | Chars?',
        name='My/Note: <With> "Special" | Chars?',
        original_text='# OK',
        created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
        assets=[],
        doc_metadata={},
    )
    mock_api.get_note.return_value = note
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_dir = tmp_path / 'export'
    result = runner.invoke(app, ['export', str(note.id), '-o', str(out_dir)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    dirs = [d for d in out_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    # Directory name should not contain unsafe chars
    dir_name = dirs[0].name
    for c in '/<>:"|?*\\':
        assert c not in dir_name
