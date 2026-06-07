from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from memex_cli.session import app


@pytest.fixture
def _patched_api(mock_config):
    api = AsyncMock()
    api.__aenter__.return_value = api
    api.__aexit__.return_value = None
    with (
        patch('memex_cli.session.get_api_context', return_value=api),
        patch('memex_cli.utils.MemexConfig'),
    ):
        yield api


def _invoke(runner, mock_config, args):
    return runner.invoke(app, args, obj=mock_config)


def test_briefing_success_writes_markdown_to_stdout(runner, mock_config, _patched_api):
    _patched_api.resolve_vault_identifier.return_value = uuid4()
    _patched_api.get_session_briefing.return_value = '# Briefing\n\nfacts'

    result = _invoke(runner, mock_config, ['--budget', '1000'])

    assert result.exit_code == 0
    assert '# Briefing' in result.stdout


def test_briefing_invalid_budget_exits_2_to_stderr(runner, mock_config, _patched_api):
    result = _invoke(runner, mock_config, ['--budget', '500'])

    assert result.exit_code == 2
    assert '--budget must be one of' in result.output


def test_briefing_connection_error_is_plain_stderr(runner, mock_config, _patched_api):
    _patched_api.resolve_vault_identifier.side_effect = httpx.ConnectError('refused')

    result = _invoke(runner, mock_config, [])

    assert result.exit_code == 1
    # Plain text for the hook consumer — no rich markup, no stdout briefing.
    assert 'Could not reach the Memex server' in result.output
    assert 'memex server start' not in result.output


def test_briefing_http_error_is_plain_stderr(runner, mock_config, _patched_api):
    _patched_api.resolve_vault_identifier.return_value = uuid4()
    response = httpx.Response(500, request=httpx.Request('GET', 'http://x'))
    _patched_api.get_session_briefing.side_effect = httpx.HTTPStatusError(
        'boom', request=response.request, response=response
    )

    result = _invoke(runner, mock_config, [])

    assert result.exit_code == 1
    assert 'returned 500' in result.output
