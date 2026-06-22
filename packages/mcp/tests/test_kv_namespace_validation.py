"""Local validation of the KV namespace prefix on memex_kv_put.

The server (services/kv.py ``_validate_namespace``) rejects un-namespaced keys;
the MCP handler mirrors that gate client-side so the LLM sees a clean ToolError
instead of a round-trip HTTP 4xx.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError


def _make_ctx():
    ctx = MagicMock()
    ctx.session_id = 'test-session'
    return ctx


@pytest.mark.asyncio
async def test_kv_put_rejects_unnamespaced_key():
    """An un-namespaced key must surface as ToolError before reaching the API."""
    from memex_mcp.server import memex_kv_put

    mock_api = AsyncMock()
    ctx = _make_ctx()
    with patch('memex_mcp.server.get_api', return_value=mock_api):
        with pytest.raises(ToolError) as exc_info:
            await memex_kv_put(ctx=ctx, value='v', key='bogus:thing')

    assert 'must start with' in str(exc_info.value)
    mock_api.embed_text.assert_not_called()
    mock_api.kv_put.assert_not_called()


@pytest.mark.asyncio
async def test_kv_put_accepts_valid_namespace():
    """A correctly-namespaced key passes the guard and reaches the API."""
    from memex_mcp.server import memex_kv_put

    entry = MagicMock()
    entry.key = 'global:lang:python'
    entry.value = '3.12'
    entry.expires_at = None

    mock_api = AsyncMock()
    mock_api.embed_text = AsyncMock(return_value=[0.0])
    mock_api.kv_put = AsyncMock(return_value=entry)
    ctx = _make_ctx()

    with patch('memex_mcp.server.get_api', return_value=mock_api):
        result = await memex_kv_put(ctx=ctx, value='3.12', key='global:lang:python')

    assert result.key == 'global:lang:python'
    mock_api.kv_put.assert_awaited_once()
