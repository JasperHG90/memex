"""Verbatim agent prompt text for deprioritize/restore tools.

Sourced from cognitive-memory-research-report.md §4 deprioritize step 6 (lines
615-628 as of 2026-04-30 against memory_augmentation @ 1c0a464). When the
spec changes, the verbatim test (T4) fails — that is the contract.
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
    'deprioritizations is more valuable than aggressive pruning.'
)

MEMEX_MEMORY_RESTORE_DESCRIPTION = (
    'memory_restore — Restore a previously-deprioritized memory unit.\n'
    'Flips ``is_deprioritized`` back to false; the unit re-enters default-scope retrieval.\n'
    'Companion to memory_deprioritize. Writes an audit_logs row.'
)
