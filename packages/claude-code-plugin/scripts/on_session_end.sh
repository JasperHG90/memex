#!/usr/bin/env bash
# Memex Claude Code Plugin — SessionEnd
# Writes a session marker note with commit subjects (the most informative
# signal available from bash). Agent may have already written a richer
# session note via PreCompact; this is the fallback.
set -euo pipefail
trap 'echo "{}"; exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/resolve_config.sh"

# --- Read session state ---
STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"

PROJECT_VAULT=""
[ -f "$STATE_DIR/project_vault" ] && PROJECT_VAULT=$(cat "$STATE_DIR/project_vault" 2>/dev/null || true)

# --- Gather session activity ---
writes=0
[ -f "$STATE_DIR/write_count" ] && writes=$(cat "$STATE_DIR/write_count" 2>/dev/null || echo 0)

commit_subjects=""
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    commit_subjects=$(git log --format='- %s' --since="4 hours ago" --author="$(git config user.name 2>/dev/null || echo '')" 2>/dev/null | head -10) || true
fi

# --- Build note content ---
content="Session ended. File writes: ${writes}."
if [ -n "$commit_subjects" ]; then
    content="${content}\n\nCommits:\n${commit_subjects}"
fi

# --- Post note via CLI ---
# Do NOT use --key here. The agent may have already written a richer session
# note via PreCompact using the session note key. Using the same key would
# overwrite the agent's note with this dumber fallback.
note_args=(note add "$(echo -e "$content")" --tag "session-marker" --tag "agent-reflection" --background)
[ -n "$PROJECT_VAULT" ] && note_args+=(--vault "$PROJECT_VAULT")

if ! memex "${note_args[@]}" 2>/dev/null; then
    echo "[memex] Warning: Failed to save session marker note." >&2
fi

# --- Clean up per-session state ---
rm -f "$STATE_DIR/write_count"
rm -rf "$STATE_DIR/file_edits"
rm -f "$STATE_DIR/session_note_key"
rm -f "$STATE_DIR/project_vault"

# Output empty JSON (SessionEnd cannot inject agent context)
echo '{}'
