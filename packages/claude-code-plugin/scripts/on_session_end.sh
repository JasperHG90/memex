#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Gathers quantitative session stats and posts a lightweight marker note.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

# --- Gather quantitative stats ---
commits=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # Count commits made in the last 4 hours (approximate session window)
    commits=$(git log --oneline --since="4 hours ago" --author="$(git config user.name 2>/dev/null || echo '')" 2>/dev/null | wc -l | tr -d ' ') || commits=0
fi

# Count file writes from the state counter (set by on_post_write.sh)
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
writes=0
COUNTER_FILE="${STATE_DIR}/write_count"
if [ -f "$COUNTER_FILE" ]; then
    writes=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
    rm -f "$COUNTER_FILE"
fi

# Build stats summary
stats="commits: ${commits}, file writes: ${writes}"

# Post marker note via CLI
memex note add \
    "Session ended. Stats: ${stats}." \
    --tags "session-marker" --tags "agent-reflection" \
    2>/dev/null || true

# Output empty JSON (SessionEnd hooks do not inject context)
echo '{}'
