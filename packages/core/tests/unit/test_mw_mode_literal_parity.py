"""Parity check between ``memex_core.memory.sql_models.MWMode`` and the
``MwModeLiteral`` Literal in ``memex_common.schemas``.

``memex_common`` cannot import ``memex_core``, so the Literal duplicates
the enum's string values. This test imports both, computes their accepted
value sets, and asserts equality. Adding a new value to either side
without updating the other fails this test.
"""

from __future__ import annotations

import typing

from memex_common.schemas import MwModeLiteral
from memex_core.memory.sql_models import MWMode


def test_mw_mode_literal_matches_enum() -> None:
    enum_values = {member.value for member in MWMode}
    literal_values = set(typing.get_args(MwModeLiteral))
    assert enum_values == literal_values, (
        f'MWMode/MwModeLiteral drift: enum={enum_values}, literal={literal_values}. '
        'Update both definitions when adding a new mode.'
    )
