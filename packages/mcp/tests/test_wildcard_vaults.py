"""Tests for the '*' wildcard vault resolution in MCP tools."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from fastmcp.exceptions import ToolError

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
