#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Records a lightweight session marker note in Memex.
set -euo pipefail

# Memex CLI invocation — pinned to the GitHub repository
MEMEX_FROM="memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
MEMEX=(uvx --from "$MEMEX_FROM" memex)

# Guard: uvx must be on PATH
command -v uvx >/dev/null 2>&1 || exit 0

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
COMPACT_FILE="$STATE_DIR/compact_pending.json"

# Check if compaction occurred during this session
compact_note=""
if [ -f "$COMPACT_FILE" ]; then
    compact_note=" (context compaction occurred)"
    rm -f "$COMPACT_FILE"
fi

"${MEMEX[@]}" note add \
    "Session ended${compact_note}." \
    --tags "session-marker" \
    2>/dev/null || exit 0
