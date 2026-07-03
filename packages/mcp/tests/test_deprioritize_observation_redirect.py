"""``memex_memory_deprioritize`` must surface the observation-redirect 400.

When the target ``unit_id`` is actually an ``Observation.id``, the server
returns a structured 400 (``ObservationReadOnlyError``) whose body carries
``source_memory_units`` so the agent can retry against the real MUs:

    {'detail': {'error': 'observations are read-only',
                'source_memory_units': [...]}}

httpx's ``raise_for_status`` flattens that body to a bare message string, so
the MCP tool must re-read ``exc.response`` and re-surface the source MUs in the
``ToolError`` — otherwise the reactive "deprioritize → read redirect → retry
against the source MU" contract the tool descriptions promise is dead through
MCP (only the proactive virtual-unit detection would work).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastmcp.exceptions import ToolError


def _http_400(body: dict | str) -> httpx.HTTPStatusError:
    request = httpx.Request('POST', 'http://test/api/v1/memories/x/deprioritize')
    if isinstance(body, str):
        response = httpx.Response(400, text=body, request=request)
    else:
        response = httpx.Response(400, json=body, request=request)
    return httpx.HTTPStatusError('Client error 400', request=request, response=response)


@pytest.mark.asyncio
async def test_deprioritize_observation_400_surfaces_source_mus(mock_api, mcp_client):
    """The redirect's source_memory_units reach the agent in the ToolError."""
    obs_id = uuid4()
    mu1, mu2 = uuid4(), uuid4()
    mock_api.deprioritize_memory_unit.side_effect = _http_400(
        {
            'detail': {
                'error': 'observations are read-only',
                'source_memory_units': [str(mu1), str(mu2)],
            }
        }
    )

    with pytest.raises(ToolError) as ei:
        await mcp_client.call_tool(
            'memex_memory_deprioritize',
            {'unit_id': str(obs_id), 'reason': 'stale', 'vault_id': str(uuid4())},
        )

    msg = str(ei.value)
    assert 'read-only observation' in msg
    # Both retry targets must be present so the agent can act on them.
    assert str(mu1) in msg
    assert str(mu2) in msg


@pytest.mark.asyncio
async def test_deprioritize_non_observation_400_falls_through(mock_api, mcp_client):
    """A 400 that is NOT the observation redirect (plain-string detail) must
    fall through unchanged — no false 'read-only observation' message."""
    mock_api.deprioritize_memory_unit.side_effect = _http_400('ambiguous vault reference')

    with pytest.raises(ToolError) as ei:
        await mcp_client.call_tool(
            'memex_memory_deprioritize',
            {'unit_id': str(uuid4()), 'reason': 'x', 'vault_id': str(uuid4())},
        )

    assert 'read-only observation' not in str(ei.value)
