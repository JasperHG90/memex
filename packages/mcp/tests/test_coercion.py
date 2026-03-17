"""Tests for MCP parameter type coercion (BeforeValidator).

MCP clients sometimes stringify non-string parameters. These tests verify
that the BeforeValidator coercion functions handle stringified lists, bools,
and ints correctly — both at the unit level and through actual tool calls.
"""

import json
from uuid import uuid4

import pytest
from conftest import parse_tool_result

from memex_mcp.server import _coerce_bool, _coerce_int, _coerce_list


# ---------------------------------------------------------------------------
# Unit tests for coercion helpers
# ---------------------------------------------------------------------------


class TestCoerceList:
    def test_string_json_array(self):
        assert _coerce_list('["a", "b"]') == ['a', 'b']

    def test_string_empty_array(self):
        assert _coerce_list('[]') == []

    def test_already_list(self):
        assert _coerce_list(['a', 'b']) == ['a', 'b']

    def test_none_passthrough(self):
        assert _coerce_list(None) is None

    def test_non_json_string_passthrough(self):
        assert _coerce_list('not-json') == 'not-json'

    def test_json_object_not_coerced(self):
        """A stringified dict should not be coerced to a list."""
        assert _coerce_list('{"a": 1}') == '{"a": 1}'

    def test_nested_array(self):
        assert _coerce_list('[["a"], ["b"]]') == [['a'], ['b']]


class TestCoerceBool:
    def test_true_string(self):
        assert _coerce_bool('true') is True

    def test_false_string(self):
        assert _coerce_bool('false') is False

    def test_true_uppercase(self):
        assert _coerce_bool('True') is True

    def test_one_string(self):
        assert _coerce_bool('1') is True

    def test_zero_string(self):
        assert _coerce_bool('0') is False

    def test_already_bool(self):
        assert _coerce_bool(True) is True
        assert _coerce_bool(False) is False

    def test_none_passthrough(self):
        assert _coerce_bool(None) is None

    def test_non_bool_string_passthrough(self):
        assert _coerce_bool('yes') == 'yes'


class TestCoerceInt:
    def test_string_int(self):
        assert _coerce_int('10') == 10

    def test_string_zero(self):
        assert _coerce_int('0') == 0

    def test_negative_string(self):
        assert _coerce_int('-5') == -5

    def test_already_int(self):
        assert _coerce_int(42) == 42

    def test_none_passthrough(self):
        assert _coerce_int(None) is None

    def test_non_int_string_passthrough(self):
        assert _coerce_int('abc') == 'abc'

    def test_float_string_passthrough(self):
        """Float strings should not be coerced to int."""
        assert _coerce_int('3.14') == '3.14'


# ---------------------------------------------------------------------------
# Integration tests: stringified params through actual MCP tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_page_indices_stringified_list(mock_api, mcp_client):
    """note_ids passed as a stringified JSON array should be coerced."""
    doc_id = uuid4()
    mock_api.get_note_page_index.return_value = {
        'metadata': {'title': 'Test', 'description': 'desc'},
        'toc': [],
    }

    result = await mcp_client.call_tool(
        'memex_get_page_indices',
        {'note_ids': json.dumps([str(doc_id)])},
    )
    data = parse_tool_result(result)
    assert isinstance(data, list)
    mock_api.get_note_page_index.assert_called_once_with(doc_id)


@pytest.mark.asyncio
async def test_get_nodes_stringified_list(mock_api, mcp_client):
    """node_ids passed as a stringified JSON array should be coerced."""
    node_id = uuid4()
    mock_api.get_node.return_value = type(
        'NodeDTO',
        (),
        {
            'id': node_id,
            'note_id': uuid4(),
            'vault_id': uuid4(),
            'title': 'Section',
            'text': 'body',
            'level': 1,
            'seq': 0,
        },
    )()

    result = await mcp_client.call_tool(
        'memex_get_nodes',
        {'node_ids': json.dumps([str(node_id)])},
    )
    data = parse_tool_result(result)
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_notes_metadata_stringified_list(mock_api, mcp_client):
    """note_ids passed as a stringified JSON array should be coerced."""
    note_id = uuid4()
    mock_api.get_note_metadata.return_value = {
        'title': 'Test',
        'description': 'desc',
        'status': 'active',
        'tags': [],
        'total_tokens': 100,
        'has_assets': False,
        'created_at': '2024-01-01T00:00:00Z',
    }

    result = await mcp_client.call_tool(
        'memex_get_notes_metadata',
        {'note_ids': json.dumps([str(note_id)])},
    )
    data = parse_tool_result(result)
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_entity_mentions_stringified_int(mock_api, mcp_client):
    """limit passed as a string should be coerced to int."""
    entity_id = uuid4()
    mock_api.get_entity_mentions.return_value = []

    result = await mcp_client.call_tool(
        'memex_get_entity_mentions',
        {'entity_id': str(entity_id), 'limit': '5'},
    )
    data = parse_tool_result(result)
    assert data == [] or data is None


@pytest.mark.asyncio
async def test_get_entities_stringified_list(mock_api, mcp_client):
    """entity_ids passed as a stringified JSON array should be coerced."""
    eid = uuid4()
    mock_api.get_entity.return_value = type(
        'Entity',
        (),
        {
            'id': eid,
            'name': 'TestEntity',
            'entity_type': 'Concept',
            'mention_count': 1,
        },
    )()

    result = await mcp_client.call_tool(
        'memex_get_entities',
        {'entity_ids': json.dumps([str(eid)])},
    )
    data = parse_tool_result(result)
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_entity_cooccurrences_stringified_int(mock_api, mcp_client):
    """limit passed as a string should be coerced to int."""
    entity_id = uuid4()
    mock_api.get_entity_cooccurrences.return_value = []

    result = await mcp_client.call_tool(
        'memex_get_entity_cooccurrences',
        {'entity_id': str(entity_id), 'limit': '3'},
    )
    data = parse_tool_result(result)
    assert data == [] or data is None
