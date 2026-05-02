"""Shared revisit-validation utilities (F20).

Lives in :mod:`memex_common` so the MCP server, the Hermes plugin, and the
core HTTP route can all import the same guard without bouncing through
``memex_core``. ``memex_core`` already depends on ``memex_common``; the
inverse dependency is forbidden.
"""

from __future__ import annotations

from typing import Any

__all__ = ['reject_bool_quality']


def reject_bool_quality(v: Any) -> Any:
    """Reject Python ``bool`` from being coerced into a quality score.

    Without this guard, ``True`` would silently route to ``Quality.AGAIN``
    and ``False`` to ``Quality(0)`` because ``bool`` is a subclass of
    ``int`` in Python. Used as a Pydantic ``BeforeValidator`` on the F20
    ``quality`` field at every public boundary (HTTP route, MCP tool,
    Hermes handler) so the rejection happens at param-parse time, before
    any service is dispatched.

    Returns the input unchanged for non-bool values; raises ``ValueError``
    for ``True`` or ``False``.
    """
    if isinstance(v, bool):
        raise ValueError(
            f"bool is not a valid quality (got {v!r}); use 1/2/3/4 or 'again'/'hard'/'good'/'easy'."
        )
    return v
