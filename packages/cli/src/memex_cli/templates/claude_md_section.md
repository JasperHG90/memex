
<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

<constraint name="proactive-memory-capture" priority="critical">
### Capture — MANDATORY

You MUST call `memex_add_note` (with `background: true`, `author: "claude-code"`) at these trigger points. This is not optional — failing to save important context means future sessions lose valuable knowledge.

**Trigger checklist (run this mentally before finishing any substantive turn):**

1. **Completed a multi-step task?** → Save what was done, key decisions, and outcome
2. **Diagnosed a bug root cause?** → Save the symptom, root cause, and fix
3. **Made or discovered an architectural decision?** → Save the decision, alternatives, and rationale
4. **Learned a user preference or workflow pattern?** → Save it for future sessions
5. **Resolved a tricky configuration or environment issue?** → Save the solution

If ANY of the above apply, call `memex_add_note` IMMEDIATELY — do not wait until later.

A Stop hook will remind you at the end of each turn. When you see the "MEMORY CHECK" message, evaluate the checklist and act.

#### Example of a good memory note

```
title: "Fix: connection pool exhaustion under concurrent reflection"
tags: ["bugfix", "postgresql", "reflection", "connection-pool"]
description: "Root cause and fix for connection pool exhaustion during concurrent reflection tasks."
markdown_content: |
  ## Problem
  Reflection tasks running concurrently exhaust the PostgreSQL connection pool,
  causing asyncpg.exceptions.TooManyConnectionsError.

  ## Root Cause
  Each reflection task called asyncpg.connect() directly instead of using the shared pool.

  ## Fix
  Changed ReflectionWorker to accept a connection pool in its constructor
  and use pool.acquire() for each task.
```

#### What NOT to capture

- Trivial exchanges or routine code edits (formatting, typos)
- Intermediate debugging attempts that did not lead to a solution
- Information already in the codebase (README, CLAUDE.md, docstrings)
- Routine file reads or searches with no novel findings

#### How to capture

- Use a **descriptive title** — include the type: "Fix:", "Decision:", "Pattern:", "Config:"
- Include **context**: what project, what problem, what was decided
- Add **tags** for retrieval (e.g., `["architecture", "auth", "decision"]`)
- Always set `background: true` to avoid blocking the conversation
- Always set `author: "claude-code"`
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
