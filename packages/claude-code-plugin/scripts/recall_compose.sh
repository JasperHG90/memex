#!/usr/bin/env bash
# Memex Claude Code Plugin — UserPromptExpansion hook.
#
# When `/recall` is invoked WITHOUT arguments, compose a search query from
# the last N turns of this conversation and inject it via additionalContext.
# When `/recall` is invoked WITH arguments, do nothing — the explicit query wins.
#
# N is configurable via MEMEX_CC_RECALL_TURNS (default 3, clamped 1..10).
#
# Output is gated to the recall skill; for any other command, returns {}.
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

_command_name=$(printf '%s' "$_payload" | jq -r '.command_name // empty' 2>/dev/null || true)
case "$_command_name" in
    recall) ;;
    *)
        echo "{}"
        exit 0
        ;;
esac

# Skip if the user passed an explicit query — the SKILL.md uses $ARGUMENTS directly.
_command_args=$(printf '%s' "$_payload" | jq -r '.command_args // empty' 2>/dev/null || true)
# Trim whitespace
_command_args_trimmed=$(printf '%s' "$_command_args" | tr -d '[:space:]')
if [ -n "$_command_args_trimmed" ]; then
    echo "{}"
    exit 0
fi

_transcript_path=$(printf '%s' "$_payload" | jq -r '.transcript_path // empty' 2>/dev/null || true)
if [ -z "$_transcript_path" ] || [ ! -f "$_transcript_path" ]; then
    echo "{}"
    exit 0
fi

# Resolve N (turns to compose from)
_n="${MEMEX_CC_RECALL_TURNS:-3}"
case "$_n" in
    ''|*[!0-9]*) _n=3 ;;
esac
[ "$_n" -lt 1 ] 2>/dev/null && _n=1
[ "$_n" -gt 10 ] 2>/dev/null && _n=10

# Build a compact query from the last N user-or-assistant turns with text.
# - Skip lines we can't parse as JSON.
# - Skip lines where role is anything other than user or assistant.
# - For string content: take as-is.
# - For array content: concatenate text-blocks only (skip tool_use / tool_result).
# - Truncate each turn's text to 200 chars to keep the composed query bounded.
# - Cap final query at 800 chars (matches Hindsight's recallMaxQueryChars default).
#
# The jq pipeline below tolerates multiple transcript shapes:
#   {role: "user", content: "..."}
#   {role: "assistant", content: [{type: "text", text: "..."}, {type: "tool_use", ...}]}
#   {type: "user"|"assistant", message: {role, content}}  (some CC versions)
_composed=$(jq -nRr --argjson n "$_n" '
    # Normalize each JSONL line to {role, text}; tolerate malformed JSON
    # (fromjson? returns empty, dropping the line).
    def extract:
        fromjson?
        | (.role // .message.role) as $r
        | (.content // .message.content) as $c
        | if ($r == "user" or $r == "assistant") then
            ( if $c == null then ""
              elif ($c | type) == "string" then $c
              elif ($c | type) == "array" then
                  [ $c[] | select(.type == "text") | .text ] | join(" ")
              else "" end ) as $text
            | { role: $r, text: ($text // "" | gsub("\\s+"; " ") | .[0:200]) }
          else empty end;

    [ inputs | extract | select(.text != "") ]
    | .[-$n:]
    | map("[\(.role)] \(.text)")
    | join("\n")
    | .[0:800]
' < "$_transcript_path" 2>/dev/null || true)

if [ -z "$_composed" ]; then
    echo "{}"
    exit 0
fi

# Compose the human-visible announcement + the actual query the skill should use.
# The hook emits BOTH so the user sees what's being matched (per the plan's
# "explicit, not magical" requirement) and the skill has unambiguous text to search.
_announce="No \`/recall\` query supplied — composing from the last ${_n} turns of this conversation."
_context=$(printf '%s\n\n%s\n\n--- Composed query (last %d turns) ---\n%s\n--- End composed query ---\n\nUse the composed query above as your recall query, routed per the /recall skill: a how-to query ("how do I X", "what is the checklist for Y") → `memex_procedural_search` ONLY; otherwise → `memex_memory_search` and `memex_note_search` (scoped to the project vault via `vault_ids` when one is set). Print a one-line summary of the query before searching so the user can see it.' \
    "$_announce" \
    "" \
    "$_n" \
    "$_composed")

jq -n --arg ctx "$_context" '{
    hookSpecificOutput: {
        hookEventName: "UserPromptExpansion",
        additionalContext: $ctx
    }
}'
