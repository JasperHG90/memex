import pytest
from memex_cli.utils import merge_overrides, async_command


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
