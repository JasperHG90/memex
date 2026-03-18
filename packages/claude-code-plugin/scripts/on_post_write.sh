#!/usr/bin/env bash
# Memex Claude Code Plugin — PostToolUse (Write/Edit)
# After significant file writes, reminds the agent to consider saving to memory.
# Throttled: only fires every 10th write to reduce context window overhead.
set -euo pipefail

# Read tool input from stdin
input=$(cat)

# Extract the file_path from the tool input
file_path=$(echo "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)

# Skip trivial / generated files
case "$file_path" in
    *node_modules*|*package-lock*|*.lock|*__pycache__*|*.pyc|*dist/*|*.min.js|*.min.css|*.map)
        echo '{}'
        exit 0
        ;;
esac

# Throttle: only fire every 10th write
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR"
COUNTER_FILE="${STATE_DIR}/write_count"

count=0
[ -f "$COUNTER_FILE" ] && count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNTER_FILE"

if [ $((count % 10)) -ne 0 ]; then
    echo '{}'
    exit 0
fi

cat <<'EOF'
{"systemMessage": "10+ files written this session. If you completed a meaningful task (feature, fix, refactor), save a summary via `memex_add_note` (background: true, author: 'claude-code')."}
EOF
