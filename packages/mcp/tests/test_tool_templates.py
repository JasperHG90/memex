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
        for slug in (
            'general_note',
            'technical_brief',
            'quick_note',
            'architectural_decision_record',
            'request_for_comments',
            'agent_reflection',
        ):
            assert slug in text, f'expected built-in {slug!r} in list output'
        assert '[builtin]' in text

    @pytest.mark.asyncio
    async def test_output_includes_action_hint(self, template_config, mcp_client: Client) -> None:
        """The list output ends with a closing hint pointing to the next call.

        This is the UX intervention that turns templates into a discoverable flow:
        agents see the list AND the next step in the same response.
        """
        result = await mcp_client.call_tool('memex_list_templates', {})
        text = result.content[0].text
        assert 'memex_get_template(slug)' in text
        assert 'memex_add_note' in text
        assert 'template=slug' in text

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


# ---------------------------------------------------------------------------
# Live LLM check: server instructions + tool descriptions actually steer the
# model to the templates flow when capturing structured content.
#
# Static schema assertions don't prove model obedience — only a real turn does.
# Mirrors the pattern in
# packages/hermes-plugin/tests/test_tools.py::test_retain_content_is_structured_markdown_via_gemini.
# ---------------------------------------------------------------------------

import importlib.util as _ilu  # noqa: E402
import json as _json  # noqa: E402
import os as _os  # noqa: E402

_HAS_GEMINI_KEY = bool(_os.environ.get('GEMINI_API_KEY') or _os.environ.get('GOOGLE_API_KEY'))
_HAS_LITELLM = _ilu.find_spec('litellm') is not None


@pytest.mark.llm
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_mcp_routes_structured_capture_to_templates_via_gemini() -> None:
    """An LLM agent given the MCP server instructions + tool descriptions must
    reach for templates when asked to capture structured content (e.g. an ADR).

    This is the contract behind the discoverability fix: descriptions and routing
    are not just text changes — they have to actually shift behavior. We pin Gemini
    to match the rest of the repo's LLM-gated tests; any model strong enough to
    follow tool schemas will do.
    """
    import asyncio

    import litellm

    from memex_mcp.server import mcp

    async def _collect():
        names = ('memex_list_templates', 'memex_get_template', 'memex_add_note')
        tools = [await mcp.get_tool(n) for n in names]
        return tools, mcp.instructions

    tools_raw, instructions = asyncio.run(_collect())
    tool_defs = [
        {
            'type': 'function',
            'function': {
                'name': t.name,
                'description': t.description,
                'parameters': t.parameters,
            },
        }
        for t in tools_raw
    ]

    resp = litellm.completion(
        model='gemini/gemini-3-flash-preview',
        messages=[
            {'role': 'system', 'content': instructions},
            {
                'role': 'user',
                'content': (
                    'We need to record a permanent architectural decision: we will '
                    'use Postgres advisory locks for leader election across our '
                    'background-worker fleet. Context: exactly-once execution is '
                    'required. Alternatives considered: Redis locks (rejected — '
                    'extra dependency) and etcd (rejected — operational weight). '
                    'Save this to memex now in the my-project vault.'
                ),
            },
        ],
        tools=tool_defs,
        temperature=0,
        timeout=30,
        api_key=_os.environ.get('GEMINI_API_KEY') or _os.environ.get('GOOGLE_API_KEY'),
    )

    tool_calls = resp.choices[0].message.tool_calls or []
    assert tool_calls, f'model did not call any tool: {resp.choices[0].message!r}'

    first = tool_calls[0]
    name = first.function.name
    args = _json.loads(first.function.arguments or '{}')

    # Three behaviors prove the routing worked:
    #   1. memex_list_templates() — agent is exploring templates first
    #   2. memex_get_template('architectural_decision_record') — direct fetch
    #   3. memex_add_note(template='architectural_decision_record', ...) — direct
    #      provenance-tagged write (still proves agent picked up the slug)
    if name == 'memex_list_templates':
        return
    if name == 'memex_get_template':
        slug = args.get('type') or args.get('slug')
        assert slug == 'architectural_decision_record', (
            f'agent fetched template {slug!r}; expected architectural_decision_record'
        )
        return
    if name == 'memex_add_note':
        template = args.get('template')
        assert template == 'architectural_decision_record', (
            f'agent called memex_add_note without (or with wrong) template '
            f'(template={template!r}). The whole point of the routing fix is that '
            f'a structured capture should reach for templates. Tool call args: {args!r}'
        )
        return

    pytest.fail(
        f'agent called unexpected tool {name!r}; expected one of '
        f'memex_list_templates / memex_get_template / memex_add_note. '
        f'All tool calls: {[(tc.function.name, tc.function.arguments) for tc in tool_calls]!r}'
    )
