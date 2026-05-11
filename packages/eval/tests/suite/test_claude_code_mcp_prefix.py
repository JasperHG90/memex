"""Claude Code MCP backend strips the ``mcp__<server>__`` prefix from tool names.

Without this, ToolCall* outcomes that compare against bare names like
``memex_memory_search`` would systematically fail under ``--answer-mode
claude-code`` because Claude Code's stream-json emits MCP tools as
``mcp__memex__memex_memory_search``.
"""

from __future__ import annotations

from memex_eval.suite.agents import (
    AgentAnswer,
    _absorb_claude_message,
    _strip_mcp_prefix,
)


class TestStripMcpPrefix:
    def test_strips_memex_prefix(self) -> None:
        assert _strip_mcp_prefix('mcp__memex__memex_memory_search') == 'memex_memory_search'

    def test_strips_other_server_prefix(self) -> None:
        assert _strip_mcp_prefix('mcp__github__create_issue') == 'create_issue'

    def test_passthrough_bare_name(self) -> None:
        assert _strip_mcp_prefix('memex_memory_search') == 'memex_memory_search'

    def test_passthrough_empty(self) -> None:
        assert _strip_mcp_prefix('') == ''

    def test_passthrough_malformed_prefix(self) -> None:
        assert _strip_mcp_prefix('mcp__no_tool_segment') == 'mcp__no_tool_segment'


class TestAbsorbAppliesStrip:
    def test_assistant_message_records_bare_tool_name(self) -> None:
        msg = {
            'type': 'assistant',
            'message': {
                'content': [
                    {
                        'type': 'tool_use',
                        'name': 'mcp__memex__memex_memory_search',
                        'input': {'query': 'project alpha'},
                    },
                ],
            },
        }
        out = AgentAnswer()
        _absorb_claude_message(msg, out)
        assert len(out.tool_calls) == 1
        assert out.tool_calls[0]['tool'] == 'memex_memory_search'
        assert out.tool_calls[0]['input'] == {'query': 'project alpha'}
