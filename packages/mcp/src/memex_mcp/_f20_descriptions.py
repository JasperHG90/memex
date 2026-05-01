"""Verbatim agent prompt text for F20 tools.

The two F20 verbs are deliberately distinct so the agent can disambiguate
intent unambiguously:

- memex_get_due_for_review — READ verb. "What memories are due for review?"
  Returns the list, takes no rating. Scoped to a vault.
- memex_memory_review — WRITE verb. "I just reviewed unit X with quality Y."
  Advances the FSRS-5 schedule, records the outcome, updates the sticky
  streak, audits the action — all in one transaction.

The disambiguation matters because both verbs share the word "review":
the agent must call get_due_for_review when listing and memory_review when
recording. The verbatim phrasings here include the trigger phrasings the
real-LLM-turn test exercises (TC-24-12).

Algorithm note: scheduling is FSRS-5 via py-fsrs 4.1.2 — the current
production-grade open-source spaced-repetition algorithm (Anki, RemNote,
ts-fsrs all ship FSRS-5 in 2025). Verified at .dev-team-artifacts/
dev-tier-a-cognitive-memory/pocs/003-f20-fsrs-parity/paper-cross-check.md.
"""

from __future__ import annotations

MEMEX_GET_DUE_FOR_REVIEW_DESCRIPTION = (
    'memex_get_due_for_review — List memories that are due for revisit in a vault.\n'
    '\n'
    'Use when the user asks something like "what memories are due for review?",\n'
    '"what should I revisit?", or "show me my review queue". Returns the units\n'
    'whose `revisit_due_at <= now()` AND that pass the 5-gate eligibility\n'
    'predicate (intent_class IN (permanent, durable), status=active, not\n'
    'deprioritized, confidence >= 0.5, mw_score >= 0.4).\n'
    '\n'
    '- vault_id: vault UUID or name (defaults to active vault if omitted)\n'
    '- limit: maximum number of due units to return (default 20)\n'
    '\n'
    'Returns a list of {unit_id, text_preview, revisit_due_at, intent_class}.\n'
    'This is a READ verb — it does NOT advance any schedule. To record a\n'
    'review outcome, use memex_memory_review.'
)

MEMEX_MEMORY_REVIEW_DESCRIPTION = (
    'memex_memory_review — Record a review outcome on a memory unit.\n'
    '\n'
    'Use when the user says something like "I just reviewed memory X, it was\n'
    '\'good\'", "mark X as easy", or "I forgot X" (which maps to quality=again).\n'
    'Advances the FSRS-5 schedule, increments success/failure outcome counters,\n'
    'maintains the sticky-deprioritize streak, and writes an audit row — all\n'
    'in a single transaction.\n'
    '\n'
    '- unit_id: the memory unit being reviewed\n'
    '- quality: one of "again" (forgotten), "hard", "good", or "easy"\n'
    '\n'
    'Quality mapping for outcome counters:\n'
    '  "again" / "hard" → recorded as a failure outcome\n'
    '  "good" / "easy"  → recorded as a success outcome\n'
    '\n'
    'Sticky-deprioritize: 5 consecutive "again" ratings (without an intervening\n'
    'hard/good/easy) automatically flip the unit to is_deprioritized=true.\n'
    'Once deprioritized, positive outcomes do NOT auto-restore — the user\n'
    'must explicitly run `memex memory restore` to bring it back.\n'
    '\n'
    'Returns: {unit_id, quality, next_review_at, interval_days, review_count,\n'
    'auto_deprioritized}. Use auto_deprioritized to inform the user when the\n'
    'sticky gate has been triggered.'
)
