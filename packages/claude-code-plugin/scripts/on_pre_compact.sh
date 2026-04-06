#!/usr/bin/env bash
# Memex Claude Code Plugin — PreCompact
# Data-driven nudge: reads session stats (writes, edit spirals, commits)
# to produce an actionable compaction reminder.
set -euo pipefail

# --- Dependency check ---
if ! command -v jq >/dev/null 2>&1; then
    echo '{}'
    exit 0
fi

# --- Read session stats ---
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"

# Total writes
writes=0
COUNTER_FILE="${STATE_DIR}/write_count"
[ -f "$COUNTER_FILE" ] && writes=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)

# Recent commits
commits=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    commits=$(git log --oneline --since="4 hours ago" 2>/dev/null | wc -l | tr -d ' ') || commits=0
fi

# Edit spirals: files edited 3+ times
FILE_EDITS_DIR="${STATE_DIR}/file_edits"
spirals=""
spiral_count=0
most_edited_file=""
most_edited_count=0

if [ -d "$FILE_EDITS_DIR" ]; then
    for edit_file in "$FILE_EDITS_DIR"/*; do
        [ -f "$edit_file" ] || continue
        line=$(head -1 "$edit_file" 2>/dev/null) || continue
        count=$(echo "$line" | cut -d' ' -f1 2>/dev/null) || continue
        filepath=$(echo "$line" | cut -d' ' -f2- 2>/dev/null) || continue
        basename_part=$(basename "$filepath" 2>/dev/null) || continue

        if [ "$count" -ge 3 ] 2>/dev/null; then
            spiral_count=$((spiral_count + 1))
            if [ -n "$spirals" ]; then
                spirals="${spirals}, ${basename_part} ${count}x"
            else
                spirals="${basename_part} ${count}x"
            fi
            if [ "$count" -gt "$most_edited_count" ] 2>/dev/null; then
                most_edited_count=$count
                most_edited_file=$basename_part
            fi
        fi
    done
fi

# --- Build data-driven nudge ---
nudge="Context compaction is imminent — conversation history will be compressed."
nudge="${nudge}\n\nThis session: ${writes} writes, ${spiral_count} edit spirals"
if [ "$spiral_count" -gt 0 ]; then
    nudge="${nudge} (${spirals})"
fi
nudge="${nudge}, ${commits} commits."

if [ "$spiral_count" -gt 0 ] && [ -n "$most_edited_file" ]; then
    nudge="${nudge}\n\nThe \`${most_edited_file}\` struggle suggests a debugging insight worth capturing."
fi

# Session note key
SESSION_NOTE_KEY=""
[ -f "${STATE_DIR}/session_note_key" ] && SESSION_NOTE_KEY=$(cat "${STATE_DIR}/session_note_key" 2>/dev/null || true)

nudge="${nudge}\n\nBefore continuing, review this session for anything worth persisting to long-term memory via \`memex_add_note\` (background: true). Save if: (1) you diagnosed a bug root cause, (2) made or discovered an architectural decision, (3) learned a user preference or workflow pattern, (4) completed a multi-step task with reusable insights. Skip if nothing notable happened."

if [ -n "$SESSION_NOTE_KEY" ]; then
    nudge="${nudge}\n\nUpdate the running session note via \`memex_add_note(note_key='${SESSION_NOTE_KEY}')\` with what you've learned."
fi

nudge="${nudge}\n\nConsider running \`/retro\` to record a structured session postmortem before context is lost."

# --- Output ---
jq -n --arg ctx "$nudge" '{
    hookSpecificOutput: {
        hookEventName: "PreCompact",
        additionalContext: $ctx
    }
}'
