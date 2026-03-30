#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Records a lightweight session marker note in Memex.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

# Quick health check — skip if server is down
curl -sf --max-time 2 ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} "${API}/health" >/dev/null 2>&1 || exit 0

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
COMPACT_FILE="$STATE_DIR/compact_pending.json"

# Check if compaction occurred during this session
compact_note=""
if [ -f "$COMPACT_FILE" ]; then
    compact_note=" (context compaction occurred)"
    rm -f "$COMPACT_FILE"
fi

# Add session marker note via API
curl -sf --max-time 3 ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -X POST "${API}/notes" \
    -H "Content-Type: application/json" \
    -d "{\"content\":\"Session ended${compact_note}.\",\"tags\":[\"session-marker\"],\"author\":\"claude-code\"}" \
    >/dev/null 2>&1 || true

exit 0
