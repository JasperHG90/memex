"""Backwards-compat shim for the deprioritize / restore tool descriptions.

Canonical text moved to ``memex_common.tool_descriptions`` on 2026-05-14
(three-tier agent-surface architecture). This module re-exports the
constants under their historical names so existing MCP imports keep
working.
"""

from __future__ import annotations

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESC as MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_MEMORY_RESTORE_DESC as MEMEX_MEMORY_RESTORE_DESCRIPTION,
)


__all__ = [
    'MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION',
    'MEMEX_MEMORY_RESTORE_DESCRIPTION',
]
