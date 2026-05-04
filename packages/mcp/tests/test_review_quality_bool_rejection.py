"""review quality bool rejection: ``quality`` must reject Python bools.

Pydantic's ``int | str`` Union does NOT reject bool — it silently coerces
``True`` → ``1`` and ``False`` → ``0`` (because ``bool ⊂ int``). Without
an explicit ``isinstance(quality, bool)`` guard before the int branch,
``memex_memory_review(quality=True)`` would route to ``Quality(1) =
AGAIN`` and silently corrupt the FSRS schedule, the outcome counters,
and the sticky streak.

This regression pins the explicit bool-rejection guard at the MCP tool
boundary. Sister test for the Hermes plugin lives in
``packages/hermes-plugin/tests/test_review_quality_bool_rejection.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize('bool_value', [True, False])
@pytest.mark.asyncio
async def test_memex_memory_review_rejects_bool_quality(
    mock_api, mcp_client, bool_value: bool
) -> None:
    """``True`` must NOT be coerced into ``Quality.AGAIN``; ``False`` must
    NOT be coerced into ``Quality(0)``.

    The bool guard is implemented as a Pydantic ``BeforeValidator`` on the
    field annotation, so rejection happens at param-parsing time — before
    the handler body runs and before any API method is dispatched. The
    failure surfaces as a ``ToolError`` whose message references "bool"
    (FastMCP wraps Pydantic's ValidationError as a tool-side error).
    """
    with pytest.raises(ToolError, match='bool'):
        await mcp_client.call_tool(
            'memex_memory_review',
            {
                'unit_id': str(uuid4()),
                'quality': bool_value,
                'vault_id': 'global',
            },
        )

    mock_api.review_memory_unit.assert_not_called()


@pytest.mark.parametrize(
    'good_value, expected_enum_value',
    [
        (1, 1),
        (3, 3),
        ('again', 1),
        ('Good', 3),
        ('EASY', 4),
    ],
)
@pytest.mark.asyncio
async def test_memex_memory_review_accepts_int_and_str_quality(
    mock_api, mcp_client, good_value: int | str, expected_enum_value: int
) -> None:
    """The bool guard is bool-only — int and str forms still route correctly."""
    from memex_core.memory.revisit import Quality

    mock_api.review_memory_unit.return_value = {
        'unit_id': 'u',
        'quality': 'good',
        'next_review_at': '2026-05-04T00:00:00+00:00',
        'interval_days': 1,
        'review_count': 0,
        'auto_deprioritized': False,
    }

    await mcp_client.call_tool(
        'memex_memory_review',
        {
            'unit_id': str(uuid4()),
            'quality': good_value,
            'vault_id': 'global',
        },
    )

    mock_api.review_memory_unit.assert_called_once()
    quality_arg = mock_api.review_memory_unit.call_args.args[1]
    assert isinstance(quality_arg, Quality)
    assert quality_arg.value == expected_enum_value
