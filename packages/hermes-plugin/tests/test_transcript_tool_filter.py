"""Unit tests for ``_normalize_messages`` tool-output filtering.

The ``_normalize_messages`` function in
``memex_hermes_plugin.memex.transcript`` converts raw Hermes
``{role, content}`` messages into ``{user, assistant}`` pairs.
Tool-related messages must be dropped:

- Messages with ``role='tool'`` are dropped entirely.
- Content lists with ``type='tool_use'`` blocks are filtered out.
- Content lists with ``type='tool_result'`` blocks are filtered out.
- Plain text content (string) is preserved.
- Content list with mixed text + tool_use keeps only text.
"""

from __future__ import annotations

from typing import Any

from memex_hermes_plugin.memex.transcript import _normalize_messages


class TestToolRoleDropped:
    """Messages with role='tool' are entirely dropped."""

    def test_tool_role_message_is_dropped(self) -> None:
        messages = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'tool', 'content': 'tool output here'},
            {'role': 'assistant', 'content': 'response'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['user'] == 'hello'
        assert result[0]['assistant'] == 'response'

    def test_multiple_tool_messages_are_dropped(self) -> None:
        messages = [
            {'role': 'user', 'content': 'q1'},
            {'role': 'tool', 'content': 'result1'},
            {'role': 'tool', 'content': 'result2'},
            {'role': 'assistant', 'content': 'a1'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['user'] == 'q1'
        assert result[0]['assistant'] == 'a1'


class TestToolUseContentFiltered:
    """Content lists with type='tool_use' blocks are filtered out."""

    def test_tool_use_block_filtered_from_content_list(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                'role': 'assistant',
                'content': [
                    {'type': 'tool_use', 'name': 'search', 'input': {'q': 'test'}},
                ],
            },
            {'role': 'user', 'content': 'what did you find?'},
        ]
        result = _normalize_messages(messages)
        # The assistant message had only a tool_use block → empty content.
        # The user message should still be captured.
        # Depending on pairing logic, we may get 1-2 pairs.
        # The assistant's text content should be empty.
        found_assistant = False
        for pair in result:
            if pair['assistant']:
                found_assistant = True
        # No substantive assistant text from a tool_use-only message.
        assert not found_assistant or all(p['assistant'] == '' for p in result if not p['user'])


class TestToolResultContentFiltered:
    """Content lists with type='tool_result' blocks are filtered out."""

    def test_tool_result_block_filtered(self) -> None:
        messages: list[dict[str, Any]] = [
            {'role': 'user', 'content': 'search for X'},
            {
                'role': 'assistant',
                'content': [
                    {'type': 'tool_result', 'content': 'search results here'},
                ],
            },
        ]
        result = _normalize_messages(messages)
        # The assistant content should be empty since tool_result is filtered.
        for pair in result:
            if pair.get('assistant'):
                # tool_result should not appear as text
                assert 'search results here' not in pair['assistant']


class TestPlainTextContentPreserved:
    """Plain text content (string) is preserved intact."""

    def test_string_content_preserved(self) -> None:
        messages = [
            {'role': 'user', 'content': 'hello world'},
            {'role': 'assistant', 'content': 'hi there'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['user'] == 'hello world'
        assert result[0]['assistant'] == 'hi there'

    def test_empty_string_content_preserved(self) -> None:
        messages = [
            {'role': 'user', 'content': ''},
            {'role': 'assistant', 'content': 'response'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['assistant'] == 'response'


class TestMixedContentKeepsOnlyText:
    """Content list with mixed text + tool_use keeps only text blocks."""

    def test_mixed_content_keeps_text_drops_tool_use(self) -> None:
        messages = [
            {
                'role': 'assistant',
                'content': [
                    {'type': 'text', 'text': 'Let me search for that.'},
                    {'type': 'tool_use', 'name': 'search', 'input': {'q': 'test'}},
                    {'type': 'text', 'text': 'Here are the results.'},
                ],
            },
        ]
        result = _normalize_messages(messages)
        # The text blocks should be joined; the tool_use block should be filtered.
        assistant_text = ''
        for pair in result:
            if pair['assistant']:
                assistant_text += pair['assistant']
        assert 'Let me search for that.' in assistant_text
        assert 'Here are the results.' in assistant_text

    def test_content_list_with_only_text_blocks(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Part 1.'},
                    {'type': 'text', 'text': 'Part 2.'},
                ],
            },
            {'role': 'assistant', 'content': 'ok'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert 'Part 1.' in result[0]['user']
        assert 'Part 2.' in result[0]['user']


class TestPreexistingPairFormat:
    """Messages already in {user, assistant} format pass through."""

    def test_pair_format_passthrough(self) -> None:
        messages = [
            {'user': 'question', 'assistant': 'answer'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['user'] == 'question'
        assert result[0]['assistant'] == 'answer'


class TestUserWithoutAssistantResponse:
    """A trailing user message without an assistant response gets empty assistant."""

    def test_trailing_user_message(self) -> None:
        messages = [
            {'role': 'user', 'content': 'last question'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 1
        assert result[0]['user'] == 'last question'
        assert result[0]['assistant'] == ''

    def test_consecutive_user_messages(self) -> None:
        """Two consecutive user messages → first gets empty assistant."""
        messages = [
            {'role': 'user', 'content': 'first'},
            {'role': 'user', 'content': 'second'},
            {'role': 'assistant', 'content': 'reply to second'},
        ]
        result = _normalize_messages(messages)
        assert len(result) == 2
        assert result[0]['user'] == 'first'
        assert result[0]['assistant'] == ''
        assert result[1]['user'] == 'second'
        assert result[1]['assistant'] == 'reply to second'
