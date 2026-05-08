"""Shared utilities for the lint subsystem (services/lint.py + diagnostics/lint_dashboard.py).

Kept in memex_core.memory so both layers can depend on it without a layering inversion.
"""

from __future__ import annotations

from typing import Any


def enum_value(v: Any) -> str:
    """Coerce SQLModel enum or raw string to its string value.

    Lint tables store lint_type/status/source as Text, but SQLModel declares
    them as enums. Reads may return either form depending on driver path.
    """
    return v.value if hasattr(v, 'value') else str(v)
