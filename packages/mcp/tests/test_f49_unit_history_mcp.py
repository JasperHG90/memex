"""F49 — memex_get_unit_history MCP-level dispatch + serialization tests.

Verifies:
- Tool is registered with the expected description, tags, and signature.
- Dispatch resolves vault_id and forwards to api.get_unit_history.
- The serialized result is a JSON dict with the expected tree shape.
- Invalid input (bad UUID, negative max_depth, missing vault) -> ToolError.
- Vault scoping is enforced at the api boundary.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastmcp import Client

from memex_common.schemas import UnitHistoryNodeDTO
from memex_mcp.server import mcp
from helpers import parse_tool_result, TEST_VAULT_UUID


def _build_history(*, root_id: UUID, pred_id: UUID, vault_label: str = 'A') -> UnitHistoryNodeDTO:
    """Build a 2-level UnitHistoryNodeDTO for serialization tests."""
    pred = UnitHistoryNodeDTO(
        unit_id=pred_id,
        text=f'older claim {vault_label}',
        note_id=uuid4(),
        confidence=0.5,
        event_date=dt.datetime(2026, 4, 25, tzinfo=dt.timezone.utc),
        link_type='contradicts',
        link_metadata={'reasoning': 'newer overrides older'},
        depth=1,
        predecessors=[],
        truncated=False,
    )
    return UnitHistoryNodeDTO(
        unit_id=root_id,
        text=f'newest claim {vault_label}',
        note_id=uuid4(),
        confidence=1.0,
        event_date=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        link_type=None,
        link_metadata={},
        depth=0,
        predecessors=[pred],
        truncated=False,
    )


@pytest.mark.asyncio
async def test_tool_is_registered_with_expected_metadata():
    """The MCP tool registry surfaces F49 with the right tag + description."""
    tool = await mcp.get_tool('memex_get_unit_history')
    assert tool is not None, 'memex_get_unit_history must be registered'
    assert 'storage' in (tool.tags or set())
    assert 'supersession history' in tool.description.lower()
    assert 'forward=true' in tool.description.lower() or 'forward' in tool.description.lower()


@pytest.mark.asyncio
async def test_dispatch_returns_serialized_tree(mock_api):
    """A valid call returns the JSON-serialized UnitHistoryNodeDTO tree."""
    root_id = uuid4()
    pred_id = uuid4()
    history = _build_history(root_id=root_id, pred_id=pred_id)

    mock_api.get_unit_history = AsyncMock(return_value=history)
    mock_api.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_get_unit_history',
            {'unit_id': str(root_id), 'vault_id': 'test-vault', 'max_depth': 5},
        )
        data = parse_tool_result(result)

    assert isinstance(data, dict)
    assert data['unit_id'] == str(root_id)
    assert data['depth'] == 0
    assert data['link_type'] is None
    assert len(data['predecessors']) == 1

    pred = data['predecessors'][0]
    assert pred['unit_id'] == str(pred_id)
    assert pred['depth'] == 1
    assert pred['link_type'] == 'contradicts'
    assert pred['link_metadata'].get('reasoning') == 'newer overrides older'

    mock_api.get_unit_history.assert_awaited_once()
    call = mock_api.get_unit_history.await_args
    assert call is not None
    assert call.kwargs['vault_id'] == TEST_VAULT_UUID
    assert call.kwargs['max_depth'] == 5


@pytest.mark.asyncio
async def test_dispatch_default_max_depth(mock_api):
    """Omitting max_depth uses the default of 10."""
    root_id = uuid4()
    pred_id = uuid4()
    history = _build_history(root_id=root_id, pred_id=pred_id)
    mock_api.get_unit_history = AsyncMock(return_value=history)
    mock_api.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    async with Client(mcp) as client:
        await client.call_tool(
            'memex_get_unit_history',
            {'unit_id': str(root_id), 'vault_id': 'test-vault'},
        )

    call = mock_api.get_unit_history.await_args
    assert call is not None
    assert call.kwargs['max_depth'] == 10


@pytest.mark.asyncio
async def test_invalid_unit_id_raises_tool_error(mock_api):
    """Bad UUID -> ToolError surfaced via MCP error envelope."""
    mock_api.get_unit_history = AsyncMock()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                'memex_get_unit_history',
                {'unit_id': 'not-a-uuid', 'vault_id': 'test-vault'},
            )

    assert 'invalid' in str(exc_info.value).lower() or 'unit_id' in str(exc_info.value).lower()
    mock_api.get_unit_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_negative_max_depth_raises_tool_error(mock_api):
    """Negative max_depth -> ToolError before reaching the api."""
    mock_api.get_unit_history = AsyncMock()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=TEST_VAULT_UUID)

    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                'memex_get_unit_history',
                {'unit_id': str(uuid4()), 'vault_id': 'test-vault', 'max_depth': -1},
            )

    assert 'max_depth' in str(exc_info.value).lower()
    mock_api.get_unit_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_vault_raises_tool_error(mock_api):
    """Vault resolution failure -> ToolError."""
    mock_api.get_unit_history = AsyncMock()
    mock_api.resolve_vault_identifier = AsyncMock(side_effect=Exception('not found'))

    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                'memex_get_unit_history',
                {'unit_id': str(uuid4()), 'vault_id': 'unknown-vault'},
            )

    assert 'vault' in str(exc_info.value).lower()
    mock_api.get_unit_history.assert_not_awaited()
