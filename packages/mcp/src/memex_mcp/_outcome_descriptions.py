"""Backwards-compat shim for the record_outcome tool description.

Canonical text moved to ``memex_common.tool_descriptions`` on 2026-05-14
(three-tier agent-surface architecture). This module re-exports the
constant under its historical name so existing MCP imports keep working.
"""

from __future__ import annotations

from memex_common.tool_descriptions import (
    MEMEX_RECORD_OUTCOME_DESC as MEMEX_RECORD_OUTCOME_DESCRIPTION,
)


__all__ = ['MEMEX_RECORD_OUTCOME_DESCRIPTION']
