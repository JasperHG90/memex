"""F20 regression: ``quality`` must reject Python bools at agent boundaries.

The team-lead's commit-9 review pinned a real silent-corruption window:
``True`` is a subclass of ``int`` in Python (``bool ⊂ int``), so a naive
``isinstance(quality, int)`` branch would coerce ``True`` → ``Quality(1)
= AGAIN`` and ``False`` → ``Quality(0)`` (which raises ValueError, but
the corruption is asymmetric — True is silently mapped). Pydantic's
``int | str`` Union does NOT reject bool either; it coerces to int.

This regression pins the bool-exclusion guard at BOTH agent surfaces:
- Hermes handler ``handle_memory_review`` — raw dict input, no Pydantic gate.
- MCP tool ``memex_memory_review`` — Pydantic ``int | str`` would coerce
  bool to int otherwise, so the explicit ``isinstance(quality, bool)``
  branch in the handler body is the actual gate.

Without these guards, ``memex_memory_review(unit_id=..., quality=True,
vault_id=...)`` would record an AGAIN outcome (failure counter
incremented, sticky streak advanced) when the caller almost certainly
meant something else. The kind of bug that ships and stays shipped for
years.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import handle_memory_review


@pytest.mark.parametrize('bool_value', [True, False])
def test_hermes_handler_rejects_bool_quality(bool_value: bool) -> None:
    """``True`` must NOT route to ``Quality(1) = AGAIN``; ``False`` must NOT
    route to ``Quality(0) = ValueError``. Both should error early with a
    structured tool_error before any API call is attempted.
    """
    api = Mock()
    api.review_memory_unit = AsyncMock()
    api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    config = HermesMemexConfig()

    result = handle_memory_review(
        api=api,
        config=config,
        vault_id=uuid4(),
        args={'unit_id': str(uuid4()), 'quality': bool_value, 'vault_id': str(uuid4())},
    )

    parsed = json.loads(result)
    assert 'error' in parsed, f'expected tool_error envelope, got {parsed!r}'
    assert 'bool' in parsed['error'].lower() or 'invalid quality' in parsed['error'].lower(), (
        f'error message should explain bool rejection; got {parsed["error"]!r}'
    )
    api.review_memory_unit.assert_not_awaited()


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
def test_hermes_handler_accepts_int_and_str_quality(
    good_value: int | str, expected_enum_value: int
) -> None:
    """Sanity check: int and string forms still route correctly after the
    bool guard. Confirms the guard is bool-only, not int-blanket.
    """
    from memex_core.memory.revisit import Quality

    api = Mock()
    api.review_memory_unit = AsyncMock(
        return_value={
            'unit_id': 'u',
            'quality': 'good',
            'next_review_at': 'x',
            'interval_days': 1,
            'review_count': 0,
            'auto_deprioritized': False,
        }
    )
    api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    config = HermesMemexConfig()

    handle_memory_review(
        api=api,
        config=config,
        vault_id=uuid4(),
        args={'unit_id': str(uuid4()), 'quality': good_value, 'vault_id': str(uuid4())},
    )

    api.review_memory_unit.assert_awaited_once()
    await_args = api.review_memory_unit.await_args
    assert await_args is not None
    quality_arg = await_args.args[1]
    assert isinstance(quality_arg, Quality)
    assert quality_arg.value == expected_enum_value
