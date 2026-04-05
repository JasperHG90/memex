---
name: retro
description: "Record a structured session postmortem to Memex. Captures what worked, what failed, tool performance, and improvement suggestions."
argument-hint: "[optional: focus area or session summary]"
---

# /retro — Agent Session Postmortem

You have been invoked via the `/retro` slash command.

## Instructions

1. **Fetch the template.**
   Call `memex_get_template("agent_reflection")` to retrieve the structured reflection
   template.

2. **Fill in the template sections.**
   Review the current session and populate each section:

   - **Session Summary**: 2-3 sentences covering the goal, scope, and outcome.
   - **What Worked**: Approaches, tools, or decisions that were effective. Be specific.
   - **What Failed**: Approaches that did not work, including root cause if known.
   - **Errors Encountered**: Error messages or failure modes and how they were resolved.
     Write "None" if no errors occurred.
   - **Tool Call Performance**: Fill in the table with tools used during the session,
     approximate call counts, successes, failures, and any notable context.
   - **Key Decisions**: Important decisions made and their rationale.
   - **Improvement Suggestions**: Actionable suggestions for future sessions.
   - **Follow-up Items**: Tasks or open questions to carry forward.

   If `$ARGUMENTS` is provided, use it to focus or contextualize the reflection.

3. **Save the reflection.**
   Call `memex_add_note` with:
   - **title**: "Session Reflection: [brief descriptor]"
   - **markdown_content**: The filled-in template.
   - **description**: A one-sentence summary of the session outcome.
   - **author**: `"claude-code"`
   - **tags**: `["agent-reflection", "session-postmortem"]` plus 1-2 topic tags.
   - **template**: `"agent_reflection"`
   - **background**: `false`

   Synchronous ingestion ensures entities are extracted immediately, triggering
   reflection on session learnings.

4. **Confirm to the user.**
   Briefly summarize the reflection and mention the note title.
