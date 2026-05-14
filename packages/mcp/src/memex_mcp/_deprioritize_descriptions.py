"""Verbatim agent prompt text for deprioritize/restore tools.

When the descriptions change, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION = (
    "memory_deprioritize — Lower a memory unit's retrieval rank without deleting it.\n"
    'Use when a memory is misleading, outdated, or noise that contaminates retrieval.\n'
    '\n'
    '- unit_id: the unit to deprioritize\n'
    '- reason: brief text explanation (logged to maintenance ledger). Use this field\n'
    '  liberally to capture WHY (e.g., "user confirmed issue fixed", "superseded by\n'
    '  v2.3 release", "was wrong about deploy target"). Free text is sufficient — no\n'
    '  enum needed.\n'
    '\n'
    'The unit remains accessible via include_deprioritized=true retrieval. To restore,\n'
    'the user runs `memex memory restore <id>`. This is non-destructive — prefer it\n'
    'over hard delete in almost all cases. Use sparingly: a small number of high-quality\n'
    'deprioritizations is more valuable than aggressive pruning.\n'
    '\n'
    'Virtual units cannot be deprioritized. Memory units whose metadata contains\n'
    '`"virtual": true` (synthesized from MentalModel observations) have no DB row —\n'
    'calling memory_deprioritize on their UUID returns 404. Before calling, filter\n'
    'retrieval candidates to those with `unit_metadata.virtual` unset or false. If\n'
    'the candidate set is empty after the filter, fall back to entity-anchored\n'
    'search (`memex_list_entities` → `memex_get_entity_mentions`) to recover real\n'
    'source units.'
)

MEMEX_MEMORY_RESTORE_DESCRIPTION = (
    'memory_restore — Restore a previously-deprioritized memory unit.\n'
    'Flips ``is_deprioritized`` back to false; the unit re-enters default-scope retrieval.\n'
    'Companion to memory_deprioritize. Writes an audit_logs row.'
)
