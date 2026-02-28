#!/usr/bin/env bash
# Memex Claude Code Hook — PostToolUse (Bash)
# After a git commit, reminds the agent to capture the change in long-term memory.
set -euo pipefail

# Read tool input from stdin
input=$(cat)

# Extract the command field (no jq dependency)
command_field=$(echo "$input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)

# Only trigger on git commit commands
case "$command_field" in
    git\ commit*)
        cat <<'EOF'
{"systemMessage": "A git commit was just made. Consider whether this change is worth saving to long-term memory via `memex_add_note` (background: true). Good candidates: bug fixes with non-obvious root causes, new features or architectural changes, configuration decisions. Skip for trivial commits (typos, formatting, minor tweaks)."}
EOF
        ;;
    *)
        echo '{}'
        ;;
esac
