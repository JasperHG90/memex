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
- Success → "that worked", "lock it in", "record it", "that's the lesson" → verb=`helpful`
- Failure → "stop suggesting X", "didn't work", "we removed it", "that was wrong" → verb=`not_helpful`

Capture cadence: write a short note (`memex_add_note`, ≤300 tokens, no per-file changelogs) when you finish a multi-step task, diagnose a non-obvious bug, or resolve a tricky env issue. User preferences / conventions are NOT note-shaped — those go to `memex_kv_write` per the KV-namespace rules above. Use `memex_append_note(note_key, delta)` to extend an existing note rather than re-ingesting."""


CLAUDE_CODE_HARNESS = """## Claude Code-specific framing

Capture cadence: call `memex_add_note(background=true, author="claude-code")` when you (1) complete a multi-step task, (2) diagnose a bug root cause, (3) make/discover an architectural decision, or (4) resolve a tricky env issue. Hard max 300 tokens; no per-file changelogs. User preferences / conventions are NOT note-shaped — those go to `memex_kv_write` per the KV-namespace rules above.

<critical_constraint name="preference_routing">
"Remember"/"save this"/"for future sessions" directives for preferences or conventions go to `memex_kv_write` — see KV namespace rules above. Do NOT use the built-in `Write` tool to save them into CLAUDE.md / AGENTS.md / .memex/ / any local file. Local files are for project code; KV is for durable settings.
</critical_constraint>

Slash commands:
- `/remember [text]` — save to memory (uses `memex_add_note`).
- `/recall [query]` — search memories (uses `memex_memory_search` + `memex_note_search`).

Prohibitions:
- NEVER use `memex_recent_notes` for discovery.
- NEVER fabricate Note/Node/Unit IDs — only IDs from tool output.
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata inline).
- NEVER use `memex_read_note` on notes >500 tokens — use `memex_get_page_indices` + `memex_get_nodes`.
- NEVER present Memex data without inline numbered citations."""


__all__ = ['CLAUDE_CODE_HARNESS', 'HERMES_HARNESS']
