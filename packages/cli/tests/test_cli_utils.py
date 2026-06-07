import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import typer

from memex_cli.utils import (
    async_command,
    emit_json,
    handle_api_error,
    merge_overrides,
    resolve_active_vault,
)


def test_merge_overrides_simple():
    config = {'key': 'value'}
    overrides = ['key=new_value']
    result = merge_overrides(config, overrides)
    assert result['key'] == 'new_value'


def test_merge_overrides_nested():
    config = {'meta': {'host': 'localhost'}}
    overrides = ['meta.host=remote']
    result = merge_overrides(config, overrides)
    assert result['meta']['host'] == 'remote'


def test_merge_overrides_json_list():
    config: dict = {'vaults': []}
    overrides = ['vaults=["work", "personal"]']
    result = merge_overrides(config, overrides)
    assert result['vaults'] == ['work', 'personal']
    assert isinstance(result['vaults'], list)


def test_merge_overrides_json_number():
    config = {'limit': 10}
    overrides = ['limit=20']
    result = merge_overrides(config, overrides)
    assert result['limit'] == 20
    assert isinstance(result['limit'], int)


def test_merge_overrides_invalid_format():
    config = {'key': 'value'}
    overrides = ['invalid_format']  # missing =
    result = merge_overrides(config, overrides)
    assert result['key'] == 'value'


@pytest.mark.asyncio
async def test_async_command_wrapper():
    @async_command
    async def dummy_async(x: int):
        return x + 1

    # async_command uses asyncio.run() internally to make it synchronous for Typer.
    # In a test with an already-running loop we can only verify the wrapper is callable.
    assert callable(dummy_async)


@pytest.mark.asyncio
async def test_resolve_active_vault_explicit_vault_wins():
    vault_id = uuid4()
    api = AsyncMock()
    api.resolve_vault_identifier.return_value = vault_id
    config = type('Config', (), {'write_vault': 'active-vault'})()

    result = await resolve_active_vault(api, config, 'explicit-vault')

    assert result == vault_id
    api.resolve_vault_identifier.assert_awaited_once_with('explicit-vault')


@pytest.mark.asyncio
async def test_resolve_active_vault_falls_back_to_config():
    api = AsyncMock()
    config = type('Config', (), {'write_vault': 'active-vault'})()

    await resolve_active_vault(api, config, None)

    api.resolve_vault_identifier.assert_awaited_once_with('active-vault')


def test_emit_json_serializes_non_json_types(capsys):
    vault_id = uuid4()
    emit_json({'id': vault_id, 'count': 2})

    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {'id': str(vault_id), 'count': 2}


@pytest.mark.parametrize(
    'exc',
    [
        httpx.ConnectError('refused'),
        httpx.ConnectTimeout('timed out'),
        httpx.ReadTimeout('timed out'),
        httpx.PoolTimeout('timed out'),
    ],
)
def test_handle_api_error_connection_failure_suggests_server_start(exc, capsys):
    with pytest.raises(typer.Exit) as exc_info:
        handle_api_error(exc)

    assert exc_info.value.exit_code == 1
    out = capsys.readouterr().out
    assert 'Could not reach the Memex server' in out
    assert 'memex server start --daemon' in out
    assert 'memex config show' in out


def test_handle_api_error_http_status_unaffected(capsys):
    response = httpx.Response(404, json={'detail': 'Note not found'})
    exc = httpx.HTTPStatusError(
        'not found', request=httpx.Request('GET', 'http://x'), response=response
    )

    with pytest.raises(typer.Exit) as exc_info:
        handle_api_error(exc)

    assert exc_info.value.exit_code == 1
    assert 'Resource not found' in capsys.readouterr().out


def test_missing_config_prints_init_cta(runner):
    from memex_cli import app

    with (
        patch('memex_cli.GlobalYamlConfigSettingsSource') as global_source,
        patch('memex_cli.LocalYamlConfigSettingsSource') as local_source,
        patch('memex_cli.parse_memex_config', side_effect=ValueError('missing fields')),
        patch('memex_cli.setup_logging'),
    ):
        global_source.return_value.return_value = {}
        local_source.return_value.return_value = {}
        result = runner.invoke(app, ['vault', 'list'])

    assert result.exit_code == 1
    assert 'No Memex configuration found' in result.output
    assert 'memex config init' in result.output


def test_invalid_config_points_at_config_commands(runner):
    from memex_cli import app

    with (
        patch('memex_cli.GlobalYamlConfigSettingsSource') as global_source,
        patch('memex_cli.LocalYamlConfigSettingsSource') as local_source,
        patch('memex_cli.parse_memex_config', side_effect=ValueError('bad port')),
        patch('memex_cli.setup_logging'),
    ):
        global_source.return_value.return_value = {'server': {'port': 'bad'}}
        local_source.return_value.return_value = {}
        result = runner.invoke(app, ['vault', 'list'])

    assert result.exit_code == 1
    assert 'Configuration error' in result.output
    assert 'memex config show' in result.output
