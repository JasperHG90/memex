---
name: research
description: "Deep research on a topic using Memex. Performs multi-step search, entity exploration, and note reading to compile a comprehensive answer."
argument-hint: "[research topic or question]"
---

# /research -- Deep Research via Memex

You have been invoked via the `/research` slash command.

## Memex API calls

All Memex operations use the plugin's `mx` helper. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <command> '<json_args>'
```

## Instructions

### Step 1. Parse the research topic.

- Use `$ARGUMENTS` as the research topic.
- If `$ARGUMENTS` is empty, ask the user what they would like to research.
- Formulate 2-3 search query variants from the topic to maximize coverage.

### Step 2. Phase 1 -- Broad discovery.

Run these four searches in parallel (use all strategies for maximum recall):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" memory-search '{"query":"<QUERY>","limit":15}'
"${CLAUDE_PLUGIN_ROOT}/bin/mx" note-search '{"query":"<QUERY>","limit":15,"expand_query":true}'
"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-entities '{"query":"<QUERY>"}'
"${CLAUDE_PLUGIN_ROOT}/bin/mx" kv-search '{"query":"<QUERY>"}'
```

Collect all note IDs, entity IDs, and KV results. Deduplicate note IDs across result sets.

### Step 3. Phase 2 -- Entity graph walk.

For the top 3 most relevant entities from Step 2 (run in parallel per entity):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-cooccurrences "<ENTITY_ID>"
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-mentions "<ENTITY_ID>"
```

Record co-occurring entities and the source facts that link back to notes.
Add any newly discovered note IDs to the read queue.

### Step 4. Phase 3 -- Deep note reading.

For the top 3-5 ranked notes (by frequency across searches and relevance):

a. Get the table of contents:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-page-indices "<NOTE_ID>"
```

b. Read the most relevant sections:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-nodes '{"node_ids":["<NODE_ID_1>","<NODE_ID_2>"]}'
```

c. If the page index shows `has_assets: true`:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-assets "<NOTE_ID>"
```
Then read relevant assets as needed.

### Step 5. Phase 4 -- Synthesis.

Compile a structured research report with the following sections:

- **Executive Summary** -- 2-3 sentence overview of findings.
- **Key Findings** -- numbered list of discoveries, each with inline [N] citations.
- **Entity Relationship Map** -- describe how the key entities connect to each other (use text or ASCII diagram).
- **Knowledge Gaps** -- what the research did NOT find or what remains unclear.

### Step 6. Phase 5 -- Optional persist.

Ask the user: "Would you like me to save this research report to Memex?"

If the user agrees:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" add-note '{"content":"<REPORT_MARKDOWN>","name":"<TITLE>","tags":["claude-code","research","<TOPIC>"]}'
```

### Step 7. Citations (MANDATORY).

Every claim sourced from Memex MUST have an inline [N] citation.
End the response with a numbered reference list. Each entry uses a type prefix:
- `[note]` title + note ID
- `[memory]` title + memory ID + source note ID
- `[asset]` filename + note ID

Example:
```
[1] [note] API Design Principles -- 2eb202ed-bee6-7b2a-f0b9-917e8d5dd6f0
[2] [memory] TEMPR uses five retrieval strategies -- abc123 (source: def456)
```
