#!/usr/bin/env bash
# Memex Claude Code Plugin — PreCompact
# Nudges the agent to persist important context before compaction discards it.
# Behavioral rules are now persistent via .claude/rules/memex.md — no need to
# re-inject them here.
set -euo pipefail

cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreCompact","additionalContext":"Context compaction is imminent — conversation history will be compressed. Before continuing, review this session for anything worth persisting to long-term memory via `memex_add_note` (background: true). Save if: (1) you diagnosed a bug root cause, (2) made or discovered an architectural decision, (3) learned a user preference or workflow pattern, (4) completed a multi-step task with reusable insights. Skip if nothing notable happened.\n\nConsider running `/reflect` to record a structured session postmortem before context is lost."}}
EOF
