"""Tests for the memex_record_outcome MCP tool.

Validates parameter handling, vault resolution, and response formatting
for the outcome recording MCP endpoint.
"""

import pytest
from uuid import uuid4

from helpers import parse_tool_result, TEST_VAULT_UUID


@pytest.mark.asyncio
async def test_mcp_record_outcome_success(mock_api, mcp_client):
    mock_api.record_outcome.return_value = {
        'units_updated': 2,
        'entities_updated': 1,
        'models_updated': 1,
    }

    unit_ids = [str(uuid4()), str(uuid4())]
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': unit_ids,
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 2
    assert data['entities_updated'] == 1
    assert data['models_updated'] == 1
    mock_api.record_outcome.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_record_outcome_failure(mock_api, mcp_client):
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': False,
            'vault_id': str(TEST_VAULT_UUID),
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 1


@pytest.mark.asyncio
async def test_mcp_record_outcome_with_reason(mock_api, mcp_client):
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
            'reason': 'User confirmed this was helpful',
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 1
    # Verify reason was forwarded
    call_kwargs = mock_api.record_outcome.call_args
    assert call_kwargs.kwargs.get('reason') == 'User confirmed this was helpful'


@pytest.mark.asyncio
async def test_mcp_record_outcome_with_confidence(mock_api, mcp_client):
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
            'outcome_confidence': 0.8,
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 1


@pytest.mark.asyncio
async def test_mcp_record_outcome_vault_scoping(mock_api, mcp_client):
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 1
    # Verify vault_id was resolved and passed through
    call_kwargs = mock_api.record_outcome.call_args
    assert call_kwargs.kwargs.get('vault_id') == str(TEST_VAULT_UUID)


@pytest.mark.asyncio
async def test_mcp_record_outcome_default_vault(mock_api, mcp_client, mock_config):
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    result = await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
        },
    )

    data = parse_tool_result(result)
    assert data['units_updated'] == 1
    # Verify default write vault was used (no explicit vault_id)
    call_kwargs = mock_api.record_outcome.call_args
    assert call_kwargs.kwargs.get('vault_id') is not None


@pytest.mark.asyncio
async def test_mcp_record_outcome_no_retrieved_set_size_no_auto_fill(mock_api, mcp_client):
    """Omitting retrieved_set_size leaves it as None — no session-derived fallback.

    Cumulative session seen_memory_ids would conflate retrievals across calls
    and produce misleading coverage_ratio values, so the handler must pass the
    caller's value through unchanged (None when absent).
    """
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
        },
    )

    call_kwargs = mock_api.record_outcome.call_args
    assert call_kwargs.kwargs.get('retrieved_set_size') is None


@pytest.mark.asyncio
async def test_mcp_record_outcome_explicit_retrieved_set_size_passthrough(mock_api, mcp_client):
    """Caller-supplied retrieved_set_size is forwarded verbatim."""
    mock_api.record_outcome.return_value = {
        'units_updated': 1,
        'entities_updated': 0,
        'models_updated': 0,
    }

    unit_id = str(uuid4())
    await mcp_client.call_tool(
        'memex_record_outcome',
        {
            'unit_ids': [unit_id],
            'success': True,
            'vault_id': str(TEST_VAULT_UUID),
            'retrieved_set_size': 10,
        },
    )

    call_kwargs = mock_api.record_outcome.call_args
    assert call_kwargs.kwargs.get('retrieved_set_size') == 10
