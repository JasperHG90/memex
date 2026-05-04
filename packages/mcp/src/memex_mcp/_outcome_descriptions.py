"""Verbatim agent prompt text for record_outcome tool.

When the descriptions change, the verbatim test fails — that is the contract.

This is the canonical short description for ``memex_record_outcome``. The
composite resolution-flow description imports it as a preamble before
appending the 5-step flow + axes table + historical-routing rule, so a
single source of truth governs both the standalone tool description and the
augmented composite.
"""

from __future__ import annotations

MEMEX_RECORD_OUTCOME_DESCRIPTION = (
    'Record whether previously retrieved memories or a stored procedure '
    'contributed to a successful outcome. Default mode increments Memory Worth '
    'counters on memory units (target_type="memory_unit", '
    'unit_ids=[...]); set target_type="kv_key" with kv_key='
    '"procedure:<verb>:<context-tag>" to score a stored procedure. Call '
    'after you actually used the retrieved memory or the procedure.\n\n'
    'Call generously. Silence provides no learning signal.'
)
