#!/usr/bin/env bash
# Memex Claude Code Plugin — PreCompact.
#
# Captures transcript-since-last-compact to the session note BEFORE compaction
# discards messages. The first capture creates the note; subsequent captures
# append deltas. The session note's note_key matches the SessionEnd hook so
# all events land in one note.
set -uo pipefail
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_payload=""
if [ ! -t 0 ]; then
    _payload=$(cat)
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "{}"
    exit 0
fi

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR" 2>/dev/null || true

_session_note_key=""
[ -f "$STATE_DIR/session_note_key" ] && _session_note_key=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)

if [ -z "$_session_note_key" ]; then
    jq -n '{
        systemMessage: "Memex pre-compact capture skipped — session note key is missing. The plugin will recover on the next session."
    }'
    exit 0
fi

# Lightweight session stats (kept from prior version)
writes=0
COUNTER_FILE="${STATE_DIR}/write_count"
[ -f "$COUNTER_FILE" ] && writes=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)

commits=0
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    commits=$(git log --oneline --since="4 hours ago" 2>/dev/null | wc -l | tr -d ' ') || commits=0
fi

source "$SCRIPT_DIR/resolve_config.sh"

# Get transcript_path and CC session id
_transcript_path=""
_cc_session_id=""
if [ -n "$_payload" ]; then
    _transcript_path=$(printf '%s' "$_payload" | jq -r '.transcript_path // empty' 2>/dev/null || true)
    _cc_session_id=$(printf '%s' "$_payload" | jq -r '.session_id // empty' 2>/dev/null || true)
fi
[ -z "$_cc_session_id" ] && _cc_session_id="default"
_safe_session_id=$(printf '%s' "$_cc_session_id" | tr -c 'A-Za-z0-9._-' '_')

_offset_file="${STATE_DIR}/session_note_offset_${_safe_session_id}"
_prev_offset=0
[ -f "$_offset_file" ] && _prev_offset=$(cat "$_offset_file" 2>/dev/null || echo 0)
case "$_prev_offset" in
    ''|*[!0-9]*) _prev_offset=0 ;;
esac

_capture_status="skipped"
_capture_reason=""

# Opt-out: MEMEX_CC_TRANSCRIPT_CAPTURE=off|0|false|no|disabled bypasses the capture
# but still emits the existing skipped-path output shape (systemMessage + stats
# appendix). Offset file is NOT advanced — re-enabling resumes from prior offset.
case "${MEMEX_CC_TRANSCRIPT_CAPTURE:-on}" in
    off|0|false|no|disabled)
        _capture_reason="disabled via MEMEX_CC_TRANSCRIPT_CAPTURE"
        ;;
esac

if [ -n "$_capture_reason" ]; then
    :  # toggle handled above; fall through to the output assembly below
elif [ -z "$_transcript_path" ] || [ ! -f "$_transcript_path" ]; then
    _capture_reason="transcript_path missing or not a file"
elif [ ! -r "$_transcript_path" ]; then
    _capture_reason="transcript_path is not readable (permissions?)"
else
    _total_lines=$(wc -l < "$_transcript_path" 2>/dev/null | tr -d ' ' || echo 0)
    case "$_total_lines" in
        ''|*[!0-9]*) _total_lines=0 ;;
    esac

    # Detect transcript shrinkage (rotation, truncation): if the file shrunk
    # below the recorded offset, reset offset and re-capture from scratch
    # rather than silently dropping content.
    if [ "$_total_lines" -lt "$_prev_offset" ]; then
        _prev_offset=0
    fi

    if [ "$_total_lines" -le "$_prev_offset" ]; then
        _capture_reason="no new turns since last compaction"
    else
        _new_lines_md=$(tail -n +"$((_prev_offset + 1))" "$_transcript_path" 2>/dev/null \
            | jq -nRr -f "$SCRIPT_DIR/_transcript_to_md.jq" 2>/dev/null || true)

        if [ -z "$_new_lines_md" ]; then
            _capture_reason="no extractable text in new turns"
        else
            _vault=$(memex_resolve_active_vault)

            _timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
            _delta=$(printf '## Pre-compaction snapshot — %s\n\n%s' "$_timestamp" "$_new_lines_md")

            _project_id=""
            [ -f "$STATE_DIR/project_id" ] && _project_id=$(cat "$STATE_DIR/project_id" 2>/dev/null || true)

            # Tag set for the (possibly-first) note creation.
            _tags=(
                "surface:claude-code"
                "auto-capture"
                "session-transcript"
                "$_session_note_key"
            )
            [ -n "$_project_id" ] && _tags+=("project:$_project_id")

            # Friendly title from Claude Code's own session title (shown in the
            # `--resume` picker): user-set `custom-title` wins over the
            # generated `ai-title`; take the latest of each. Raw mode
            # (`fromjson?`) so one malformed transcript line can't abort the
            # scan. Title is set only on note CREATION (append never retitles).
            # `// empty` drops null/absent title values so they don't become a
            # literal "null" title (fromjson? guards parse errors, not nulls).
            _cc_title=$(jq -Rrc 'fromjson? | select(.type=="custom-title") | .customTitle // empty' "$_transcript_path" 2>/dev/null | tail -1)
            [ -z "$_cc_title" ] && _cc_title=$(jq -Rrc 'fromjson? | select(.type=="ai-title") | .aiTitle // empty' "$_transcript_path" 2>/dev/null | tail -1)
            _title_ts=$(date -u +'%Y-%m-%d %H:%M UTC' 2>/dev/null || printf '%s' "$_timestamp")
            _title="Session: ${_cc_title:-transcript — ${_title_ts}}"
            _description="Auto-captured CC session transcript (pre-compact + final)."

            if memex_persist_session_delta \
                "$STATE_DIR" \
                "$_cc_session_id" \
                "$_session_note_key" \
                "$_vault" \
                "$_title" \
                "$_description" \
                "$_delta" \
                "${_tags[@]}"; then
                _capture_status="ok"
                printf '%s' "$_total_lines" > "$_offset_file"
            else
                _capture_reason="memex CLI failed (server unreachable?)"
            fi
        fi
    fi
fi

if [ "$_capture_status" = "ok" ]; then
    msg="Memex pre-compact capture ✓ — appended new turns to \`${_session_note_key}\`."
else
    msg="Memex pre-compact capture skipped (${_capture_reason})."
fi

msg="${msg}\n\nSession stats: ${writes} writes, ${commits} commits in the last 4h."

if [ "$writes" -gt 5 ] || [ "$commits" -gt 0 ]; then
    msg="${msg}\n\nIf you discovered a non-obvious decision, root cause, or workflow learning during this session, consider an explicit \`memex_add_note(background=true)\` to capture it before continuing — the auto-capture preserves the transcript, but a curated note is more discoverable."
fi

jq -n --arg ctx "$msg" '{
    systemMessage: $ctx
}'
