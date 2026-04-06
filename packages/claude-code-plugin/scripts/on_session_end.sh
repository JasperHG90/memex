#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Gathers quantitative session stats (including edit spirals) and posts a
# lightweight marker note. Cleans up per-session state.
set -euo pipefail
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

# --- Gather quantitative stats ---
commits=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # Count commits made in the last 4 hours (approximate session window)
    commits=$(git log --oneline --since="4 hours ago" --author="$(git config user.name 2>/dev/null || echo '')" 2>/dev/null | wc -l | tr -d ' ') || commits=0
fi

# Count file writes from the state counter (set by on_post_write.sh)
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
writes=0
COUNTER_FILE="${STATE_DIR}/write_count"
if [ -f "$COUNTER_FILE" ]; then
    writes=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
fi

# Count edit spirals (files edited 3+ times)
spirals=0
spiral_details=""
if [ -d "$STATE_DIR/file_edits" ]; then
    for f in "$STATE_DIR/file_edits"/*; do
        [ -f "$f" ] || continue
        cnt=$(head -1 "$f" | cut -d' ' -f1 2>/dev/null || echo 0)
        fpath=$(head -1 "$f" | cut -d' ' -f2- 2>/dev/null || echo "unknown")
        if [ "$cnt" -ge 3 ] 2>/dev/null; then
            spirals=$((spirals + 1))
            bname=$(basename "$fpath")
            if [ -n "$spiral_details" ]; then
                spiral_details="${spiral_details}, ${bname} (${cnt}x)"
            else
                spiral_details="${bname} (${cnt}x)"
            fi
        fi
    done
fi

# Build stats summary
stats="commits: ${commits}, file writes: ${writes}"
if [ "$spirals" -gt 0 ]; then
    stats="${stats}, edit spirals: ${spirals} [${spiral_details}]"
fi

# Read session note key
SESSION_NOTE_KEY=""
[ -f "$STATE_DIR/session_note_key" ] && SESSION_NOTE_KEY=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)

# Post marker note via CLI (if memex is available)
note_args=(note add "Session ended. Stats: ${stats}." --tags "session-marker" --tags "agent-reflection")
[ -n "$SESSION_NOTE_KEY" ] && note_args+=(--key "$SESSION_NOTE_KEY")

if ! memex "${note_args[@]}" 2>/dev/null; then
    echo "[memex] Warning: Failed to save session marker note. Memex server may be down." >&2
fi

# --- Clean up per-session state ---
rm -f "$COUNTER_FILE"
rm -rf "$STATE_DIR/file_edits"
rm -f "$STATE_DIR/session_note_key"

# Output empty JSON (SessionEnd hooks do not inject context)
echo '{}'
