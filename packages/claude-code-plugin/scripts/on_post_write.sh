#!/usr/bin/env bash
# Memex Claude Code Plugin — PostToolUse (Write/Edit)
# Tracks per-file edit counts and nudges on edit spirals (3+ edits to same file).
# Also keeps a global write counter and nudges every 10th write.
set -euo pipefail
trap 'echo "{}"; exit 0' ERR

# Read tool input from stdin
input=$(cat)

# --- Dependency check ---
if ! command -v jq >/dev/null 2>&1; then
    echo '{}'
    exit 0
fi

# Extract the file_path from the tool input
file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)

# Skip trivial / generated files
case "$file_path" in
    ''|*node_modules*|*package-lock*|*.lock|*__pycache__*|*.pyc|*dist/*|*.min.js|*.min.css|*.map)
        echo '{}'
        exit 0
        ;;
esac

# --- State directory ---
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR"

# --- Global write counter (existing behavior) ---
COUNTER_FILE="${STATE_DIR}/write_count"

count=0
[ -f "$COUNTER_FILE" ] && count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNTER_FILE"

global_nudge=""
if [ $((count % 10)) -eq 0 ]; then
    global_nudge="10+ files written this session. If you completed a meaningful task (feature, fix, refactor), save a summary via \`memex_add_note\` (background: true, author: 'claude-code')."
fi

# --- Per-file edit tracking (edit spiral detection) ---
FILE_EDITS_DIR="$STATE_DIR/file_edits"
mkdir -p "$FILE_EDITS_DIR"
file_hash=$(echo -n "$file_path" | openssl md5 2>/dev/null | awk '{print substr($NF,1,16)}')
edit_file="$FILE_EDITS_DIR/$file_hash"

file_edit_count=0
if [ -f "$edit_file" ]; then
    file_edit_count=$(head -1 "$edit_file" | cut -d' ' -f1 2>/dev/null || echo 0)
fi
file_edit_count=$((file_edit_count + 1))
printf '%d %s\n' "$file_edit_count" "$file_path" > "$edit_file"

spiral_nudge=""
basename_part=$(basename "$file_path")

# Gentle nudge at exactly 3 edits
if [ "$file_edit_count" -eq 3 ]; then
    spiral_nudge="You've edited \`${basename_part}\` 3 times this session. If you're chasing a bug or iterating on a tricky problem, consider capturing what you've learned via \`memex_add_note\` (background: true)."
fi

# Structured prompt at 5+ edits (every 2nd edit)
if [ "$file_edit_count" -ge 5 ] && [ $((file_edit_count % 2)) -eq 1 ]; then
    spiral_nudge="You've edited \`${basename_part}\` ${file_edit_count} times this session — this suggests a complex problem worth documenting. Consider \`memex_add_note\` to capture: (1) what you were trying to achieve, (2) what approaches didn't work, (3) the solution that worked."
    # Reference session note key if available
    SESSION_NOTE_KEY=""
    [ -f "$STATE_DIR/session_note_key" ] && SESSION_NOTE_KEY=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)
    if [ -n "$SESSION_NOTE_KEY" ]; then
        spiral_nudge="${spiral_nudge} Update the running session note via \`memex_add_note(note_key='${SESSION_NOTE_KEY}')\`."
    fi
fi

# --- Build output ---
message=""
[ -n "$global_nudge" ] && message="$global_nudge"
if [ -n "$spiral_nudge" ]; then
    if [ -n "$message" ]; then
        message="${message}\n\n${spiral_nudge}"
    else
        message="$spiral_nudge"
    fi
fi

if [ -z "$message" ]; then
    echo '{}'
else
    jq -n --arg ctx "$message" '{
        hookSpecificOutput: {
            hookEventName: "PostToolUse",
            additionalContext: $ctx
        }
    }'
fi
