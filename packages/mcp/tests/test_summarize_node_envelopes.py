"""MCP `memex_memory_summarize_node` envelope translation tests.

Validates that the MCP tool translates client-side exceptions
(``RateLimitExceeded`` and ``ReflectionAbandoned``) into structured
envelopes the agent can programmatically interpret. The actual
reflection logic is exercised in `packages/core/tests/.../reflect/`;
this test isolates the surface contract.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastmcp import Client

from memex_common.client import RateLimitExceeded, ReflectionAbandoned
from memex_mcp.server import mcp


@pytest.mark.asyncio
async def test_summarize_node_returns_reflection_abandoned_envelope(mock_api):
    """V18 round-7: a ``ReflectionAbandoned`` raised by the client lib
    translates to a structured envelope (not a generic ToolError).
    """
    entity_id = str(uuid4())
    mock_api.summarize_node.side_effect = ReflectionAbandoned(
        retry_after_seconds=60.0,
        message=f'Reflection for entity {entity_id} abandoned.',
        hint='Prefer re-reading via memex_get_entity.',
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_memory_summarize_node',
            {'entity_id': entity_id, 'scope': 'incremental'},
        )

    payload = json.loads(result.content[0].text)
    assert payload['error'] == 'reflection_abandoned'
    assert payload['entity_id'] == entity_id
    assert payload['retry_after_seconds'] == 60.0
    assert 'abandoned' in payload['message'].lower()
    assert 'hint' in payload
    assert 'memex_get_entity' in payload['hint']


@pytest.mark.asyncio
async def test_summarize_node_returns_rate_limit_envelope(mock_api):
    """Round-7 regression guard: ``RateLimitExceeded`` keeps returning
    its existing envelope shape unchanged by the round-6/7 abandon work.
    """
    entity_id = str(uuid4())
    mock_api.summarize_node.side_effect = RateLimitExceeded(
        retry_after_seconds=42.5,
        message=f'Rate limit exceeded for entity {entity_id}.',
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            'memex_memory_summarize_node',
            {'entity_id': entity_id, 'scope': 'incremental'},
        )

    payload = json.loads(result.content[0].text)
    assert payload['error'] == 'rate_limit_exceeded'
    assert payload['entity_id'] == entity_id
    assert payload['retry_after_seconds'] == 42.5
    assert 'rate limit' in payload['message'].lower()
