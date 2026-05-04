"""Verbatim agent prompt text for lint flags tool.

When the spec changes, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_GET_LINT_FLAGS_DESCRIPTION = (
    'memex_get_lint_flags — List pending memory-hygiene findings the linter has detected.\n'
    'Use periodically (e.g., once per long session) or when the user asks about memory state.\n'
    '\n'
    '- vault_id (optional): scope to a single vault. Defaults to the active write vault '
    'when omitted (vault-scoping invariant — never falls through to a global '
    'all-vault view).\n'
    '- lint_type (optional): structural | quality | governance | schema\n'
    '- status (optional): pending | resolved | dismissed (default: pending)\n'
    '- limit (default 20)\n'
    '\n'
    'Each finding includes: target_id, lint_type, evidence (why detected), suggested_action.\n'
    'Most findings can be auto-resolved by calling the relevant tool (e.g., memory_deprioritize\n'
    'for low-Memory-Worth units). Surface high-confidence findings to the user; act autonomously on\n'
    'low-risk ones (deprioritize, mark stale).'
)
