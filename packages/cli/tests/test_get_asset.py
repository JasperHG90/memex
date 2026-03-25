"""Tests for the `memex note get-asset` command."""

from uuid import uuid4

from memex_cli.notes import app


def test_get_asset_to_file(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Download an asset and write it to a file."""
    asset_path = f'assets/memex/{uuid4()}/photo.png'
    png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
    mock_api.get_resource.return_value = png_bytes
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_file = tmp_path / 'photo.png'
    result = runner.invoke(app, ['get-asset', asset_path, '-o', str(out_file)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert out_file.read_bytes() == png_bytes
    assert 'Saved to' in result.stdout
    mock_api.get_resource.assert_awaited_once_with(asset_path)


def test_get_asset_to_stdout(runner, mock_api, mock_config, monkeypatch):
    """Download an asset and write it to stdout."""
    asset_path = f'assets/memex/{uuid4()}/data.csv'
    csv_bytes = b'col1,col2\nval1,val2\n'
    mock_api.get_resource.return_value = csv_bytes
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['get-asset', asset_path], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert csv_bytes in result.stdout_bytes
    mock_api.get_resource.assert_awaited_once_with(asset_path)


def test_get_asset_creates_parent_dirs(runner, mock_api, mock_config, monkeypatch, tmp_path):
    """Output file parent directories are created if they don't exist."""
    asset_path = f'assets/memex/{uuid4()}/nested.txt'
    mock_api.get_resource.return_value = b'hello'
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    out_file = tmp_path / 'deep' / 'nested' / 'nested.txt'
    result = runner.invoke(app, ['get-asset', asset_path, '-o', str(out_file)], obj=mock_config)

    assert result.exit_code == 0, result.stdout
    assert out_file.read_bytes() == b'hello'


def test_get_asset_api_error(runner, mock_api, mock_config, monkeypatch):
    """Gracefully handle API errors."""
    asset_path = f'assets/memex/{uuid4()}/missing.png'
    mock_api.get_resource.side_effect = FileNotFoundError('not found')
    monkeypatch.setattr('memex_cli.notes.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['get-asset', asset_path], obj=mock_config)

    assert result.exit_code == 1
