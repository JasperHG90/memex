#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Auto-installs/updates rules file.
# 2. Fetches token-budgeted session briefing via single CLI call.
# 3. Resolves active vault from the hierarchical resolver (project → user → agent → env → default).
#
# Dependencies: uvx (uv), jq, git (optional)
set -uo pipefail
# Match every other hook: any unguarded failure emits `{}` so Claude Code
# always sees valid hookSpecificOutput JSON for SessionStart (rather than
# `set -e` quietly killing the script mid-write with no payload). The
# EXIT trap registered later for `tmp_briefing` cleanup composes cleanly
# with this one because bash invokes both.
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# SessionStart's only output is JSON; let the resolver surface uvx/version
# errors as systemMessage so the user sees them.
MEMEX_RESOLVE_VERBOSE=1 source "$SCRIPT_DIR/resolve_config.sh"

# Read the SessionStart payload from stdin. Empty stdin is OK (defensive
# for invocations outside Claude Code, e.g. unit tests).
_payload=""
if [ ! -t 0 ]; then
    _payload=$(cat)
fi

# --- Dependency check: jq ---
if ! command -v jq >/dev/null 2>&1; then
    cat <<'NOJQ'
{"systemMessage": "jq is not installed. Memex hooks require jq for reliable JSON handling.\n\nInstall it: apt-get install jq (Debian/Ubuntu), brew install jq (macOS), or see https://jqlang.github.io/jq/download/\n\nMemex MCP tools still work, but hook context injection is degraded."}
NOJQ
    exit 0
fi

# --- Auto-install/update rules file ---
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    _project_root=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
else
    _project_root="$PWD"
fi
if [ -n "$_project_root" ]; then
    _rules_src_dir="$PLUGIN_ROOT/rules"
    _rules_dst_dir="$_project_root/.claude/rules"
    if [ -d "$_rules_src_dir" ]; then
        mkdir -p "$_rules_dst_dir" 2>/dev/null || true
        for _foreign in "$_rules_src_dir"/*; do
            [ -f "$_foreign" ] || continue
            case "$_foreign" in
                *.md) ;;
                *)
                    echo "memex on_session_start: ignoring non-.md rule file: $_foreign (extend the install loop to support new formats)" >&2
                    ;;
            esac
        done
        for _rules_src in "$_rules_src_dir"/*.md; do
            [ -f "$_rules_src" ] || continue
            _rules_name="$(basename "$_rules_src")"
            _rules_dst="$_rules_dst_dir/$_rules_name"
            if [ ! -f "$_rules_dst" ]; then
                cp "$_rules_src" "$_rules_dst" 2>/dev/null || true
            fi
        done
    fi
fi

# --- Clear stale session state ---
# Per-session files written by other hooks (PreCompact, SessionEnd, PreToolUse
# trackers) carry the previous session's id in their suffix. Wipe them at
# SessionStart so they can't leak into the new session — otherwise we keep
# growing files in $STATE_DIR for the lifetime of the plugin install.
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR/write_count"
rm -rf "$STATE_DIR/file_edits"
rm -f "$STATE_DIR"/capture_count_*
rm -f "$STATE_DIR"/session_note_offset_*
rm -f "$STATE_DIR"/session_note_created_*

# --- Generate session note key ---
SESSION_NOTE_KEY="session:$(date -u +%Y-%m-%dT%H:%M:%S.%3N 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)"
echo "$SESSION_NOTE_KEY" > "$STATE_DIR/session_note_key"

# --- Cache identifiers from the SessionStart payload for later hooks ---
# `model` is only present on the SessionStart payload — other events don't
# carry it — so PreToolUse hooks need it cached on disk.
if [ -n "$_payload" ]; then
    _model=$(printf '%s' "$_payload" | jq -r '.model // empty' 2>/dev/null || true)
    [ -n "$_model" ] && echo "$_model" > "$STATE_DIR/model"

    _cc_session_id=$(printf '%s' "$_payload" | jq -r '.session_id // empty' 2>/dev/null || true)
    [ -n "$_cc_session_id" ] && echo "$_cc_session_id" > "$STATE_DIR/cc_session_id"
fi

# --- Resolve project + active vault ---
project_id=$(memex_resolve_project_id)
project_vault=$(memex_resolve_active_vault)

# Persist resolved vault so other hooks in the same session can read it
# without paying KV round-trip cost on every invocation.
echo "$project_vault" > "$STATE_DIR/active_vault"
echo "$project_id" > "$STATE_DIR/project_id"

# --- Build briefing CLI args ---
briefing_args=(briefing --budget 2000)
[ -n "$project_vault" ] && briefing_args+=(--vault "$project_vault")
[ -n "$project_id" ] && briefing_args+=(--project-id "$project_id")

# --- Fetch session briefing (single CLI call) ---
tmp_briefing=$(mktemp)
trap 'rm -f "$tmp_briefing"' EXIT

if ! memex "${briefing_args[@]}" > "$tmp_briefing" 2>/dev/null; then
    cat <<'EOF'
{"systemMessage": "Memex server is not reachable. Start it with:\n  memex server start -d\n\nMemex MCP tools will not work until the server is running."}
EOF
    exit 0
fi

# --- Build additionalContext ---
briefing_content=$(cat "$tmp_briefing")
status="🧠 Memex connected"

if [ -n "$project_vault" ]; then
    vault_instruction="
### Per-project vault

This project uses vault \`${project_vault}\` (project: \`${project_id}\`). Pass \`vault_id: \"${project_vault}\"\` on Memex write calls (\`memex_add_note\`, \`memex_append_note\`). Read calls default to search vaults and generally do not need a vault_id override."
else
    vault_instruction="
### Per-project vault

No project-specific vault is configured (project: \`${project_id}\`). Notes will be written to the default vault. To bind this project to a specific vault, call \`memex_kv_write(key=\"app:claude-code:project:${project_id}:vault\", value=\"<vault_name>\")\`. This will take effect on the next session."

    status="${status} · No vault set — tell me which vault to use for this project"
fi

session_note_instruction="
### Session note

This session's note key is \`${SESSION_NOTE_KEY}\`. The plugin auto-captures the full session transcript to this note on exit, and appends pre-compaction context here. When you complete a meaningful unit of work, you can also update it explicitly via \`memex_add_note(note_key=\"${SESSION_NOTE_KEY}\", background=true)\` with a concise summary of what was done and why."

auto_tag_instruction="
### Auto-injected metadata

Every \`memex_add_note\` call from this session is auto-tagged with: \`surface:claude-code\`, \`session:${SESSION_NOTE_KEY#session:}\`, \`project:${project_id}\`, plus git context (\`git:branch=...\`, \`git:sha=...\`, \`git:repo=...\`, \`git:dirty\` when applicable) and \`claude:model=...\`. \`background\` defaults to \`true\` unless you explicitly pass \`false\`. Pre-existing tags you supply are preserved."

additional_context="${briefing_content}${vault_instruction}${session_note_instruction}${auto_tag_instruction}"

[ -n "$project_vault" ] && status="${status} (vault: ${project_vault})"

# --- Output JSON ---
jq -n \
    --arg sm "$status" \
    --arg ac "$additional_context" \
    '{
        systemMessage: $sm,
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ac
        }
    }'
