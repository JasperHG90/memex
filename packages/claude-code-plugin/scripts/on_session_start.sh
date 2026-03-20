#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Checks that the Memex server is reachable (warns if not).
# 2. Loads vault context, KV facts, recent notes via direct HTTP calls.
# 3. Resolves per-project vault from KV store.
# 4. Injects behavioral instructions via additionalContext.
#
# Dependencies: curl, jq, git (optional)
set -euo pipefail

RESOLVED_URL="${MEMEX_SERVER_URL:-http://127.0.0.1:8000}"
API="${RESOLVED_URL}/api/v1"

# --- Health check: verify Memex server is reachable ---
if ! curl -sf --max-time 3 "${API}/health" >/dev/null 2>&1; then
    cat <<EOF
{"systemMessage": "⚠️ Memex server is not reachable at ${RESOLVED_URL}. Start it with:\n  memex server start -d\n\nMemex MCP tools will not work until the server is running."}
EOF
    exit 0
fi

# --- Derive portable project identifier ---
# Git-based: stripped remote URL (e.g. github.com/user/repo)
# Non-git:   path relative to $HOME (e.g. projects/myapp)
project_id=""
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    remote_url=$(git remote get-url origin 2>/dev/null) || true
    if [ -n "$remote_url" ]; then
        project_id=$(echo "$remote_url" | sed 's/\.git$//; s|https://[^@]*@|https://|; s|[a-zA-Z][a-zA-Z0-9+.-]*://||')
    fi
fi
if [ -z "$project_id" ]; then
    # Use path relative to $HOME for a stable, collision-resistant identifier
    case "$PWD" in
        "$HOME"/*) project_id="${PWD#"$HOME"/}" ;;
        *)         project_id="$PWD" ;;
    esac
fi

# --- Fetch vaults, recent notes, and namespace-filtered KV (parallel) ---
tmp_vaults=$(mktemp)
tmp_notes=$(mktemp)
tmp_kv=$(mktemp)
trap 'rm -f "$tmp_vaults" "$tmp_notes" "$tmp_kv"' EXIT

# Build namespace filter: always global + user + app:claude-code, plus project if available
if [ -n "$project_id" ]; then
    encoded_ns=$(jq -rn --arg ns "global,user,app:claude-code,project:${project_id}" '$ns | @uri')
else
    encoded_ns=$(jq -rn --arg ns "global,user,app:claude-code" '$ns | @uri')
fi

curl -sf --max-time 5 "${API}/vaults" > "$tmp_vaults" 2>/dev/null &
curl -sf --max-time 5 "${API}/notes?sort=-created_at&limit=10" > "$tmp_notes" 2>/dev/null &
curl -sf --max-time 5 "${API}/kv?namespaces=${encoded_ns}" > "$tmp_kv" 2>/dev/null &
wait

# --- Resolve per-project vault from KV ---
project_vault=""
project_vault_key="project:${project_id}:vault"
if [ -s "$tmp_kv" ]; then
    project_vault=$(jq -r --arg k "$project_vault_key" \
        '.[] | select(.key == $k) | .value // empty' "$tmp_kv" 2>/dev/null) || true
fi

# --- Resolve the instructions file path (next to this script) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTRUCTIONS_FILE="${SCRIPT_DIR}/instructions.md"

# --- Format vaults table (NDJSON → markdown) ---
vaults_md=""
if [ -s "$tmp_vaults" ]; then
    vaults_md=$(jq -rs '
        if length == 0 then "" else
        "## Memex Vaults\n\n| Name | Notes | Last Modified | Active | Description |\n|------|-------|---------------|--------|-------------|\n"
        + (map(
            "| " + (.name // "") + " | " + (.note_count // 0 | tostring) + " | "
            + ((.last_note_added_at // "—") | .[:10]) + " | "
            + (if .is_active then "yes" else "" end) + " | "
            + (.description // "") + " |"
        ) | join("\n"))
        end
    ' "$tmp_vaults" 2>/dev/null) || true
fi

# --- Format KV facts ---
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

# --- Format recent notes table (NDJSON → markdown) ---
notes_md=""
if [ -s "$tmp_notes" ]; then
    notes_md=$(jq -rs '
        if length == 0 then "" else
        "## Recent Memex Notes\n\n| Title | Description | Vault | Created | Note ID |\n|-------|-------------|-------|---------|----------|\n"
        + (map(
            "| " + (.title // .name // "(untitled)") + " | "
            + ((.description // "") | .[:80]) + " | "
            + ((.vault_id // "") | .[:8]) + " | "
            + ((.created_at // "") | .[:10]) + " | "
            + (.id // "") + " |"
        ) | join("\n"))
        end
    ' "$tmp_notes" 2>/dev/null) || true
fi

# --- Build session context (all data + instructions, injected silently) ---
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

# Append instructions
instructions=""
if [ -f "$INSTRUCTIONS_FILE" ]; then
    instructions=$(cat "$INSTRUCTIONS_FILE")
fi

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
vault_count=$([ -s "$tmp_vaults" ] && jq -rs 'length' "$tmp_vaults" 2>/dev/null || echo "0")
note_count=$([ -s "$tmp_notes" ] && jq -rs 'length' "$tmp_notes" 2>/dev/null || echo "0")
kv_count=$([ -s "$tmp_kv" ] && jq 'length' "$tmp_kv" 2>/dev/null || echo "0")
status="🧠 Memex connected — ${vault_count} vaults, ${note_count} recent notes, ${kv_count} KV facts loaded"
if [ -n "$project_vault" ]; then
    status="${status} (vault: ${project_vault})"
fi

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
