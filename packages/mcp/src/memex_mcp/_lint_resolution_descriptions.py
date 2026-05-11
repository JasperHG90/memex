"""Verbatim agent prompt text for the lint resolution write tools.

When the spec changes, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_LINT_APPLY_WINNER_DESCRIPTION = (
    'memex_lint_apply_winner — Apply the recommended action on a '
    'winner-proposal lint finding (rule_name=propose_contradiction_winner).\n'
    'Use after surfacing the proposal to the user and confirming the agent '
    'has authority to apply the recorded action.\n'
    '\n'
    '- finding_id (required): UUID of the pending winner-proposal finding.\n'
    '\n'
    'The action recorded under evidence.action drives the mutation:\n'
    '  mark_loser_stale       — flip the loser memory_unit.status to stale.\n'
    '  supersede_loser_note   — set the loser note.superseded_by to the winner '
    'note id (falls back to mark_loser_stale when both units share the same '
    'parent note; the fallback reason is recorded under '
    'evidence.resolution.fallback_reason).\n'
    '  refine_not_contradict  — rewrite the inbound link from contradicts to '
    'refines (graph-pressure weight 0.0).\n'
    '  inconclusive           — no-op write; flips the finding to resolved.\n'
    '\n'
    'Each apply captures prior_state under evidence.resolution so the '
    'mutation is fully reversible via memex_lint_reverse_winner.'
)

MEMEX_LINT_REVERSE_WINNER_DESCRIPTION = (
    'memex_lint_reverse_winner — Reverse a previously applied winner-proposal '
    'lint finding.\n'
    'Reads the prior_state captured at apply-time and atomically restores '
    'the affected memory_unit.status, note.superseded_by, or '
    'memory_link.link_type. Writes a paired audit row '
    '(rule_name=propose_contradiction_winner_reversal); the original '
    'finding stays resolved so the partial unique index on pending findings '
    'remains valid.\n'
    '\n'
    '- finding_id (required): UUID of the previously applied (status='
    'resolved) winner-proposal finding.\n'
    '\n'
    'Use when the apply was premature or the user rejects the action after '
    'review.'
)
