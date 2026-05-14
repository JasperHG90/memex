"""Backwards-compat shim for the summarize_node tool description.

Canonical text moved to ``memex_common.tool_descriptions`` on 2026-05-14
(three-tier agent-surface architecture).
"""

from __future__ import annotations

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_SUMMARIZE_NODE_DESC as MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION,
)


__all__ = ['MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION']
