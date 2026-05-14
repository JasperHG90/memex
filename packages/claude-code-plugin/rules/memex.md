<!--
This file is intentionally minimal. Universal Memex doctrine (capture
cadence, slash commands, prohibitions, citations) arrives at session start
via the plugin's SessionStart hook → `memex agent-surface --for=claude-code`.
Only Claude-Code-specific content that is NOT covered by that pipeline
belongs here. See packages/claude-code-plugin/scripts/on_session_start.sh
and packages/common/src/memex_common/agent_harnesses.py (CLAUDE_CODE_HARNESS).
-->

## Search query formulation

<constraint name="search-queries" priority="high">
ALWAYS formulate search queries as natural language, NEVER as keyword lists.
ALWAYS preserve proper nouns, amounts, dates, qualifiers from the original question.
ALWAYS search for the subject/activity, NOT the answer type.
</constraint>
