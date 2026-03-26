"""Tests for MCP template tools (memex_get_template, memex_list_templates, memex_register_template)."""

import pathlib

import pytest
import tomli_w
from unittest.mock import MagicMock, patch

from fastmcp import Client
from fastmcp.exceptions import ToolError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template_config(tmp_path: pathlib.Path):
    """Mock config whose filestore root points to a temp directory."""
    config = MagicMock()
    config.write_vault = 'my-project'
    config.read_vaults = ['my-project']
    config.server.default_active_vault = 'global'
    config.server.default_reader_vault = 'global'
    config.server.file_store.root = str(tmp_path)
    with patch('memex_mcp.server.get_config', return_value=config):
        yield config


@pytest.fixture
def s3_config():
    """Mock config with an S3 filestore root."""
    config = MagicMock()
    config.write_vault = 'my-project'
    config.read_vaults = ['my-project']
    config.server.default_active_vault = 'global'
    config.server.default_reader_vault = 'global'
    config.server.file_store.root = 's3://my-bucket/memex'
    with patch('memex_mcp.server.get_config', return_value=config):
        yield config


def _write_toml_template(
    directory: pathlib.Path,
    slug: str,
    template: str = '---\ndate: 2025-01-01\n---\n# Test',
    name: str | None = None,
    description: str | None = None,
) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {}
    if name:
        data['name'] = name
    if description:
        data['description'] = description
    data['template'] = template
    path = directory / f'{slug}.toml'
    path.write_bytes(tomli_w.dumps(data).encode('utf-8'))
    return path


# ---------------------------------------------------------------------------
# memex_get_template
# ---------------------------------------------------------------------------


class TestGetTemplate:
    @pytest.mark.asyncio
    async def test_get_builtin_template(self, template_config, mcp_client: Client) -> None:
        result = await mcp_client.call_tool('memex_get_template', {'type': 'general_note'})
        text = result.content[0].text
        assert 'Executive Summary' in text

    @pytest.mark.asyncio
    async def test_get_unknown_template_errors(self, template_config, mcp_client: Client) -> None:
        with pytest.raises(ToolError, match='Unknown template'):
            await mcp_client.call_tool('memex_get_template', {'type': 'nonexistent'})

    @pytest.mark.asyncio
    async def test_get_user_template(
        self, template_config, mcp_client: Client, tmp_path: pathlib.Path
    ) -> None:
        templates_dir = pathlib.Path(template_config.server.file_store.root) / 'templates'
        _write_toml_template(
            templates_dir, 'standup', template='---\ndate: 2025-01-01\n---\n# Daily Standup'
        )
        result = await mcp_client.call_tool('memex_get_template', {'type': 'standup'})
        text = result.content[0].text
        assert 'Daily Standup' in text

    @pytest.mark.asyncio
    async def test_user_template_overrides_builtin(
        self, template_config, mcp_client: Client
    ) -> None:
        templates_dir = pathlib.Path(template_config.server.file_store.root) / 'templates'
        _write_toml_template(
            templates_dir,
            'general_note',
            template='---\ndate: 2025-01-01\n---\n# My Custom Note',
        )
        result = await mcp_client.call_tool('memex_get_template', {'type': 'general_note'})
        text = result.content[0].text
        assert 'My Custom Note' in text


# ---------------------------------------------------------------------------
# memex_list_templates
# ---------------------------------------------------------------------------


class TestListTemplates:
    @pytest.mark.asyncio
    async def test_lists_all_builtins(self, template_config, mcp_client: Client) -> None:
        result = await mcp_client.call_tool('memex_list_templates', {})
        text = result.content[0].text
        assert 'general_note' in text
        assert 'technical_brief' in text
        assert 'quick_note' in text
        assert '[builtin]' in text

    @pytest.mark.asyncio
    async def test_includes_user_templates(self, template_config, mcp_client: Client) -> None:
        templates_dir = pathlib.Path(template_config.server.file_store.root) / 'templates'
        _write_toml_template(templates_dir, 'sprint_retro', name='Sprint Retro')
        result = await mcp_client.call_tool('memex_list_templates', {})
        text = result.content[0].text
        assert 'sprint_retro' in text
        assert '[global]' in text

    @pytest.mark.asyncio
    async def test_s3_root_skips_global(self, s3_config, mcp_client: Client) -> None:
        """With S3 filestore, global templates are skipped but builtins still work."""
        result = await mcp_client.call_tool('memex_list_templates', {})
        text = result.content[0].text
        assert 'general_note' in text
        assert '[builtin]' in text


# ---------------------------------------------------------------------------
# memex_register_template
# ---------------------------------------------------------------------------


class TestRegisterTemplate:
    @pytest.mark.asyncio
    async def test_register_from_content(self, template_config, mcp_client: Client) -> None:
        result = await mcp_client.call_tool(
            'memex_register_template',
            {
                'slug': 'daily_standup',
                'template': '---\ndate: 2025-01-01\n---\n# Standup',
                'name': 'Daily Standup',
                'description': 'Morning standup template',
            },
        )
        text = result.content[0].text
        assert 'Registered' in text
        assert 'daily_standup' in text

        # Verify it appears in list
        list_result = await mcp_client.call_tool('memex_list_templates', {})
        assert 'daily_standup' in list_result.content[0].text

        # Verify it can be retrieved
        get_result = await mcp_client.call_tool('memex_get_template', {'type': 'daily_standup'})
        assert '# Standup' in get_result.content[0].text

    @pytest.mark.asyncio
    async def test_register_minimal(self, template_config, mcp_client: Client) -> None:
        """Register with only required fields (slug + template)."""
        result = await mcp_client.call_tool(
            'memex_register_template',
            {
                'slug': 'quick',
                'template': '---\ndate: 2025-01-01\n---\n# Quick',
            },
        )
        text = result.content[0].text
        assert 'Registered' in text
