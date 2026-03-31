#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Fetches vault context, KV facts, recent notes via the memex CLI.
# 2. Resolves per-project vault from KV store.
# 3. Injects behavioral instructions via additionalContext.
#
# Dependencies: uvx (uv), jq, git (optional)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

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

# --- Fetch vaults, recent notes, KV facts, and project vault (parallel) ---
tmp_vaults=$(mktemp)
tmp_notes=$(mktemp)
tmp_kv=$(mktemp)
tmp_project_vault=$(mktemp)
trap 'rm -f "$tmp_vaults" "$tmp_notes" "$tmp_kv" "$tmp_project_vault"' EXIT

# Build namespace filter args for KV list
kv_ns_args=(-n global -n user -n "app:claude-code")
[ -n "$project_id" ] && kv_ns_args+=(-n "project:${project_id}")

memex vault list --compact > "$tmp_vaults" 2>/dev/null &
memex note recent --compact > "$tmp_notes" 2>/dev/null &
memex kv list --json "${kv_ns_args[@]}" > "$tmp_kv" 2>/dev/null &
if [ -n "$project_id" ]; then
    memex kv get "project:${project_id}:vault" --value-only > "$tmp_project_vault" 2>/dev/null &
fi
wait

# --- Implicit health check: vaults always has >= 1 entry ---
if [ ! -s "$tmp_vaults" ]; then
    cat <<'EOF'
{"systemMessage": "⚠️ Memex server is not reachable. Start it with:\n  memex server start -d\n\nMemex MCP tools will not work until the server is running."}
EOF
    exit 0
fi

# --- Read project vault ---
project_vault=""
[ -s "$tmp_project_vault" ] && project_vault=$(cat "$tmp_project_vault")

# --- Build session context ---
vaults_md="## Memex Vaults

$(cat "$tmp_vaults")"

kv_md=""
if [ -s "$tmp_kv" ]; then
    kv_count=$(jq 'length' "$tmp_kv" 2>/dev/null) || kv_count=0
    if [ "$kv_count" -gt 0 ] 2>/dev/null; then
        kv_body=$(jq -r '.' "$tmp_kv" 2>/dev/null) || true
        if [ -n "$kv_body" ] && [ "$kv_body" != "[]" ]; then
            kv_md="## Memex KV Facts (user preferences & conventions)

${kv_body}"
        fi
    fi
fi

notes_md=""
[ -s "$tmp_notes" ] && notes_md="## Recent Memex Notes

$(cat "$tmp_notes")"

# --- Assemble context parts ---
session_context=""
for part in "$vaults_md" "$kv_md" "$notes_md"; do
    if [ -n "$part" ]; then
        if [ -n "$session_context" ]; then
            session_context="${session_context}

${part}"
        else
            session_context="$part"
        fi
    fi
done

# --- Append instructions ---
INSTRUCTIONS_FILE="${SCRIPT_DIR}/instructions.md"
instructions=""
[ -f "$INSTRUCTIONS_FILE" ] && instructions=$(cat "$INSTRUCTIONS_FILE")

if [ -n "$project_vault" ]; then
    vault_instruction="
### Per-project vault

This project uses vault \`${project_vault}\` (project: \`${project_id}\`). Pass \`vault_id: \"${project_vault}\"\` on all Memex write calls (\`memex_add_note\`). Read calls default to search vaults and generally do not need a vault_id override."
else
    vault_instruction="
### Per-project vault

No project-specific vault is configured (project: \`${project_id}\`). Notes will be written to the default vault. To bind this project to a specific vault, call \`memex_kv_write(key=\"project:${project_id}:vault\", value=\"<vault_name>\")\`. This will take effect on the next session."
fi

additional_context="${session_context}

${instructions}${vault_instruction}"

# --- Build compact status summary ---
# Count data rows in markdown table (skip header + separator lines starting with |---)
vault_count=$(grep -c '^|[^-]' "$tmp_vaults" 2>/dev/null || echo "0")
vault_count=$((vault_count > 1 ? vault_count - 1 : 0))  # subtract column header row
note_count=$(grep -c '^- ' "$tmp_notes" 2>/dev/null || echo "0")
kv_count=$([ -s "$tmp_kv" ] && jq 'length' "$tmp_kv" 2>/dev/null || echo "0")
status="🧠 Memex connected — ${vault_count} vaults, ${note_count} recent notes, ${kv_count} KV facts loaded"
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
