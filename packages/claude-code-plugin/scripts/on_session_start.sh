#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionStart
# 1. Checks that the Memex server is reachable (warns if not).
# 2. Loads vault context, KV facts, recent notes.
# 3. Resolves per-project vault from KV store.
# 4. Injects behavioral instructions via additionalContext.
set -euo pipefail

# Memex CLI invocation — pinned to the GitHub repository
MEMEX_FROM="memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
MEMEX=(uvx --from "$MEMEX_FROM" memex)

# Guard: uvx must be on PATH
command -v uvx >/dev/null 2>&1 || {
    cat <<'EOF'
{"systemMessage": "⚠️ uvx not found on PATH. Install uv first: https://docs.astral.sh/uv/\n\nMemex plugin is inactive for this session."}
EOF
    exit 0
}

# --- Health check: verify Memex server is reachable ---
if ! "${MEMEX[@]}" server status >/dev/null 2>&1; then
    cat <<'EOF'
{"systemMessage": "⚠️ Memex server is not reachable. Start it with:\n  uvx --from \"memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli\" memex server start -d\n\nMemex MCP tools will not work until the server is running."}
EOF
    exit 0
fi

# --- Derive portable project identifier ---
# Prefer git remote origin URL (same across all team members' machines).
# Strip .git suffix and credentials for consistency.
# Fall back to the basename of the working directory.
project_id=""
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    remote_url=$(git remote get-url origin 2>/dev/null) || true
    if [ -n "$remote_url" ]; then
        project_id=$(echo "$remote_url" | sed 's/\.git$//; s|https://[^@]*@|https://|')
    fi
fi
if [ -z "$project_id" ]; then
    project_id=$(basename "$PWD")
fi

# --- Resolve per-project vault from KV ---
project_vault=""
kv_key="claude-code:vault:${project_id}"
project_vault=$("${MEMEX[@]}" kv get "$kv_key" --value-only 2>/dev/null) || true

vault_context=""
if [ -n "$project_vault" ]; then
    vault_context="**Active vault for this project**: \`${project_vault}\` (from KV key \`${kv_key}\`). Pass \`vault_id: \"${project_vault}\"\` on all write operations (\`memex_add_note\`, \`memex_kv_write\`)."
fi

# --- Collect vault + memory context ---
parts=()

vault_output=$("${MEMEX[@]}" vault list --compact 2>/dev/null) || true
if [ -n "$vault_output" ]; then
    parts+=("## Memex Vaults" "" "$vault_output" "")
fi

if [ -n "$vault_context" ]; then
    parts+=("$vault_context" "")
fi

kv_output=$("${MEMEX[@]}" kv list --json 2>/dev/null) || true
if [ -n "$kv_output" ] && [ "$kv_output" != "[]" ]; then
    parts+=("## Memex KV Facts (user preferences & conventions)" "" "$kv_output" "")
fi

recent_output=$("${MEMEX[@]}" note recent --limit 10 --compact 2>/dev/null) || true
if [ -n "$recent_output" ]; then
    parts+=("## Recent Memex Notes" "" "$recent_output")
fi

# Build the user-visible system message from collected parts
system_message=""
for part in "${parts[@]}"; do
    if [ -n "$system_message" ]; then
        system_message="${system_message}
${part}"
    else
        system_message="$part"
    fi
done

# --- Behavioral instructions (injected as additionalContext) ---
vault_instruction=""
if [ -n "$project_vault" ]; then
    vault_instruction="
### Per-project vault

This project uses vault \`${project_vault}\` (project: \`${project_id}\`). Pass \`vault_id: \"${project_vault}\"\` on all Memex write calls (\`memex_add_note\`, \`memex_kv_write\`). Read calls default to search vaults and generally do not need a vault_id override."
else
    vault_instruction="
### Per-project vault

No project-specific vault is configured (project: \`${project_id}\`). Notes will be written to the default vault. To bind this project to a specific vault, call \`memex_kv_write(key=\"${kv_key}\", value=\"<vault_name>\")\`. This will take effect on the next session."
fi

read -r -d '' additional_context << 'INSTRUCTIONS' || true
## Memex memory integration

Access Memex (long-term memory) via MCP tools. Build persistent knowledge across sessions.

### Capture — MANDATORY

Call `memex_add_note` (with `background: true`, `author: "claude-code"`) when any of these apply:

1. Completed a multi-step task (save what was done, decisions, outcome)
2. Diagnosed a bug root cause (save symptom, cause, fix)
3. Made/discovered an architectural decision (save decision, rationale)
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration/environment issue

**Keep notes concise** (hard maximum: 300 tokens). Capture the key insight, not a detailed report. No per-file changelogs.

### Retrieval

Session start context is automatic via hook. Do NOT redundantly search at session start.

Route by query type:

IF you know (or roughly know) a note title:
- `memex_find_note(query="title fragment")` → note IDs, titles, similarity scores
- Then read via `memex_get_page_indices` → `memex_get_nodes` as needed

IF query asks about relationships, connections, "how X relates to Y", or landscape:
- `memex_list_entities(query="X")` → entity IDs, types, mention counts
- `memex_get_entity_cooccurrences(entity_id)` → related entities with names, types, counts
- `memex_get_entity_mentions(entity_id)` → source facts linking back to notes
- Then read source notes via Search/Read below as needed

IF query asks about specific content or document lookup:
- **Search**: `memex_memory_search` (broad) and/or `memex_note_search` (targeted). Run in parallel.
- **Filter**: after `memex_memory_search`, call `memex_get_notes_metadata` with Note IDs. After `memex_note_search`, metadata is inline — skip.
- **Read**: `memex_get_page_indices` → `memex_get_nodes` (batch). `memex_read_note` only when total_tokens < 500.
- **Assets**: IF `has_assets: true` in page_index/metadata → `memex_list_assets` → `memex_get_resources` for each. Use images as visual input. Reproduce diagrams as Mermaid/ASCII in response. NEVER skip this step.

IF query is broad: run entity exploration AND search in parallel.

IF storing/retrieving structured facts, preferences, or conventions:
- `memex_kv_write(value, key, vault_id)` — store a user fact or preference
- `memex_kv_get(key)` — exact key lookup
- `memex_kv_search(query)` — fuzzy semantic search over stored facts
- `memex_kv_list()` — list all stored facts
- When the user states a preference, convention, or static fact, proactively store it via `memex_kv_write`.
- Deletion is user-only (CLI `memex kv delete`). Do NOT attempt to delete KV entries.

All vault parameters on MCP tools are **optional** — they default to resolved config values.

PROHIBITED:
- `memex_recent_notes` for discovery.
- Fabricating Note/Node/Unit IDs. Only use IDs from tool output.
- `memex_get_notes_metadata` after `memex_note_search` (metadata already inline).
- `memex_read_note` on notes over 500 tokens. Use `memex_get_page_indices` + `memex_get_nodes`.
- Creating diagrams without first checking assets via `memex_list_assets` → `memex_get_resources`.
- Presenting Memex information without citations.

### Citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim from Memex.
2. Reference list at end of response. Each entry uses a type prefix:
   - `[note]` — title + note ID
   - `[memory]` — title + memory ID + source note ID
   - `[asset]` — filename + note ID

### Slash commands

- `/remember [text]` — save to memory
- `/recall [query]` — search memories
INSTRUCTIONS

# Append the vault instruction to additional_context
additional_context="${additional_context}${vault_instruction}"

# --- Output JSON with both systemMessage and additionalContext ---
python3 -c "
import json, sys

system_message = sys.stdin.read()
additional_context = open('/dev/fd/3').read()

output = {}
if system_message.strip():
    output['systemMessage'] = system_message.strip()
output['hookSpecificOutput'] = {
    'additionalContext': additional_context.strip()
}
print(json.dumps(output))
" 3<<<"$additional_context" <<< "$system_message"
