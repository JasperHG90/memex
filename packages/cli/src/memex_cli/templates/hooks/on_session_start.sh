#!/usr/bin/env bash
# Memex Claude Code Hook — SessionStart
# Automatically loads recent memories at session start.
set -euo pipefail

# Guard: required tools must be on PATH
command -v uv  >/dev/null 2>&1 || exit 0
command -v jq  >/dev/null 2>&1 || exit 0

# Fetch recent notes (silent failure keeps the session working)
notes=$(uv run memex note recent --limit 5 --json 2>/dev/null) || exit 0

count=$(echo "$notes" | jq 'length' 2>/dev/null) || exit 0
if [ "$count" -eq 0 ]; then
    exit 0
fi

echo "## Recent Memex Memories"
echo ""
echo "$notes" | jq -r '.[] | "- **\(.title)** (\(.created_at // "unknown date")): \(.description // "")"' 2>/dev/null || exit 0
