---
name: remember
description: "Save information to Memex long-term memory. Captures the given text (or infers the most important context from the conversation) as a persistent note."
argument-hint: "[what to remember]"
---

# /remember — Save to Memex Long-Term Memory

You have been invoked via the `/remember` slash command.

## Instructions

1. **Determine what to remember.**
   - If `$ARGUMENTS` is provided and non-empty, use that text as the memory content.
   - If `$ARGUMENTS` is empty, review the recent conversation and identify the single
     most important piece of information worth persisting (e.g. a decision, a discovery,
     a user preference).

2. **Format the memory.**
   - **title**: A concise, descriptive title (≤10 words).
   - **markdown_content**: The memory body in Markdown. Be specific and include enough
     context so the memory is useful in a future session without the original conversation.
   - **description**: A one-sentence summary (≤250 words).
   - **author**: `"claude-code"`
   - **tags**: Always include `"claude-code"` and `"manual-capture"`. Add 1-3 additional
     topic tags derived from the content.

3. **Consider a template (for structured content).**
   If the memory is an architectural decision, technical brief, retro, or RFC,
   call `memex_list_templates` then `memex_get_template(slug)`, follow the
   structure when writing `markdown_content`, and pass `template: "<slug>"` to
   `memex_add_note`. Skip for short, unstructured captures.

4. **Save the memory.**
   Call the `memex_add_note` MCP tool with the values above and set `background: true`
   so ingestion does not block the conversation.

5. **Confirm to the user.**
   After calling the tool, briefly confirm what was saved and mention the title.

## Curating memory after the fact (deprioritize vs archive)

When a previously-saved memory turns out to be misleading, outdated, or noise
that contaminates retrieval, prefer the **NON-DESTRUCTIVE** verb:

- `memex_memory_deprioritize(unit_id, reason)` lowers the unit's retrieval
  rank without removing it from the entity graph. The unit remains accessible
  via `include_deprioritized=true` retrieval. Use the `reason` field liberally
  ("user confirmed issue fixed", "superseded by v2.3 release") — it is logged
  to `audit_logs` as the audit trail. Reversible via `memex_memory_restore`.
- Archive (CLI-only, `memex memory delete`) is the **DESTRUCTIVE** counterpart:
  it removes the unit from the entity graph and is irreversible. Prefer
  deprioritize unless the unit MUST leave the graph entirely.

<!--
Tier A — /remember verb extensions
F4:  WS-quick-wins  (memory_deprioritize/restore disclosure)
F5:  WS-quick-wins  (memory_summarize_node disclosure)
F9:  WS-locks       (memory_reconsolidate, memory_consolidate disclosure)
F14: WS-quick-wins  (procedural KV capture surfacing)
F20: WS-revisit     (memory_review disclosure)

# --- F4 ---  (filled by WS-quick-wins — see "Curating memory after the fact" section above)

# --- F5 ---  (filled by WS-quick-wins)

# --- F9 ---  (filled by WS-locks)

# --- F14 --- (filled by WS-quick-wins)

# --- F20 --- (filled by WS-revisit)
-->
