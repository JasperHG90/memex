#!/usr/bin/env bash
# Memex Claude Code Plugin — PreCompact
# Nudges the agent to persist important context before compaction discards it,
# and re-injects key behavioral reminders.
set -euo pipefail

cat <<'EOF'
{"systemMessage": "Context compaction is imminent — conversation history will be compressed. Before continuing, review this session for anything worth persisting to long-term memory via `memex_add_note` (background: true). Save if: (1) you diagnosed a bug root cause, (2) made or discovered an architectural decision, (3) learned a user preference or workflow pattern, (4) completed a multi-step task with reusable insights. Skip if nothing notable happened this session.\n\nConsider running `/reflect` to record a structured session postmortem before context is lost.\n\nReminder — Memex behavioral rules that must survive compaction:\n- ALWAYS cite Memex data with inline [N] references and a reference list.\n- Route retrieval by query type: title → `memex_find_note`, relationships → entity tools, content → `memex_memory_search`/`memex_note_search`.\n- Proactively capture insights via `memex_add_note` (background: true, author: 'claude-code', ≤300 tokens).\n- NEVER use `memex_recent_notes` for discovery or fabricate IDs."}
EOF
