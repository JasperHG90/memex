# Memex Claude Code Plugin — shared transcript-to-markdown filter.
#
# Reads Claude Code transcript JSONL on stdin (one JSON message per line)
# and emits markdown blocks of the form:
#
#   ### <role>
#
#   <text>
#
# Lines that fail to parse, that lack a user/assistant role, or whose
# content has no extractable text are silently dropped. Both `content` as
# a string and `content` as an array of blocks (with `{type: "text"}` items)
# are supported.
#
# Invoke as:  jq -nRr -f _transcript_to_md.jq < transcript.jsonl

def extract:
    fromjson?
    | (.role // .message.role) as $r
    | (.content // .message.content) as $c
    | if ($r == "user" or $r == "assistant") then
        ( if $c == null then ""
          elif ($c | type) == "string" then $c
          elif ($c | type) == "array" then
              [ $c[] | select(.type == "text") | .text ] | join("\n")
          else "" end ) as $text
        | { role: $r, text: ($text // "") }
      else empty end;
inputs | extract | select(.text != "")
| "### \(.role)\n\n\(.text)\n"
