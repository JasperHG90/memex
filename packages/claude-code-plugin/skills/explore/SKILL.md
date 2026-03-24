---
name: explore
description: "Navigate the Memex entity knowledge graph. Discover relationships, co-occurrences, and source facts for entities."
argument-hint: "[entity name or topic]"
---

# /explore -- Navigate the Memex Knowledge Graph

You have been invoked via the `/explore` slash command.

## Memex API calls

All Memex operations use the plugin's `mx` helper. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <command> '<json_args>'
```

## Instructions

### Step 1. Parse the entity or topic.

- Use `$ARGUMENTS` as the entity or topic to explore.
- If `$ARGUMENTS` is empty, ask the user what entity or topic they want to explore.

### Step 2. Find matching entities.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-entities '{"query":"<QUERY>"}'
```

Review the returned entities (IDs, types, mention counts). If no entities match,
tell the user and suggest alternative queries or broader terms.

### Step 3. Explore each relevant entity.

For each relevant entity from Step 2, run these two calls in parallel:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-cooccurrences "<ENTITY_ID>"
"${CLAUDE_PLUGIN_ROOT}/bin/mx" get-entity-mentions "<ENTITY_ID>"
```

- **Co-occurrences** return related entities with names, types, and co-occurrence counts.
  This data is complete inline -- no follow-up call is needed.
- **Mentions** return source facts that link back to notes, showing where and how
  the entity appears in the knowledge base.

Repeat for all relevant entities (run in parallel across entities where possible).

### Step 4. Present the entity graph.

Format the results as a clear entity map:

- **Entities** -- list each entity with its type and mention count.
- **Relationships** -- for each entity, list its co-occurring entities with
  co-occurrence counts. Group by relationship strength.
- **Supporting Facts** -- include the key source facts from mentions that
  explain the relationships. Cite each fact.

Use plain text or ASCII diagrams to illustrate the graph structure.
Do NOT fabricate relationships that are not in the data.

### Step 5. Citations (MANDATORY).

Every fact sourced from Memex MUST have an inline [N] citation.
End the response with a numbered reference list. Each entry uses a type prefix:
- `[note]` title + note ID
- `[memory]` title + memory ID + source note ID

Example:
```
[1] [note] System Architecture Overview -- 2eb202ed-bee6-7b2a-f0b9-917e8d5dd6f0
[2] [memory] pgvector stores entity embeddings -- abc123 (source: def456)
```
