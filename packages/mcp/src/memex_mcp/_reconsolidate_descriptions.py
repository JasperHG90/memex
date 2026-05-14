"""Backwards-compat shim for the reconsolidate / consolidate tool descriptions.

Canonical text moved to ``memex_common.tool_descriptions`` on 2026-05-14
(three-tier agent-surface architecture).
"""

from __future__ import annotations

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_CONSOLIDATE_DESC as MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION,
    MEMEX_MEMORY_RECONSOLIDATE_DESC as MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION,
)


__all__ = [
    'MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION',
    'MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION',
]
