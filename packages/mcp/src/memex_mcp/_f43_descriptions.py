"""Verbatim agent prompt text for F43 — §3.5 5-step resolution flow + §3.4.2 historical routing.

Sourced from cognitive-memory-research-report.md §3.5 ("User-driven memory
resolution: how should agents invoke F4?") + §3.4.1 ("MW is the gradient;
deprioritize is the binary") + §3.4.2 (historical-routing rule, added
2026-05-02). When the spec changes, the verbatim test fails — that is the
contract.

Two surfaces touched:
- ``MEMEX_RECORD_OUTCOME_DESCRIPTION`` — F1a's outcome verb, expanded with the
  flow's step-by-step routing.
- ``MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION`` — F4's binary verb, expanded with
  the same flow + the orthogonal-axes table.

Both descriptions teach the same flow because both verbs participate in it.
"""

from __future__ import annotations

from memex_mcp._f1a_descriptions import (
    MEMEX_RECORD_OUTCOME_DESCRIPTION as _RECORD_OUTCOME_PREAMBLE,
)
from memex_mcp._f4_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION as _F4_DEPRIORITIZE_DESCRIPTION,
)

# ---------------------------------------------------------------------------
# Shared section blocks (composed into both tool descriptions verbatim).
# ---------------------------------------------------------------------------

_FLOW_HEADER = (
    'When the user reports an issue resolved ("the X bug is fixed", "we shipped\n'
    'Y", "issue Z is no longer relevant"), follow the §3.5 5-step flow before\n'
    'writing. Skipping any step (especially Step 1 disambiguation or Step 3 LLM\n'
    'judgment) leads to bulk-writing irrelevant units.\n'
)

_FLOW_BODY = (
    'Step 1 — Disambiguate first.\n'
    '  If the scope is ambiguous (multiple candidate notes, multiple candidate\n'
    '  topics, or a topic that may span notes), ASK before writing. Examples:\n'
    "  'telegram issues from yesterday' when there are three reflection notes\n"
    "  mentioning Telegram; 'the auth bug we discussed' with no temporal anchor;\n"
    "  multiple distinct issues conflated into 'the issues are fixed'.\n"
    '\n'
    'Step 2 — Route by info quality and pick the cheapest path.\n'
    '  Title-fragment known → `memex_find_note(query="…")` (indexed, cheap).\n'
    '  Title unknown, content only → `memex_memory_search` (full pipeline,\n'
    '  expensive but right when title-fragment lookup is unavailable). Then pick\n'
    '  one of three coverage paths:\n'
    '    Option A (entity-anchored, highest recall when topic ↔ entity):\n'
    '      `memex_list_entities(query="…")` → `memex_get_entity_mentions(entity_id=…)`.\n'
    '      Structural traversal; no semantic-rank miss.\n'
    '    Option B (cross-note semantic, when no entity anchor):\n'
    '      `memex_memory_search(query="…", after="…", top_k=30)`.\n'
    '      CRITICAL: top_k must be ≥30 — the default 5 is too narrow and will\n'
    '      miss cross-note matches.\n'
    '    Option C (single-note PageIndex traversal, when scope is provably one note):\n'
    '      `memex_get_page_indices(note_id)` → read chunk summaries → pick relevant\n'
    '      chunks → `memex_get_memory_units(chunk_ids=[…])`. Captures every unit\n'
    '      in the chunk; semantic top-k can miss paraphrased mentions.\n'
    '\n'
    'Step 3 — Mandatory LLM judgment over the candidate set.\n'
    '  Whichever path returned candidates, READ the unit bodies and judge which\n'
    "  ones actually correspond to the user's claim. Memory units are short by\n"
    '  design (single fact / observation / event, ~1–3 sentences) — the search\n'
    '  response IS the content; reading does not blow up context. The judgment\n'
    '  CANNOT be skipped: a daily-reflection note contains episodic observations\n'
    "  ('worked on memex 3h today') that look superficially relevant but are not\n"
    '  fix-targets. NEVER bulk-write against the raw candidate set.\n'
    '\n'
    'Step 4+5 — Paired writes against the LLM-judged-relevant subset only.\n'
    '  For each unit the LLM judged relevant, issue BOTH writes:\n'
    '    `memex_record_outcome(unit_ids=[…], success=false, reason="…")` AND\n'
    '    `memex_memory_deprioritize(unit_id=…, reason="…")`.\n'
    '  Both, against the SAME subset. The two verbs are orthogonal axes — see\n'
    '  the table below — and the user-confirmed-fix flow is BOTH signals at once\n'
    '  (negative-usefulness + binary verdict to stop surfacing).\n'
)

_AXES_TABLE = (
    'Orthogonal axes (verbatim from §3.4.1 — MW is the gradient; deprioritize is\n'
    'the binary). The two verbs answer different questions; keep them separate:\n'
    '\n'
    '  | Tool                   | Question it answers           | Cardinality                | Reversible? |\n'
    '  |------------------------|-------------------------------|----------------------------|-------------|\n'
    '  | `memex_record_outcome` | "Did this memory help when    | Append-only counter        | No          |\n'
    '  |   (success=…)          |  retrieved?"                  | (compounds across retrievals)| (audit log) |\n'
    '  | `memex_memory_         | "Should this surface by       | Binary state on the unit   | Yes         |\n'
    '  |   deprioritize`        |  default at all?"             |                            | (memory_restore) |\n'
    '\n'
    '  - outcome=false but no deprioritize: gradient signal only — let MW\n'
    '    compound; perhaps a different query still legitimately wants this unit.\n'
    '  - deprioritize but no outcome: verdict without judging past usefulness\n'
    "    (e.g., 'correct when written but no longer relevant').\n"
    '  - BOTH (the resolved-issue case): negative-usefulness AND binary verdict.\n'
)

_IMPERFECT_RECALL = (
    'Imperfect recall is BY DESIGN. None of Options A/B/C give *provable* 100%\n'
    'recall on cross-note resolution. Semantic search misses paraphrases; entity\n'
    'traversal misses oblique references; chunk-scoped reads miss issues split\n'
    'across chunks. This is fine — F33 exploration is the safety net. Any unit\n'
    'that slipped past resolution will occasionally re-surface; the user re-\n'
    'confirms; another `record_outcome(success=false)` compounds the MW penalty.\n'
    'User-driven resolution is a GRADIENT across many turns, NOT a one-shot delete.\n'
)

_HISTORICAL_ROUTING = (
    'Historical / audit-query routing rule (separate path from the resolution\n'
    'flow above). The 5-step flow assumes the user is asking "what is true *now*".\n'
    'For queries about HOW THINGS CHANGED — "how has my view on X evolved", "what\n'
    'did I used to think about Y", "show me everything I believed about Z\n'
    'including the wrong stuff" — route differently:\n'
    '  - Ordered-chain timeline on a specific unit:\n'
    '      `memex_get_unit_history(unit_id)` (F49) — graph walk through\n'
    '      contradiction links, oldest → newest. Cleaner than ranked search.\n'
    '  - Broader audit / "show me everything including hidden stuff":\n'
    '      `memex_memory_search(query="…", apply_pre_filter=False)` — bypasses\n'
    '      F40+F48 (MW + FSFM + confidence pre-filters) so contradicted,\n'
    '      behaviorally-failed, and decayed units appear. Post-reranker boosts\n'
    '      (F47 confidence_boost, F1c MW) still apply, so contradicted units\n'
    '      rank below clean ones — which is correct for audit queries.\n'
    '\n'
    'Disambiguation triggers the agent should learn (any of these → use the\n'
    'historical routing rule, NOT the resolution flow): "evolved", "used to",\n'
    '"history of", "what changed", "what did I think before", "audit",\n'
    '"show me everything", "show me the hidden ones", explicit time-window-\n'
    'with-no-filter intent.\n'
    '\n'
    'When the user says "the X issue is resolved" → resolution flow (Steps 1–5).\n'
    'When the user says "how has my position on X changed" → historical routing.\n'
    "Disambiguation is the agent's responsibility.\n"
)

_DO_NOT_ADD = (
    'Things this flow DOES NOT need (codified to resist scope creep — do NOT\n'
    'request these as new endpoints or parameters; the existing primitives are\n'
    'sufficient):\n'
    '  - A combined `memex_resolve(unit_ids, reason)` endpoint. Hides the\n'
    '    orthogonal axes. Compose at the agent layer.\n'
    '  - A `resolved_at` timestamp column. The maintenance ledger already\n'
    "    records when deprioritize fired; the note's `created_at` anchors the\n"
    '    original observation.\n'
    '  - A `resolution_type` enum on deprioritize. Free text in `reason`\n'
    '    captures the same information without committing to a closed taxonomy.\n'
    '  - A `bulk-by-source` parameter on deprioritize. Agents iterate.\n'
    '  - Note-level deprioritize. Notes are episodic anchors; deprioritization\n'
    '    applies to the derived units, not the source notes.\n'
)


# ---------------------------------------------------------------------------
# Composed tool descriptions (the strings the MCP server actually serves).
# ---------------------------------------------------------------------------

# F1a's outcome-recording verb. The original short docstring is imported from
# `_f1a_descriptions` (single source of truth) so MW counter discoverability
# stays in sync between the standalone tool description and the F43-augmented
# composite. F1a's verbatim test pins the constant against the spec; F43
# appends the §3.5 flow + axes table below it. The `\n\n` after the preamble
# yields a blank line before the F43 section header — visually matches the
# deprioritize sibling description (see `_DEPRIORITIZE_PREAMBLE` below).
MEMEX_RECORD_OUTCOME_DESCRIPTION = (
    _RECORD_OUTCOME_PREAMBLE
    + '\n\n'
    + '## When the user reports an issue resolved (§3.5 5-step flow)\n'
    + '\n'
    + _FLOW_HEADER
    + '\n'
    + _FLOW_BODY
    + '\n'
    + _AXES_TABLE
    + '\n'
    + _IMPERFECT_RECALL
    + '\n'
    + '## Historical-routing rule (§3.4.2)\n'
    + '\n'
    + _HISTORICAL_ROUTING
    + '\n'
    + _DO_NOT_ADD
)


# F4's deprioritize verb. The original short F4 description (kept as the
# preamble so deprioritize discoverability for misleading/outdated/noise
# units is unchanged) is followed by the same §3.5 flow + axes + history.
# Imported from `_f4_descriptions` so there is a single source of truth; F4's
# verbatim test (test_f4_tool_descriptions.py) pins the constant against the
# spec, and F43 just appends a trailing newline for clean section separation.
_DEPRIORITIZE_PREAMBLE = _F4_DEPRIORITIZE_DESCRIPTION + '\n'

MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION = (
    _DEPRIORITIZE_PREAMBLE
    + '\n'
    + '## When the user reports an issue resolved (§3.5 5-step flow)\n'
    + '\n'
    + _FLOW_HEADER
    + '\n'
    + _FLOW_BODY
    + '\n'
    + _AXES_TABLE
    + '\n'
    + _IMPERFECT_RECALL
    + '\n'
    + '## Historical-routing rule (§3.4.2)\n'
    + '\n'
    + _HISTORICAL_ROUTING
    + '\n'
    + _DO_NOT_ADD
)


__all__ = [
    'MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION',
    'MEMEX_RECORD_OUTCOME_DESCRIPTION',
]
