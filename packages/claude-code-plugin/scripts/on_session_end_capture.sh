#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd auto-capture.
#
# Persists the session transcript to long-term memory as a final safety net.
# Mirrors the Hermes plugin's `on_session_end` behavior so notes from either
# host carry the same shape.
#
# Gating: skip on `clear` and `resume` (the session is continuing) and on
# `bypass_permissions_disabled` (abnormal exit, transcript may be partial).
# Capture on `prompt_input_exit`, `logout`, and `other`.
#
# SessionEnd has no decision-control output (per code.claude.com/docs/en/hooks.md),
# so this hook produces side-effects only. Configured `async: true` in
# hooks.json so the agent's exit isn't blocked.
set -uo pipefail
# SessionEnd has no decision-control output and Claude Code discards stdout
# from async hooks, so emitting `{}` is purely cosmetic here — but matching
# the rest of the plugin's `trap 'echo "{}"; exit 0' ERR` pattern removes a
# maintenance-time trap: future refactors that flip this hook to sync
# (or copy the trap to a sync hook) won't suddenly start panicking on a
# malformed response.
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_payload=""
if [ ! -t 0 ]; then
    _payload=$(cat)
fi

if [ -z "$_payload" ]; then
    exit 0
fi

# =============================================================================
# === BEHAVIOUR GUARD — opt-out short-circuit. All side effects that should ==
# === run regardless of MEMEX_CC_TRANSCRIPT_CAPTURE MUST be placed ABOVE this. ==
# =============================================================================
# Falsy values (off|0|false|no|disabled) skip the capture entirely. Offset file
# is NOT advanced — re-enabling resumes from the prior offset (no turns lost).
# Note: the jq availability check below this guard is intentional — when capture
# is disabled, missing jq is irrelevant (we have nothing to serialise), so we
# don't want to fail or warn on jq absence in that path.
case "${MEMEX_CC_TRANSCRIPT_CAPTURE:-on}" in
    off|0|false|no|disabled)
        exit 0
        ;;
esac

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

# --- Gate on `reason` ---
_reason=$(printf '%s' "$_payload" | jq -r '.reason // "other"' 2>/dev/null || echo "other")
case "$_reason" in
    clear|resume|bypass_permissions_disabled)
        exit 0
        ;;
    prompt_input_exit|logout|other) ;;
    *)
        # Unknown reason — capture defensively, better to over-capture than
        # silently lose context.
        ;;
esac

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR" 2>/dev/null || true

_session_note_key=""
[ -f "$STATE_DIR/session_note_key" ] && _session_note_key=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)

if [ -z "$_session_note_key" ]; then
    # SessionStart never ran cleanly. Generate a fallback key so we still
    # get a note out of this session.
    _session_note_key="session:$(date -u +%Y-%m-%dT%H:%M:%S.%3N 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)"
fi

_transcript_path=$(printf '%s' "$_payload" | jq -r '.transcript_path // empty' 2>/dev/null || true)
if [ -z "$_transcript_path" ] || [ ! -f "$_transcript_path" ] || [ ! -r "$_transcript_path" ]; then
    exit 0
fi

_cc_session_id=$(printf '%s' "$_payload" | jq -r '.session_id // empty' 2>/dev/null || true)
[ -z "$_cc_session_id" ] && _cc_session_id="default"
_safe_session_id=$(printf '%s' "$_cc_session_id" | tr -c 'A-Za-z0-9._-' '_')

_offset_file="${STATE_DIR}/session_note_offset_${_safe_session_id}"
_prev_offset=0
[ -f "$_offset_file" ] && _prev_offset=$(cat "$_offset_file" 2>/dev/null || echo 0)
case "$_prev_offset" in
    ''|*[!0-9]*) _prev_offset=0 ;;
esac

_total_lines=$(wc -l < "$_transcript_path" 2>/dev/null | tr -d ' ' || echo 0)
case "$_total_lines" in
    ''|*[!0-9]*) _total_lines=0 ;;
esac

# Detect transcript shrinkage (rotation, truncation): re-capture from start
# rather than silently dropping the rotated content.
if [ "$_total_lines" -lt "$_prev_offset" ]; then
    _prev_offset=0
fi

if [ "$_total_lines" -le "$_prev_offset" ] && [ -f "${STATE_DIR}/session_note_created_${_safe_session_id}" ]; then
    # No new turns AND the note already exists — nothing to do.
    exit 0
fi

# Slice from offset; if the note was never created (no compactions), include
# everything from the start.
_lines_md=$(tail -n +"$((_prev_offset + 1))" "$_transcript_path" 2>/dev/null \
    | jq -nRr -f "$SCRIPT_DIR/_transcript_to_md.jq" 2>/dev/null || true)

if [ -z "$_lines_md" ]; then
    exit 0
fi

source "$SCRIPT_DIR/resolve_config.sh"
_vault=$(memex_resolve_active_vault)

_project_id=""
[ -f "$STATE_DIR/project_id" ] && _project_id=$(cat "$STATE_DIR/project_id" 2>/dev/null || true)

_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
_section_header=$(printf '## Session-end snapshot — %s (reason: %s)' "$_timestamp" "$_reason")
_delta=$(printf '%s\n\n%s' "$_section_header" "$_lines_md")

_tags=(
    "surface:claude-code"
    "auto-capture"
    "session-transcript"
    "$_session_note_key"
    "session-end:$_reason"
)
[ -n "$_project_id" ] && _tags+=("project:$_project_id")

# Friendly title from Claude Code's own session title (the one shown in the
# `--resume` picker): user-set `custom-title` wins over the generated
# `ai-title`; take the latest of each. Raw mode (`fromjson?`) so one malformed
# transcript line can't abort the scan — mirrors _transcript_to_md.jq. The
# title is only set on note CREATION (append never retitles), so an early
# capture that precedes CC's title generation falls back to the date.
# `// empty` drops null/absent title values so they don't become a literal
# "null" title (fromjson? guards parse errors, not nulls).
_cc_title=$(jq -Rrc 'fromjson? | select(.type=="custom-title") | .customTitle // empty' "$_transcript_path" 2>/dev/null | tail -1)
[ -z "$_cc_title" ] && _cc_title=$(jq -Rrc 'fromjson? | select(.type=="ai-title") | .aiTitle // empty' "$_transcript_path" 2>/dev/null | tail -1)
_title_ts=$(date -u +'%Y-%m-%d %H:%M UTC' 2>/dev/null || printf '%s' "$_timestamp")
_title="Session: ${_cc_title:-transcript — ${_title_ts}}"
_description="Auto-captured CC session transcript (final, reason: ${_reason})."

memex_persist_session_delta \
    "$STATE_DIR" \
    "$_cc_session_id" \
    "$_session_note_key" \
    "$_vault" \
    "$_title" \
    "$_description" \
    "$_delta" \
    "${_tags[@]}" \
    && printf '%s' "$_total_lines" > "$_offset_file"

exit 0
