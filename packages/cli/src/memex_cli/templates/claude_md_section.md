
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex Memory Integration

You have access to **Memex**, a long-term memory system, via MCP tools. Use these to
build persistent knowledge across sessions.

### Proactive memory capture

Call `memex_add_note` (with `background: true`) when you encounter:

- **Architectural decisions** or design rationale
- **Bug root causes** and their fixes
- **User preferences** and workflow patterns
- **Important project context** that would be useful in future sessions
- **Key technical discoveries** or learnings

**Do NOT capture**: trivial exchanges, routine code edits, debugging noise, or
information already in the codebase.

### Memory retrieval

Use `memex_search` or `memex_note_search` when:

- Starting a new session to recall prior context
- The user asks "what do you know about X"
- You need background on a topic discussed in a previous session

### Slash commands

- `/remember [text]` — explicitly save something to memory
- `/recall [query]` — search your memories
