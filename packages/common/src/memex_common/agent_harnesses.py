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

Capture cadence: write a short note (`memex_add_note`, ≤300 tokens, no per-file changelogs) when you finish a multi-step task, diagnose a non-obvious bug, or resolve a tricky env issue. User preferences / conventions are NOT note-shaped — those go to `memex_kv_put` per the KV-namespace rules above. Use `memex_append_note(note_key, delta)` to extend an existing note rather than re-ingesting."""


CLAUDE_CODE_HARNESS = """## Claude Code-specific framing

Capture cadence: call `memex_add_note(background=true, author="claude-code")` when you (1) complete a multi-step task, (2) diagnose a bug root cause, (3) make/discover an architectural decision, or (4) resolve a tricky env issue. Hard max 300 tokens; no per-file changelogs. User preferences / conventions are NOT note-shaped — those go to `memex_kv_put` per the KV namespace rules above.

<critical_constraint name="write_routing">
Route user write intents to the right tool — failure here is silent ("I'm ready" with no tool call) or the wrong namespace.
- `"Remember about me: I prefer X"` → `memex_kv_put(key="user:<field>", value=X)`.
- `"Remember in this repo / project / codebase: ..."` → `memex_kv_put(key="project:<id>:<field>", ...)`.
- `"Remember whenever I use <app> ..."` → `memex_kv_put(key="app:<app-id>:<field>", ...)`. The `<app>` cue wins over "I"/"my" — Claude Code preferences go under `app:claude-code:*`, NOT `user:claude-code:*`.
- `"Remember across our projects / company-wide"` → `memex_kv_put(key="global:<field>", ...)`.
- A multi-step WORKFLOW worth reusing ("how we deploy / release / rotate creds") → `memex_procedural_create` on the procedural plane (see the Procedural plane section), NOT a KV `procedure:` key (deprecated). A one-line CONVENTION ("always lint before commit") → `memex_kv_put(key="<scope>:<field>")` — scope by cue, ASK if ambiguous.
- `"That worked / it's holding / that fixed it"` with a referent in scope → `memex_record_outcome(units=[{unit_id, verb:"helpful", reason}])` on the units search returned. Do NOT `memex_add_note` a "Resolution confirmed" note — paired-write on the existing units.
- `"Save this insight / decision / lesson"` (new durable knowledge, not a confirmation) → `memex_add_note(...)`.
<example>User: "The JWT rotation cadence change we landed last sprint — it's been clean."
WRONG: search finds the rotation-decision unit → call `memex_add_note(title="JWT rotation confirmed working")`.
RIGHT: same search → `memex_record_outcome(units=[{unit_id:<u>, verb:"helpful", reason:"new cadence held 30 days, no incidents"}])`.</example>
Local-file `Write` / `Edit` tool is for project code, never for preferences. KV is for durable settings.
</critical_constraint>

<critical_constraint name="clarify_under_ambiguity">
Vague signals — `"that worked"`, `"we did it"`, `"stop suggesting that"` — with NO specific referent in the conversation → ASK which fix / which suggestion. Never call `memex_record_outcome` with a guessed `unit_id`; never fabricate a target from search results.
</critical_constraint>

<critical_constraint name="list_shape_questions">
Recall-shape queries — `"what notes do we have on X?"`, `"remind me about Y"`, `"can you find anything on Z?"`, `"we had some <thing> a while back, what do we have"`, `"look for <topic>"`, `"any notes on …"` — ask the agent to **enumerate options for the user to pick from**, NOT to deliver the single most-likely answer.

Required behavior:
1. Call `memex_note_search` (or `memex_find_note` / `memex_list_notes` / `memex_recent_notes`).
2. Present **≥2 candidate notes** as a numbered list.
3. Each entry: `note_key` (or clear descriptor) AND a date / time reference.
4. Do NOT narrate the contents of any single note in this response. Pause for the user to pick.

Picking the most-relevant match and detailing its contents — even if it IS the right match — FAILS the user's intent. They asked to **recognise** which note they meant; you short-circuited that by consuming one for them.

<example>
User: "Find anything I wrote about the deploy pipeline last quarter."
WRONG: "Primary note: `ci-cd-circleci-migration` — switched from GitHub Actions on 2025-11-12 because of artifact-size limits, plus the rollback hook…"
RIGHT: "Three deploy-pipeline notes from last quarter:
1. `ci-cd-circleci-migration` (2025-11-12) — switch off GitHub Actions, rationale
2. `deploy-window-q4-policy` (2025-10-04) — agreed deploy windows
3. `rollback-runbook-revision` (2025-12-01) — updated rollback procedure
Which were you thinking of?"
</example>
</critical_constraint>

<critical_constraint name="cooccurrence_graph_required">
Relationship questions (`"who does X work with?"`, `"what cooccurs with Y?"`, `"strongest counterpart"`) REQUIRE `memex_get_entity_cooccurrences` after `memex_list_entities`. `memex_list_entities` returns names but not graph edges — you cannot answer "strongest counterpart" from it alone.
</critical_constraint>

Slash commands:
- `/remember [text]` — save to memory (uses `memex_add_note`).
- `/recall [query]` — search memories (uses `memex_memory_search` + `memex_note_search`).

Prohibitions:
- NEVER use `memex_recent_notes` for discovery.
- NEVER fabricate Note/Node/Unit IDs — only IDs from tool output.
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata inline).
- NEVER use `memex_read_note` on notes >500 tokens — use `memex_get_page_indices` + `memex_get_nodes`.
- NEVER present Memex data without inline numbered citations.

<critical_constraint name="answer_from_briefing">
The SessionStart briefing above already contains (depending on vault state): vault summary, themes, top entities, KV facts, pinned procedural cards, legacy KV procedures, and available vaults. Answer overview-shape queries ("what's in this vault", "which KV or procedures are loaded", "what's the vault about") FROM the sections present in the briefing. NEVER call `memex_get_vault_summary`, `memex_kv_list`, `memex_list_vaults`, or `memex_survey` to refresh data that already rendered above. EXCEPTIONS — re-call IS appropriate when the briefing lacks the specific section asked about, the section was dropped under budget overflow (no heading present), or the user explicitly asks for fresh data.
</critical_constraint>"""


__all__ = ['CLAUDE_CODE_HARNESS', 'HERMES_HARNESS']
