#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Records a lightweight session marker note in Memex.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
COMPACT_FILE="$STATE_DIR/compact_pending.json"

# Check if compaction occurred during this session
compact_note=""
if [ -f "$COMPACT_FILE" ]; then
    compact_note=" (context compaction occurred)"
    rm -f "$COMPACT_FILE"
fi

# Add session marker note via CLI (fails silently if server is unreachable)
memex note add "Session ended${compact_note}." \
    --tag session-marker --author claude-code \
    >/dev/null 2>&1 || true

exit 0
