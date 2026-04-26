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
