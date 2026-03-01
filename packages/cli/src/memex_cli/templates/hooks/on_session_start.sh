#!/usr/bin/env bash
# Memex Claude Code Hook — SessionStart
# Automatically loads recent memories at session start.
set -euo pipefail

# Guard: uv must be on PATH
command -v uv >/dev/null 2>&1 || exit 0

# Fetch recent notes in compact format (silent failure keeps the session working)
output=$(uv run memex note recent --limit 5 --compact 2>/dev/null) || exit 0

[ -z "$output" ] && exit 0

echo "## Recent Memex Memories"
echo ""
echo "$output"
