#!/usr/bin/env bash
# Memex Claude Code Plugin — PostToolUse (Bash)
# After a git commit, reminds the agent to capture the change in long-term memory.
set -euo pipefail
trap 'echo "{}"; exit 0' ERR

# Read tool input from stdin
input=$(cat)

# --- Dependency check ---
if ! command -v jq >/dev/null 2>&1; then
    # Fallback to grep/sed if jq unavailable
    command_field=$(echo "$input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)
else
    command_field=$(echo "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
fi

# Only trigger on git commit commands
case "$command_field" in
    git\ commit*)
        cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"A git commit was just made. Consider whether this change is worth saving to long-term memory via `memex_add_note` (background: true). Good candidates: bug fixes with non-obvious root causes, new features or architectural changes, configuration decisions. Skip for trivial commits (typos, formatting, minor tweaks)."}}
EOF
        ;;
    *)
        echo '{}'
        ;;
esac
