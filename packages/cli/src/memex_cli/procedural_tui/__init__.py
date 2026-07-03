"""Procedural-plane curation TUI (`memex procedure review`).

Operator surface for the §18.8 / §19.8 curation contract: browse and
search entries, pin/unpin them into the briefing chain (global →
project:<id> → app:<consumer>), preview the assembled briefing, and
diff/rollback the non-destructive version ledger.
"""

from memex_cli.procedural_tui.controller import (
    ProceduralCurationController,
    build_chain,
    unified_version_diff,
    validate_context_key,
)

__all__ = [
    'ProceduralCurationController',
    'build_chain',
    'unified_version_diff',
    'validate_context_key',
]
