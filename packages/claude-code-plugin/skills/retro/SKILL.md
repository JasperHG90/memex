---
name: retro
description: "Record a structured session postmortem to Memex and propose durable learnings for CLAUDE.md."
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

   Synchronous ingestion ensures entities are extracted immediately and queued
   for reflection. Reflection runs on a background schedule, but extraction
   happens before this call returns.

4. **Extract durable learnings for CLAUDE.md.**
   Review the filled-in retro — specifically **What Worked**, **What Failed**,
   **Key Decisions**, and **Improvement Suggestions** — and extract learnings that are:

   - **Durable**: Will remain relevant across future sessions (not one-off fixes).
   - **Actionable**: Can be expressed as a concrete instruction or constraint
     (e.g., "always X when Y", "never do Z because W").
   - **Non-redundant**: Not already covered by existing CLAUDE.md content.

   Exclude learnings that are:
   - Derivable from reading the code or git history.
   - Specific to a single bug fix or transient situation.
   - Already captured by existing CLAUDE.md instructions.

   Read the project CLAUDE.md (`CLAUDE.md` in the repo root) to check for overlap.

5. **Propose CLAUDE.md additions to the user.**
   If you identified durable learnings in Step 4, present them to the user as a
   numbered list. For each, show:
   - The proposed instruction text (concise, imperative style).
   - A one-line rationale from the session (why this matters).

   Ask the user which ones (if any) they want added. Accepted formats:
   - "all" — add everything proposed.
   - "1, 3" — add specific items by number.
   - "none" — skip CLAUDE.md updates entirely.

   If no durable learnings were identified, skip this step and tell the user.

6. **Append approved learnings to CLAUDE.md.**
   For approved items:
   - Read the current CLAUDE.md.
   - If a `## Learnings` section exists, append to it.
   - If not, create a `## Learnings` section at the end of the file.
   - Format each learning as a bullet point: `- <instruction> — <rationale>`.
   - Do NOT modify any other part of CLAUDE.md.

7. **Confirm to the user.**
   Briefly summarize:
   - The reflection note title (saved to Memex).
   - How many learnings were added to CLAUDE.md (if any), or that none were added.
