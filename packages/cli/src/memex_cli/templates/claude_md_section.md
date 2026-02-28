
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

<constraint name="proactive-memory-capture" priority="critical">
### Capture

You MUST call `memex_add_note` (with `background: true`) at these trigger points:

1. **After completing a multi-step task** — summarize what was done, key decisions, and outcome
2. **After diagnosing a bug root cause** — record the symptom, root cause, and fix
3. **After making an architectural decision** — capture the decision, alternatives considered, and rationale
4. **After learning a user preference** — record workflow preferences, tool choices, or communication style

#### What NOT to capture

- Trivial exchanges or routine code edits
- Debugging noise (intermediate failed attempts)
- Information already in the codebase (README, CLAUDE.md, docstrings)

#### How to capture

- Use a **descriptive title** (not "Untitled" or "Note")
- Include **context**: what project, what problem, what was decided
- Add **tags** for retrieval (e.g., `["architecture", "auth", "decision"]`)
</constraint>

### Retrieval

Session start context is automatic via `on_session_start` hook. Do NOT redundantly search at session start.
PROHIBITED: `memex_list_notes` for discovery (titles are often "Untitled").

### Memory search vs. note search

These are distinct search interfaces. Choose based on what you need:

`memex_search` — searches atomic facts, observations, and mental models across the knowledge graph (TEMPR architecture). Use when you want a synthesized answer spanning the entire knowledge base, exploring entity relationships, or answering general questions. Think: "What do we know about X globally?"

`memex_note_search` — searches raw source notes (PDFs, Markdown, web scrapes) via hybrid RRF. Supports `reason=True` (identify relevant sections) and `summarize=True` (synthesize answer from matched sections). Use when you need a specific quote, original formatting/context, or scoped search within notes. Think: "Find the note where X is mentioned."

### Note reading

1. `memex_get_page_index` (Note ID → table of contents)
2. `memex_get_node` (node ID → section text)
3. Fallback only: `memex_read_note` (small notes or when page index unavailable)

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
