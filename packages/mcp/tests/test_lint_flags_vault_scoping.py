"""``memex_get_lint_flags`` MCP surface vault scoping.

The MCP tool defaults to the session active write vault when no
``vault_id`` is supplied — never falls through to a global view.
"""

from __future__ import annotations

import pytest

from helpers import parse_tool_result, TEST_VAULT_UUID


@pytest.mark.asyncio
async def test_lint_flags_defaults_to_active_write_vault_when_omitted(
    mock_api, mcp_client, mock_config
):
    """Omitting vault_id MUST scope the call to the session
    active write vault, NOT trigger a global all-vault query.

    The conftest mock_config fixture sets ``write_vault='my-project'``,
    and resolve_vault_identifier returns TEST_VAULT_UUID. We assert the
    forwarded vault_id ends up resolved (non-None) — proving the tool
    refused to fall through to a global lookup.
    """
    mock_api.lint_get_flags.return_value = {'findings': [], 'next_cursor': None}

    result = await mcp_client.call_tool('memex_get_lint_flags', {})

    data = parse_tool_result(result)
    assert data == {'findings': [], 'next_cursor': None}
    mock_api.lint_get_flags.assert_called_once()
    forwarded = mock_api.lint_get_flags.call_args.kwargs.get('vault_id')
    assert forwarded is not None, (
        'memex_get_lint_flags must scope to the active write vault when '
        'vault_id is omitted; forwarded vault_id was None (global fallthrough).'
    )
    assert forwarded == str(TEST_VAULT_UUID)


@pytest.mark.asyncio
async def test_lint_flags_scopes_to_explicit_vault_id(mock_api, mcp_client, mock_config):
    """When the agent supplies vault_id, the tool resolves and forwards it
    verbatim.
    """
    mock_api.lint_get_flags.return_value = {'findings': [], 'next_cursor': None}

    result = await mcp_client.call_tool('memex_get_lint_flags', {'vault_id': str(TEST_VAULT_UUID)})

    data = parse_tool_result(result)
    assert data == {'findings': [], 'next_cursor': None}
    forwarded = mock_api.lint_get_flags.call_args.kwargs.get('vault_id')
    assert forwarded == str(TEST_VAULT_UUID)
