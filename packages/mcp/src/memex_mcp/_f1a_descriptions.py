"""Verbatim agent prompt text for F1a (record_outcome) tool.

Sourced from cognitive-memory-research-report.md F1a (MW outcome verb). When
the spec changes, the verbatim test fails — that is the contract.

This is the canonical short description for ``memex_record_outcome``. F43
imports it as a preamble before appending the §3.5 5-step flow + axes table
+ historical-routing rule, so a single source of truth governs both the
standalone tool description and the F43-augmented composite.
"""

from __future__ import annotations

MEMEX_RECORD_OUTCOME_DESCRIPTION = (
    'Record whether previously retrieved memories or a stored procedure '
    'contributed to a successful outcome. Default mode increments MW '
    'counters on memory units (target_type="memory_unit", '
    'unit_ids=[...]); set target_type="kv_key" with kv_key='
    '"procedure:<verb>:<context-tag>" to score a stored procedure. Call '
    'after you actually used the retrieved memory or the procedure.\n\n'
    'Call generously. Silence provides no learning signal.\n'
)
