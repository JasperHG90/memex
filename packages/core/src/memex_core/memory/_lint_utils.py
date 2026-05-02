"""Shared utilities for the lint subsystem (services/lint.py + diagnostics/lint_dashboard.py).

Kept in :mod:`memex_core.memory` so both the services and diagnostics layers
can depend on it without creating a layering inversion.
"""

from __future__ import annotations

from typing import Any


def enum_value(v: Any) -> str:
    """Coerce SQLModel enum / raw string to its string value.

    F6 stores ``lint_type``/``status``/``source`` as Text columns but the
    SQLModel declarations type them as enums (``LintType``/``LintStatus``/
    ``LintSource``). Reads can come back as either the enum instance or the
    raw string depending on driver path; this collapses both to the string
    form callers expect.
    """
    return v.value if hasattr(v, 'value') else str(v)
