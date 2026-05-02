"""Local validation of intent_class / risk_class on memex_memory_search (issue #92).

The CLI and Hermes plugin validate against ``VALID_INTENT_CLASSES`` /
``VALID_RISK_CLASSES`` before the API call. The MCP server does the same so
tool consumers see a clean ``ToolError`` instead of an HTTP 422 from the
Pydantic-driven server boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError


def _make_ctx():
    ctx = MagicMock()
    ctx.session_id = 'test-session'
    return ctx


@pytest.mark.asyncio
async def test_memory_search_invalid_intent_class_raises_tool_error():
    """An unknown intent_class must surface as ToolError before reaching the API."""
    from memex_mcp.server import memex_memory_search

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[])
    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memex_memory_search(ctx=ctx, query='q', intent_class='bogus')

    assert 'Invalid intent_class' in str(exc_info.value)
    mock_api.search.assert_not_called()


@pytest.mark.asyncio
async def test_memory_search_invalid_risk_class_raises_tool_error():
    """An unknown risk_class must surface as ToolError before reaching the API."""
    from memex_mcp.server import memex_memory_search

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[])
    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memex_memory_search(ctx=ctx, query='q', risk_class='nope')

    assert 'Invalid risk_class' in str(exc_info.value)
    mock_api.search.assert_not_called()


@pytest.mark.asyncio
async def test_memory_search_valid_intent_class_passes_through():
    """A valid intent_class must be coerced to IntentClass before reaching api.search."""
    from memex_common.schemas import IntentClass
    from memex_mcp.server import memex_memory_search

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[])
    mock_api.get_notes_metadata = AsyncMock(return_value=[])
    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        await memex_memory_search(ctx=ctx, query='q', intent_class='ephemeral')

    mock_api.search.assert_called_once()
    kwargs = mock_api.search.call_args.kwargs
    assert kwargs['intent_class'] == IntentClass.EPHEMERAL


@pytest.mark.asyncio
async def test_memory_search_valid_risk_class_passes_through():
    """A valid risk_class must be coerced to RiskClass before reaching api.search."""
    from memex_common.schemas import RiskClass
    from memex_mcp.server import memex_memory_search

    mock_api = AsyncMock()
    mock_api.search = AsyncMock(return_value=[])
    mock_api.get_notes_metadata = AsyncMock(return_value=[])
    ctx = _make_ctx()

    with (
        patch('memex_mcp.server.get_api', return_value=mock_api),
        patch('memex_mcp.server._default_read_vaults', return_value=['vault-1']),
        patch('memex_mcp.server._validate_vault_ids'),
        patch('memex_mcp.server._resolve_vault_ids', new_callable=AsyncMock, return_value=['vid']),
    ):
        await memex_memory_search(ctx=ctx, query='q', risk_class='sensitive')

    mock_api.search.assert_called_once()
    kwargs = mock_api.search.call_args.kwargs
    assert kwargs['risk_class'] == RiskClass.SENSITIVE
