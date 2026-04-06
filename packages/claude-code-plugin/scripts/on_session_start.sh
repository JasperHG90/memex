#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Auto-installs/updates rules file.
# 2. Fetches token-budgeted session briefing via single CLI call.
# 3. Resolves per-project vault from KV store.
#
# Dependencies: uvx (uv), jq, git (optional)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

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
    _rules_src="$PLUGIN_ROOT/rules/memex.md"
    _rules_dst="$_project_root/.claude/rules/memex.md"
    if [ -f "$_rules_src" ]; then
        if [ ! -f "$_rules_dst" ] || ! diff -q "$_rules_src" "$_rules_dst" >/dev/null 2>&1; then
            mkdir -p "$(dirname "$_rules_dst")" 2>/dev/null || true
            cp "$_rules_src" "$_rules_dst" 2>/dev/null || true
        fi
    fi
fi

# --- Clear stale session state ---
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR/write_count"
rm -rf "$STATE_DIR/file_edits"

# --- Generate session note key ---
SESSION_NOTE_KEY="session:$(date -u +%Y-%m-%dT%H:%M:%S.%3N 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)"
echo "$SESSION_NOTE_KEY" > "$STATE_DIR/session_note_key"

# --- Derive portable project identifier ---
project_id=""
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    remote_url=$(git remote get-url origin 2>/dev/null) || true
    if [ -n "$remote_url" ]; then
        project_id=$(echo "$remote_url" | sed 's/\.git$//; s|https://[^@]*@|https://|; s|[a-zA-Z][a-zA-Z0-9+.-]*://||')
    fi
fi
if [ -z "$project_id" ]; then
    case "$PWD" in
        "$HOME"/*) project_id="${PWD#"$HOME"/}" ;;
        *)         project_id="$PWD" ;;
    esac
fi

# --- Resolve project vault from KV store ---
project_vault=""
if [ -n "$project_id" ]; then
    project_vault=$(memex kv get "project:${project_id}:vault" --value-only 2>/dev/null) || true
fi

# Persist project vault for other hooks (session end, pre-compact)
[ -n "$project_vault" ] && echo "$project_vault" > "$STATE_DIR/project_vault"

# --- Build briefing CLI args ---
briefing_args=(session --budget 2000)
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

if [ -n "$project_vault" ]; then
    vault_instruction="
### Per-project vault

This project uses vault \`${project_vault}\` (project: \`${project_id}\`). Pass \`vault_id: \"${project_vault}\"\` on all Memex write calls (\`memex_add_note\`). Read calls default to search vaults and generally do not need a vault_id override."
else
    vault_instruction="
### Per-project vault

No project-specific vault is configured (project: \`${project_id}\`). Notes will be written to the default vault. To bind this project to a specific vault, call \`memex_kv_write(key=\"project:${project_id}:vault\", value=\"<vault_name>\")\`. This will take effect on the next session."
fi

session_note_instruction="
### Session note

This session's note key is \`${SESSION_NOTE_KEY}\`. When you complete a meaningful unit of work (bug fix, feature, architectural decision), update the session note via \`memex_add_note(note_key=\"${SESSION_NOTE_KEY}\", background=true)\` with a concise summary of what was done and why. This note persists across sessions for continuity."

additional_context="${briefing_content}${vault_instruction}${session_note_instruction}"

# --- Build status summary ---
status="Memex connected"
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
