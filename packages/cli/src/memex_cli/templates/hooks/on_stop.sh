#!/usr/bin/env bash
# Memex Claude Code Hook — Stop
# Nudges the agent to save noteworthy context to long-term memory when it finishes a turn.
set -euo pipefail

# Read hook input from stdin
input=$(cat)

# Guard: if stop_hook_active is true, this is already a continuation — exit to prevent loops
stop_active=$(echo "$input" | grep -o '"stop_hook_active"[[:space:]]*:[[:space:]]*true' || true)
if [ -n "$stop_active" ]; then
    exit 0
fi

cat <<'EOF'
{"decision": "block", "reason": "MEMORY CHECK: Did this turn involve: (1) completing a multi-step task, (2) diagnosing a bug, (3) an architectural/design decision, or (4) learning a user preference? If YES to any, call `memex_add_note` now with background=true, author='claude-code'. If nothing notable happened, you may stop."}
EOF
