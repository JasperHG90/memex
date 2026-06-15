#!/usr/bin/env bash
# Memex Claude Code Plugin — PostToolUse on mcp__memex__memex_add_note.
#
# Records that a successful capture happened during this session. The counter
# file is read by other hooks (e.g. future safety-net heuristics).
#
# Line-count is concurrency-safe via O_APPEND: each capture appends one line
# ≤ PIPE_BUF, so `wc -l` on the counter file is exact under parallel hook
# invocations. The per-line timestamps are best-effort — `date` and `printf`
# happen in separate syscalls, so under heavy concurrency the recorded
# epochs may not reflect the actual append order. Only the line count is
# load-bearing; timestamps are advisory and present for future debugging.
#
# Outputs an empty JSON object — this hook only has side-effects.
set -uo pipefail
trap 'echo "{}"; exit 0' ERR

_payload=""
if [ ! -t 0 ]; then
    _payload=$(cat)
fi
if [ -z "$_payload" ]; then
    echo "{}"
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "{}"
    exit 0
fi

# Only act on memex_add_note (matcher should already gate this; defend
# against config drift). Suffix glob, not exact: matches both the plugin-
# namespaced runtime tool (`mcp__plugin_memex_memex__memex_add_note`) and a
# standalone MCP server's `mcp__memex__memex_add_note`.
_tool_name=$(printf '%s' "$_payload" | jq -r '.tool_name // empty' 2>/dev/null || true)
case "$_tool_name" in
    *memex__memex_add_note) ;;
    *)
        echo "{}"
        exit 0
        ;;
esac

# Use the CC session_id from the payload as the counter scope, falling back
# to a generic counter when missing (defensive — payload should always have it).
_session_id=$(printf '%s' "$_payload" | jq -r '.session_id // empty' 2>/dev/null || true)
[ -z "$_session_id" ] && _session_id="default"

# Sanitize: session_id should already be alphanumeric/dashes from CC, but
# strip anything else defensively to keep the filename safe.
_safe_session_id=$(printf '%s' "$_session_id" | tr -c 'A-Za-z0-9._-' '_')

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR" 2>/dev/null || true

_counter_file="${STATE_DIR}/capture_count_${_safe_session_id}"

# Append a single marker line. O_APPEND is atomic on POSIX local filesystems
# for writes ≤ PIPE_BUF, so concurrent hook invocations cannot corrupt the
# counter. Marker content is the timestamp; only the line count matters.
printf '%s\n' "$(date -u +%s)" >> "$_counter_file"

echo "{}"
