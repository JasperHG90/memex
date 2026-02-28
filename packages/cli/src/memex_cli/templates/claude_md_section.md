
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

### Capture

Call `memex_add_note` (background: true) for: architectural decisions, bug root causes, user preferences, important project context, key technical discoveries.
Do NOT capture: trivial exchanges, routine edits, debugging noise, information already in codebase.

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
