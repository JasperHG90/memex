#!/usr/bin/env bash
# Memex Claude Code Hook — PostToolUse (Write/Edit)
# After significant file writes, reminds the agent to consider saving to memory.
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

cat <<'EOF'
{"systemMessage": "A file was just written/edited. If this completed a meaningful task (feature, fix, refactor), consider saving a summary to long-term memory via `memex_add_note` (background: true, author: 'claude-code')."}
EOF
