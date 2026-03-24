---
name: digest
description: "Summarize the current session's key decisions, discoveries, and outcomes, then save to Memex long-term memory."
argument-hint: ""
---

# /digest -- Save Session Digest to Memex

You have been invoked via the `/digest` slash command.

## Memex API calls

All Memex operations use the plugin's `mx` helper. `${CLAUDE_PLUGIN_ROOT}` is an environment variable set by Claude Code in every Bash call.

```
"${CLAUDE_PLUGIN_ROOT}/bin/mx" <command> '<json_args>'
```

## Instructions

### Step 1. Review the conversation.

Scan the entire conversation history and extract items in these categories:

- **Decisions made** -- architectural choices, technology selections, config changes.
- **Bugs diagnosed** -- root causes found, error patterns identified, fixes applied.
- **User preferences learned** -- coding style, workflow habits, tool preferences.
- **Tasks completed** -- features built, refactors done, tests written.
- **Config/environment discoveries** -- settings found, environment quirks, integration details.

Skip trivial or transient items (e.g. typo fixes, routine file reads).
Each item should be a concise note of at most 300 tokens.

### Step 2. Check for overlap with recent notes.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" list-notes '{"limit":5}'
```

Review the recent notes to avoid duplicating information already saved.

### Step 3. Save each significant item.

For each item identified in Step 1:

a. Compose a concise, self-contained note in Markdown. Include enough context
   that the note is useful in a future session without the original conversation.

b. Run a dedup check against the title:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" find-note "<TITLE>"
```
If a high-similarity match exists (score > 0.8), skip this item and note the overlap.

c. Save the note:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/mx" add-note '{"content":"<MARKDOWN_CONTENT>","name":"<TITLE>","tags":["claude-code","session-digest","<TOPIC>"]}'
```

### Step 4. Report what was saved.

Provide a summary to the user:

- Total number of notes saved.
- Title of each saved note.
- Any items skipped due to dedup overlap (mention the existing note title).
