"""Per-agent Tier 2 harness strings shared across Memex consumers.

Tier 2 (per-agent) framing — outcome-signal lexicon, capture cadence,
slash-command behaviour — used to be defined in each agent's own surface
(Hermes' ``briefing.py``; the Claude Code CLI bridge in ``memex_cli``).
That layout meant the same prose existed in two physical places per
agent — Hermes' in-process plugin path and the CLI bridge path both
needed identical bytes, with nothing to enforce it.

This module is the **single source of truth** for those harness strings.
Hermes' ``briefing.py`` and the CLI ``agent_surface`` module both import
from here so the in-process and CLI-bridge paths surface byte-identical
prose. Identity is pinned by tests in each consumer package.

Style: same Tier 2 discipline as ``memex_common.agent_surface`` (concrete,
imperative, rationale-that-aids-generalization only). Each harness sits
≤ 1,600 chars / 400 tokens — the universal Tier 1b block carries the rest.
"""

from __future__ import annotations


HERMES_HARNESS = """## Hermes-specific framing

Outcome-signal lexicon for paired writes:
- Success → "that worked", "that fixed it", "record it", "save this" → verb=`helpful`
- Failure → "stop suggesting X", "didn't work", "we removed it", "that was wrong" → verb=`not_helpful`

Capture cadence — ask: would I want these steps back next time? YES (you worked out HOW to do or fix something non-obvious) → `memex_case_submit` (trigger, actions, outcome, lesson) — a reusable procedure, NOT a note. A durable FACT / DECISION → `memex_add_note` (≤300 tokens); `memex_append_note(note_key, delta)` to extend. Preferences / conventions → `memex_kv_put` per the KV rules above. Tie-break: unsure it's worth saving → note/nothing; unsure case-vs-note → case."""


CLAUDE_CODE_HARNESS = """## Claude Code-specific framing

<critical_constraint name="capture_routing">
Before saving, ask: "next time I hit this, would I want these steps back?"
- YES — you worked out HOW to do or fix something non-obvious (a debugging path, a workaround, a sequence that worked) → `memex_case_submit` (trigger, actions, outcome, lesson). Becomes a reusable procedure — not `memex_add_note`.
- NO, but it's a durable FACT / DECISION someone would look up ("we chose X", a config value, an API shape) → `memex_add_note(background=true, author="claude-code")` (≤300 tokens, no per-file changelogs).
- NO to both (it just worked, a typo, a one-off) → save NOTHING. Tie-break: unsure it's worth saving at all → note or nothing; unsure case-vs-note for something how-to-shaped → case.
<example>vitest failed on a stale snapshot cache; clearing `.vitest-cache` fixed it → `memex_case_submit(trigger="vitest fails on stale snapshots", actions=["cleared .vitest-cache"], outcome="success", lesson="clear vitest's cache when tests fail for no code reason")`. "We chose Tailwind v4" → `memex_add_note`; "login UI worked first try" → nothing.</example>
</critical_constraint>

<critical_constraint name="write_routing">
Route each write intent; a miss is silent (no tool call) or wrong-namespace.
- A preference / setting / convention ("I prefer X", "for this repo …", "always lint first") → `memex_kv_put` per the KV namespace rules above (scope by cue; the `<app>` cue beats "I"/"my" — `app:claude-code:*`, not `user:`).
- A reusable workflow or worked episode → `memex_case_submit` (see capture_routing; never `memex_add_note`).
- `"That worked / it's holding / that fixed it"` about an existing memory → `memex_record_outcome(units=[{unit_id, verb:"helpful", reason}])` on the search-returned units; do NOT add a "confirmed" note.
Local `Write`/`Edit` is for project code, never preferences.
</critical_constraint>

<critical_constraint name="clarify_under_ambiguity">
Vague signals — `"that worked"`, `"we did it"`, `"stop suggesting that"` — with NO specific referent → ASK which fix / which suggestion. Never call `memex_record_outcome` with a guessed `unit_id` or a target fabricated from search results.
</critical_constraint>

<critical_constraint name="list_shape_questions">
Recall-shape queries — `"what notes do we have on X?"`, `"remind me about Y"`, `"find anything on Z"`, `"any notes on …"`, `"look for <topic>"` — **enumerate options for the user to pick from**, do NOT deliver the single best answer.

Required:
1. Call `memex_note_search` (or `memex_find_note` / `memex_list_notes`).
2. Present **≥2 candidate notes** as a numbered list.
3. Each entry: `note_key` (or clear descriptor) AND a date reference.
4. Do NOT narrate any single note's contents. Pause for the user to pick.

Detailing the top match — even when it IS the right one — FAILS the intent: they asked to **recognise** which note, and you consumed one for them.

<example>
User: "Find anything I wrote about the deploy pipeline last quarter."
WRONG: "Primary note: `ci-cd-circleci-migration` — switched off GitHub Actions on 2025-11-12 over artifact-size limits, plus the rollback hook…"
RIGHT: "Three deploy-pipeline notes from last quarter:
1. `ci-cd-circleci-migration` (2025-11-12) — switch off GitHub Actions, rationale
2. `deploy-window-q4-policy` (2025-10-04) — agreed deploy windows
3. `rollback-runbook-revision` (2025-12-01) — updated rollback procedure
Which were you thinking of?"
</example>
</critical_constraint>

<critical_constraint name="cooccurrence_graph_required">
Relationship questions (`"who does X work with?"`, `"what cooccurs with Y?"`, `"strongest counterpart"`) REQUIRE `memex_get_entity_cooccurrences` after `memex_list_entities` — the latter returns names but not graph edges, so it can't answer "strongest counterpart" alone.
</critical_constraint>

Slash commands:
- `/remember [text]` — save to memory (routes to `memex_kv_put` / `memex_case_submit` / `memex_add_note` by shape).
- `/recall [query]` — search memory (how-to query → `memex_procedural_search`; else `memex_memory_search` + `memex_note_search`).
- `/retro` — session postmortem; files reusable how-tos as cases.
- `/extract-case [note|file|url]` — turn content into a case, if it holds a reusable how-to (gated).

Prohibitions:
- NEVER use `memex_recent_notes` for discovery.
- NEVER fabricate Note/Node/Unit IDs — only IDs from tool output.
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata is inline).
- NEVER `memex_read_note` on notes >500 tokens — use `memex_get_page_indices` + `memex_get_nodes`.
- NEVER present Memex data without inline numbered citations.

<critical_constraint name="answer_from_briefing">
The session briefing injected at SessionStart (the `# Session Briefing` block in context) already holds (per vault state): vault summary, themes, top entities, KV facts, pinned procedural cards, and available vaults. Answer overview-shape queries ("what's in this vault", "what's it about") FROM those sections. NEVER call `memex_get_vault_summary`, `memex_kv_list`, `memex_list_vaults`, or `memex_survey` to refresh data already rendered there. EXCEPT re-call when the asked-about section is absent (dropped under budget) or the user wants fresh data.
</critical_constraint>"""


__all__ = ['CLAUDE_CODE_HARNESS', 'HERMES_HARNESS']
