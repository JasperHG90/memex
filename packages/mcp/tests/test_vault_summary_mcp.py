"""Tests for vault summary MCP tools."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError

from helpers import parse_tool_result, TEST_VAULT_UUID
from memex_core.memory.sql_models import VaultSummary


def _make_summary(vault_id=None):
    now = datetime.now(timezone.utc)
    return VaultSummary(
        id=uuid4(),
        vault_id=vault_id or uuid4(),
        narrative='This vault tracks AI research and agent architecture.',
        themes=[
            {
                'name': 'AI',
                'description': 'AI research',
                'note_count': 5,
                'trend': 'growing',
                'last_addition': '2026-04-06',
                'representative_titles': ['ReAct paper'],
            }
        ],
        inventory={'total_notes': 5, 'total_entities': 3},
        key_entities=[{'name': 'Claude', 'type': 'product', 'mention_count': 10}],
        version=3,
        notes_incorporated=5,
        patch_log=[],
        created_at=now,
        updated_at=now,
    )


# ── memex_get_vault_summary ──


@pytest.mark.asyncio
async def test_get_vault_summary_returns_data(mock_api, mock_config, mcp_client):
    summary = _make_summary(TEST_VAULT_UUID)
    mock_api.get_vault_summary = AsyncMock(return_value=summary)

    result = await mcp_client.call_tool('memex_get_vault_summary', {})
    data = parse_tool_result(result)

    assert data['narrative'] == 'This vault tracks AI research and agent architecture.'
    assert data['vault_id'] == str(TEST_VAULT_UUID)
    assert data['version'] == 3
    assert data['notes_incorporated'] == 5
    assert len(data['themes']) == 1
    assert data['themes'][0]['trend'] == 'growing'
    assert data['inventory']['total_notes'] == 5
    assert len(data['key_entities']) == 1


@pytest.mark.asyncio
async def test_get_vault_summary_no_summary(mock_api, mock_config, mcp_client):
    mock_api.get_vault_summary = AsyncMock(return_value=None)

    result = await mcp_client.call_tool('memex_get_vault_summary', {})
    data = parse_tool_result(result)

    assert 'message' in data
    assert 'No summary' in data['message']


@pytest.mark.asyncio
async def test_get_vault_summary_with_vault_id(mock_api, mock_config, mcp_client):
    vid = uuid4()
    summary = _make_summary(vid)
    mock_api.get_vault_summary = AsyncMock(return_value=summary)
    mock_api.resolve_vault_identifier.return_value = vid

    result = await mcp_client.call_tool('memex_get_vault_summary', {'vault_id': str(vid)})
    data = parse_tool_result(result)

    assert data['vault_id'] == str(vid)


@pytest.mark.asyncio
async def test_get_vault_summary_error(mock_api, mock_config, mcp_client):
    mock_api.get_vault_summary = AsyncMock(side_effect=RuntimeError('DB error'))

    with pytest.raises(ToolError, match='DB error'):
        await mcp_client.call_tool('memex_get_vault_summary', {})
