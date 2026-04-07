"""Tests for the '*' wildcard vault resolution in MCP tools."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from helpers import parse_tool_result
from fastmcp.exceptions import ToolError

from memex_common.schemas import MemoryUnitDTO, FactTypes
from memex_mcp.server import _resolve_vault_id, _resolve_vault_ids, _validate_vault_ids


class TestValidateVaultIds:
    """Ensure '*' passes validation without error."""

    def test_wildcard_passes(self):
        result = _validate_vault_ids(['*'])
        assert result == ['*']

    def test_wildcard_with_others_passes(self):
        result = _validate_vault_ids(['*', 'my-vault'])
        assert result == ['*', 'my-vault']


class TestResolveVaultIdsWildcard:
    """Test _resolve_vault_ids with the '*' wildcard."""

    @pytest.mark.asyncio
    async def test_wildcard_returns_all_vaults(self):
        v1, v2, v3 = uuid4(), uuid4(), uuid4()
        api = AsyncMock()
        api.list_vaults = AsyncMock(
            return_value=[
                MagicMock(id=v1),
                MagicMock(id=v2),
                MagicMock(id=v3),
            ]
        )

        result = await _resolve_vault_ids(api, ['*'])

        assert result == [v1, v2, v3]
        api.list_vaults.assert_called_once()
        api.resolve_vault_identifier.assert_not_called()

    @pytest.mark.asyncio
    async def test_wildcard_with_other_names_still_returns_all(self):
        """Wildcard takes precedence — extra names are ignored."""
        v1 = uuid4()
        api = AsyncMock()
        api.list_vaults = AsyncMock(return_value=[MagicMock(id=v1)])

        result = await _resolve_vault_ids(api, ['*', 'some-vault'])

        assert result == [v1]
        api.resolve_vault_identifier.assert_not_called()

    @pytest.mark.asyncio
    async def test_without_wildcard_resolves_individually(self):
        v1 = uuid4()
        api = AsyncMock()
        api.resolve_vault_identifier = AsyncMock(return_value=v1)

        result = await _resolve_vault_ids(api, ['my-vault'])

        assert result == [v1]
        api.list_vaults.assert_not_called()
        api.resolve_vault_identifier.assert_called_once_with('my-vault')

    @pytest.mark.asyncio
    async def test_wildcard_empty_database_returns_empty(self):
        """Edge case: no vaults in the database returns an empty list."""
        api = AsyncMock()
        api.list_vaults = AsyncMock(return_value=[])

        result = await _resolve_vault_ids(api, ['*'])

        assert result == []


class TestResolveVaultIdWildcard:
    """Test _resolve_vault_id (singular) rejects '*' with a helpful error."""

    @pytest.mark.asyncio
    async def test_wildcard_raises_tool_error(self):
        api = AsyncMock()

        with pytest.raises(ToolError, match='not supported'):
            await _resolve_vault_id(api, '*')


class TestWildcardMcpToolIntegration:
    """End-to-end tests calling MCP tools with vault_ids=['*']."""

    @pytest.mark.asyncio
    async def test_memory_search_wildcard(self, mock_api, mock_config, mcp_client):
        """memex_memory_search with vault_ids=['*'] should list all vaults."""
        v1, v2 = uuid4(), uuid4()
        mock_api.list_vaults.return_value = [MagicMock(id=v1), MagicMock(id=v2)]
        mock_api.search.return_value = [
            MemoryUnitDTO(
                id=uuid4(),
                text='Found across vaults.',
                fact_type=FactTypes.WORLD,
                score=0.9,
                vault_id=v1,
                metadata={},
            )
        ]

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'test', 'vault_ids': ['*']}
        )

        data = parse_tool_result(result)
        assert len(data) == 1

        mock_api.list_vaults.assert_called_once()
        mock_api.resolve_vault_identifier.assert_not_called()
        call_args = mock_api.search.call_args[1]
        assert set(call_args['vault_ids']) == {v1, v2}

    @pytest.mark.asyncio
    async def test_note_search_wildcard(self, mock_api, mock_config, mcp_client):
        """memex_note_search with vault_ids=['*'] should list all vaults."""
        v1 = uuid4()
        mock_api.list_vaults.return_value = [MagicMock(id=v1)]
        mock_api.search_notes.return_value = []

        await mcp_client.call_tool('memex_note_search', {'query': 'test', 'vault_ids': ['*']})

        mock_api.list_vaults.assert_called_once()
        mock_api.resolve_vault_identifier.assert_not_called()
        call_args = mock_api.search_notes.call_args[1]
        assert call_args['vault_ids'] == [v1]

    @pytest.mark.asyncio
    async def test_find_note_wildcard(self, mock_api, mock_config, mcp_client):
        """memex_find_note with vault_ids=['*'] should list all vaults."""
        v1 = uuid4()
        mock_api.list_vaults.return_value = [MagicMock(id=v1)]
        mock_api.find_notes_by_title.return_value = []

        await mcp_client.call_tool('memex_find_note', {'query': 'test', 'vault_ids': ['*']})

        mock_api.list_vaults.assert_called_once()
        call_args = mock_api.find_notes_by_title.call_args[1]
        assert call_args['vault_ids'] == [v1]

    @pytest.mark.asyncio
    async def test_list_entities_wildcard_rejected(self, mock_api, mock_config, mcp_client):
        """memex_list_entities uses singular vault_id — '*' should error."""
        with pytest.raises(ToolError, match='not supported'):
            await mcp_client.call_tool('memex_list_entities', {'vault_id': '*'})
