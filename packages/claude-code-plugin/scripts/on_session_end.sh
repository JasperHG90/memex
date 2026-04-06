#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Instructs the agent to write a session summary, then cleans up state.
set -euo pipefail
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo '{}'
    exit 0
fi

# --- Read session state ---
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"

SESSION_NOTE_KEY=""
[ -f "$STATE_DIR/session_note_key" ] && SESSION_NOTE_KEY=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)

# --- Gather quantitative stats for context ---
writes=0
[ -f "$STATE_DIR/write_count" ] && writes=$(cat "$STATE_DIR/write_count" 2>/dev/null || echo 0)

commits=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    commits=$(git log --oneline --since="4 hours ago" --author="$(git config user.name 2>/dev/null || echo '')" 2>/dev/null | wc -l | tr -d ' ') || commits=0
fi

spirals=0
if [ -d "$STATE_DIR/file_edits" ]; then
    for f in "$STATE_DIR/file_edits"/*; do
        [ -f "$f" ] || continue
        cnt=$(head -1 "$f" | cut -d' ' -f1 2>/dev/null || echo 0)
        [ "$cnt" -ge 3 ] 2>/dev/null && spirals=$((spirals + 1))
    done
fi

stats="writes: ${writes}, commits: ${commits}, edit spirals: ${spirals}"

# --- Build nudge ---
nudge="Session is ending. Stats: ${stats}."

if [ -n "$SESSION_NOTE_KEY" ]; then
    nudge="${nudge}\n\nUpdate the session note via \`memex_add_note(note_key=\"${SESSION_NOTE_KEY}\", background=true)\` with a 1-2 sentence summary of what was accomplished this session and any key decisions made. Keep it concise — this will be shown in the next session's briefing for continuity."
fi

# --- Clean up per-session state ---
rm -f "$STATE_DIR/write_count"
rm -rf "$STATE_DIR/file_edits"
rm -f "$STATE_DIR/session_note_key"
rm -f "$STATE_DIR/project_vault"

# --- Output ---
jq -n --arg ctx "$nudge" '{
    hookSpecificOutput: {
        hookEventName: "SessionEnd",
        additionalContext: $ctx
    }
}'
