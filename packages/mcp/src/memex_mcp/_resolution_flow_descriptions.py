"""Backwards-compat shim for the resolution-flow composite descriptions.

Historically this module composed the bare per-tool preambles (from
``_outcome_descriptions`` and ``_deprioritize_descriptions``) with a large
shared block containing the 5-step flow + axes table + historical routing
+ "do-not-add" scope-creep list — and exported the composite under
``MEMEX_RECORD_OUTCOME_DESCRIPTION`` / ``MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION``.

As of 2026-05-14 the three-tier agent-surface architecture moved the
universal flow / axes / routing content to ``memex_common.agent_surface``
(Tier 1b — delivered via agent system prompts). MCP tool descriptions
now carry only Tier 1a per-tool contracts, sourced from
``memex_common.tool_descriptions``. The composite is no longer composed
here; this module re-exports the bare per-tool descriptions under their
historical names so existing imports keep working without duplicating
text across packages.

Importers should migrate to ``memex_common.tool_descriptions`` directly.
"""

from __future__ import annotations

from memex_common.tool_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESC as MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESC as MEMEX_RECORD_OUTCOME_DESCRIPTION,
)


__all__ = [
    'MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION',
    'MEMEX_RECORD_OUTCOME_DESCRIPTION',
]
