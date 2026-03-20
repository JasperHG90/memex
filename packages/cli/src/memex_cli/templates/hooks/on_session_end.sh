#!/usr/bin/env bash
# Memex Claude Code Hook — SessionEnd
# Records a lightweight session marker note in Memex.
set -euo pipefail

# Guard: required tools must be on PATH
command -v uv >/dev/null 2>&1 || exit 0

STATE_DIR="__PROJECT_DIR__/.claude/hooks/memex/.state"
COMPACT_FILE="$STATE_DIR/compact_pending.json"

# Check if compaction occurred during this session
compact_note=""
if [ -f "$COMPACT_FILE" ]; then
    compact_note=" (context compaction occurred)"
    rm -f "$COMPACT_FILE"
fi

uv run memex note add \
    "Session ended${compact_note}." \
    --tags "session-marker" \
    2>/dev/null || exit 0
