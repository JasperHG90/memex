"""Shared test helpers for MCP tests."""

import json
from uuid import UUID

TEST_VAULT_UUID = UUID('00000000-0000-0000-0000-000000000001')


def parse_tool_result(result) -> list[dict] | dict | None:
    """Parse a tool call result into structured data.

    Returns a list of dicts for list results, a dict for single model results,
    or None for empty/null results.
    """
    # FastMCP may return structured_content with empty content list
    if not result.content:
        if hasattr(result, 'structured_content') and result.structured_content:
            return result.structured_content.get('result', [])
        return None
    text = result.content[0].text
    if not text or text == 'null':
        return None
    return json.loads(text)
