"""Enum-coercion helper used by both the V3 exporter and V12 importer.

Several `MemoryUnit` / `MemoryLink` / `MaintenanceProposal` columns are
typed as Python enums (`FactTypes`, `ContentStatus`, `LintType`, ...) but
backed by Postgres `Text`. SQLModel/SA currently hydrates them as plain
strings, but a future schema or library change could hydrate them as
enum members. Plain `str(EnumMember)` returns ``'FactTypes.WORLD'``, NOT
``'world'``, which would silently break:

- the export contract (snapshot JSONL would carry a non-canonical token),
- ``format_for_embedding`` on the import side (its `.capitalize()` would
  emit ``'Facttypes.world'`` and the regenerated embedding would diverge
  from extraction-time semantics).

Both sides funnel through this helper to neutralize the risk symmetrically.
"""

from __future__ import annotations

from typing import Any


def coerce_enum_value(value: Any) -> str:
    """Return the underlying string value of an enum-or-str column.

    Idempotent on plain strings. For enum members, returns
    ``str(value.value)`` instead of ``str(value)``.
    """
    if value is None:
        return ''
    if hasattr(value, 'value'):
        return str(value.value)
    return str(value)
