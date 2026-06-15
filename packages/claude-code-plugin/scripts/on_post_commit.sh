#!/usr/bin/env bash
# Memex Claude Code Plugin — PostToolUse (Bash)
# After a git commit, a thin just-in-time trigger: ask the one routing
# question; the full case-vs-note rule lives in the agent surface.
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

# Only trigger on git commit commands. Match `git commit` as a COMMAND, not as
# a substring — so it fires for compound commits (`git add … && git commit …`,
# `cd x && git commit`, `git commit … && git push`), which a `git commit*`
# prefix glob silently missed (the project routinely stages then commits in one
# Bash call). The anchor `(^|[;&|]|&&)` requires `git commit` to start a command
# segment, so a quoted mention (`echo "git commit"`, `git log --grep "git commit"`)
# does NOT false-trigger.
if [[ "$command_field" =~ (^|[;\&\|])[[:space:]]*git[[:space:]]+commit($|[[:space:]]) ]]; then
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"A commit was just made. Ask: next time I hit this, would I want these steps back? If yes — you worked out HOW to do or fix something non-obvious — file a case: memex_case_submit (trigger/actions/outcome/lesson), which becomes a reusable procedure. If it is only a durable decision or fact, memex_add_note. If routine (it just worked, typo, formatting), skip. When unsure between case and note, pick note."}}
EOF
else
    echo '{}'
fi
