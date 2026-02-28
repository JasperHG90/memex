#!/usr/bin/env bash
# Memex Claude Code Hook — PreCompact
# Warns before context compaction and records the event.
set -euo pipefail

STATE_DIR="__PROJECT_DIR__/.claude/hooks/memex/.state"
mkdir -p "$STATE_DIR"

# Record compaction event
echo "{\"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$STATE_DIR/compact_pending.json"

echo "## Context Compaction Imminent"
echo ""
echo "Context compaction is about to discard conversation history."
echo "Use **/remember** to save any important learnings before they are lost."
