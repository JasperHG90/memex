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
{"decision": "block", "reason": "MEMORY CHECK: Save notable work (bug fix, decision, feature, preference) via memex_add_note. Skip if trivial."}
EOF
