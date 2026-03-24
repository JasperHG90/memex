---
name: remember
description: "Save information to Memex long-term memory. Captures the given text (or infers the most important context from the conversation) as a persistent note."
argument-hint: "[what to remember]"
---

# /remember -- Save to Memex Long-Term Memory

You have been invoked via the `/remember` slash command.

## Memex API calls

All Memex operations use the plugin's `mx` helper. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <command> '<json_args>'
```

## Instructions

### Step 1. Determine what to remember.

- If `$ARGUMENTS` is provided and non-empty, use that text as the memory content.
- If `$ARGUMENTS` is empty, review the recent conversation and identify the single
  most important piece of information worth persisting (e.g. a decision, a discovery,
  a user preference, a bug fix).

### Step 2. Classify content type and select template.

Determine which category best fits the content, then read the corresponding template:

| Content Type              | Template File                                           |
| :------------------------ | :------------------------------------------------------ |
| Bug fix / implementation  | `${CLAUDE_PLUGIN_ROOT}/templates/technical_brief.md`    |
| Architecture decision     | `${CLAUDE_PLUGIN_ROOT}/templates/adr.md`                |
| Proposal / RFC            | `${CLAUDE_PLUGIN_ROOT}/templates/rfc.md`                |
| General note              | `${CLAUDE_PLUGIN_ROOT}/templates/general_note.md`       |
| Quick capture (default)   | `${CLAUDE_PLUGIN_ROOT}/templates/quick_note.md`         |

Read the selected template file to get the structure. If classification is unclear,
default to `quick_note.md`.

### Step 3. Fill in the template.

Populate the template with the content from Step 1. Be specific and include enough
context so the note is useful in a future session without the original conversation.

- **Title**: concise, descriptive (max 10 words).
- **Body**: follow the template structure. Be thorough but not verbose.
- **Tags**: always include `claude-code` and `manual-capture`. Add 1-3 topic tags
  derived from the content.

### Step 4. Dedup check.

Search for an existing note with a similar title:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" find-note "<TITLE>"
```

If a high-similarity match exists (score > 0.8), warn the user that a similar note
already exists and show the existing title. Ask whether to proceed or skip.

### Step 5. Save the note.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" add-note '{"content":"<FILLED_TEMPLATE_MARKDOWN>","name":"<TITLE>","tags":["claude-code","manual-capture","<TOPIC>"],"vault_id":"<VAULT>"}'
```

- Set `vault_id` from the session context (use the project vault if one is configured).
  Omit `vault_id` to use the default vault.
- `background` defaults to `true` (ingestion runs asynchronously).

### Step 6. Confirm to the user.

After saving, report:
- The note title.
- The note ID returned in the JSON output.
- That background ingestion is in progress (facts and entities will be extracted shortly).
