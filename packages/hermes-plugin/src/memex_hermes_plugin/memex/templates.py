"""Template name constants for Hermes-originated notes."""

from __future__ import annotations

from memex_common.agent_surface import (
    LAYER_ROUTING_PRIMER_FRAGMENT as _CANONICAL_LAYER_ROUTING_PRIMER_FRAGMENT,
)

# Template used for the per-session transcript note ingested on exit.
HERMES_SESSION_TEMPLATE = 'hermes-session'

# Template used for explicit ``memex_add_note`` captures with no other template.
HERMES_USER_NOTE_TEMPLATE = 'hermes-user-note'

# Template for future ``memex_retro`` structured postmortems (v2).
AGENT_REFLECTION_TEMPLATE = 'agent_reflection'

__all__ = [
    'AGENT_REFLECTION_TEMPLATE',
    'HERMES_SESSION_TEMPLATE',
    'HERMES_USER_NOTE_TEMPLATE',
]


# ============================================================
# Tier A — Prompt-fragment templates
# F4:  WS-quick-wins  (deprioritize/restore disclosure)
# F5:  WS-quick-wins  (summarize_node prompt)
# F8:  WS-linter      (get_lint_flags discoverability)
# F9:  WS-locks       (reconsolidate/consolidate disclosure)
# F20: WS-revisit     (memory_review prompt)
# F32: WS-diagnostics (diagnostics_summary disclosure)
# ============================================================

# --- F4 ---  (filled by WS-quick-wins)

# --- F5 ---  (filled by WS-quick-wins)

# --- F8 ---  (filled by WS-linter)

# --- F9 ---  (filled by WS-locks)

# --- F20 --- (filled by WS-revisit)

# --- F32 --- (filled by WS-diagnostics)
DIAGNOSTICS_SUMMARY_PROMPT_FRAGMENT = (
    'Diagnostics: call `memex_get_diagnostics_summary(vault_id=...)` to inspect '
    "a vault's health — unit counts by status (active/stale/deprioritized), "
    'pending lint counts by type, cluster_count (null when manifold cache is '
    'cold), avg MW score, and top retrieved entities. Synchronous; surfaces '
    'manifold status without waiting on UMAP compute.'
)


# --- F43 --- (5-step user-confirmed-fix resolution flow + historical routing)
# Verb-pair scaffolding for Hermes turns that handle "the X is fixed" prompts.
# A Hermes turn can lean on this structured prompt rather than free-form
# generation — see briefing.py `_RESOLUTION_FLOW_PRIMER` for the canonical text
# shown in the system prompt; this fragment is the same flow rendered as a
# tool-call planning template.
RESOLUTION_FLOW_PROMPT_FRAGMENT = (
    'When the user reports an issue resolved (§3.5 5-step flow):\n'
    '\n'
    '  1. DISAMBIGUATE — if scope is ambiguous, ASK before writing.\n'
    '  2. ROUTE — title-fragment → memex_find_note;\n'
    '     content-only → memex_memory_search; then pick:\n'
    '       (A) entity-anchored: memex_list_entities → memex_get_entity_mentions\n'
    '       (B) cross-note semantic: memex_memory_search(top_k>=30, after=...)\n'
    '       (C) single-note: memex_get_page_indices → memex_get_memory_units(chunk_ids=[...])\n'
    '  3. JUDGE — read candidate unit bodies; LLM-pick the fix-relevant subset.\n'
    '  4. RECORD — for each judged-relevant unit:\n'
    "     memex_record_outcome(unit_ids=[...], success=False, reason='...')\n"
    '  5. DEPRIORITIZE — for the SAME subset:\n'
    "     memex_memory_deprioritize(unit_id=..., reason='...')\n"
    '\n'
    'BOTH writes (steps 4 + 5) against the SAME subset only — never bulk-write\n'
    'against the raw candidate set. The two verbs are orthogonal axes:\n'
    'record_outcome is the MW gradient (compounds across retrievals);\n'
    'memory_deprioritize is the binary surface state (reversible via\n'
    'memex_memory_restore). User-confirmed-fix is BOTH signals at once.'
)


# --- F3 --- (4-layer memory-routing primer — single source of truth lives in
# `memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT`. Re-exported here
# under the `_PROMPT_FRAGMENT` suffix to match the templates.py naming
# convention for prompt fragments. The briefing.py `_LAYER_ROUTING_PRIMER`
# (table form) is sourced from the same module.)
LAYER_ROUTING_PROMPT_FRAGMENT = _CANONICAL_LAYER_ROUTING_PRIMER_FRAGMENT


# --- F43 --- (historical / audit-query routing rule)
HISTORICAL_ROUTING_PROMPT_FRAGMENT = (
    'When the user asks HOW THINGS CHANGED (not "what is true now"), do NOT\n'
    'use the resolution flow. Triggers: "evolved", "used to", "history of",\n'
    '"what changed", "what did I think before", "audit", "show me everything",\n'
    '"show me the hidden ones".\n'
    '\n'
    '  - Ordered chain on a specific unit:\n'
    '    `memex_get_unit_history(unit_id)` — graph walk through\n'
    '    contradiction links, oldest → newest.\n'
    '  - Broader audit / "show me everything including hidden stuff":\n'
    "    `memex_memory_search(query='...', apply_pre_filter=False)` —\n"
    '    bypasses MW + FSFM + confidence pre-filters; contradicted, decayed,\n'
    '    and behaviorally-failed units appear. Post-reranker boosts still apply.'
)


__all__ += [
    'HISTORICAL_ROUTING_PROMPT_FRAGMENT',
    'LAYER_ROUTING_PROMPT_FRAGMENT',
    'RESOLUTION_FLOW_PROMPT_FRAGMENT',
]
