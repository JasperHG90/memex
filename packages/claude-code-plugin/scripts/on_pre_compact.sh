#!/usr/bin/env bash
# Memex Claude Code Plugin — PreCompact
# Nudges the agent to persist important context before compaction discards it,
# and re-injects key behavioral reminders.
set -euo pipefail

cat <<'EOF'
{"systemMessage": "Context compaction is imminent — conversation history will be compressed. Before continuing, review this session for anything worth persisting to long-term memory via `\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" add-note` (background: true). Save if: (1) you diagnosed a bug root cause, (2) made or discovered an architectural decision, (3) learned a user preference or workflow pattern, (4) completed a multi-step task with reusable insights. Skip if nothing notable happened this session.\n\nReminder — Memex behavioral rules that must survive compaction:\n- ALWAYS cite Memex data with inline [N] references and a reference list.\n- Route retrieval by query type: title → `\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" find-note`, relationships → entity tools, content → `\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" memory-search`/`\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" note-search`.\n- Proactively capture insights via `\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" add-note` (background: true, author: 'claude-code', ≤300 tokens).\n- NEVER use `\"${CLAUDE_PLUGIN_ROOT}/bin/mx\" list-notes` for discovery or fabricate IDs."}
EOF
