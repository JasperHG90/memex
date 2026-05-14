## Search query formulation

<constraint name="search-queries" priority="high">
ALWAYS formulate search queries as natural language, NEVER as keyword lists.
ALWAYS preserve proper nouns, amounts, dates, qualifiers from the original question.
ALWAYS search for the subject/activity, NOT the answer type.
</constraint>

## Authoritative sources

The MCP server ships the **retrieval routing**, **storage model**, **KV namespace rules**, and the **5-step resolution flow** inside its session instructions and per-tool descriptions. Follow those — they are the contract. This rule file only carries the agent-side framing that does not belong in tool descriptions.

## Memex capture — MANDATORY

Hard max: 300 tokens per note. No per-file changelogs.

Call `memex_add_note` (background: true, author: "claude-code") when:
1. Completed a multi-step task (what, decisions, outcome)
2. Diagnosed a bug root cause (symptom, cause, fix)
3. Made/discovered an architectural decision (decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

Do NOT save: per-file changelogs, info derivable from code, git history, the fix itself (save the insight why), ephemeral task details.

## Memex citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim
2. Reference list: `[note]` title + note ID, `[memory]` title + memory ID + source note ID, `[asset]` filename + note ID

## Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories

## Memex prohibitions

- NEVER use `memex_recent_notes` for discovery
- NEVER fabricate Note/Node/Unit IDs — only use IDs from tool output
- NEVER call `memex_get_notes_metadata` after `memex_note_search` (metadata inline)
- NEVER use `memex_read_note` on notes over 500 tokens — use page_indices + get_nodes
- NEVER create diagrams without first checking assets
- NEVER present Memex data without citations
