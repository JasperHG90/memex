#!/usr/bin/env bash
# Memex Claude Code Plugin — PreToolUse on mcp__memex__memex_add_note.
#
# Augments every memex_add_note call with ambient capture metadata:
#   - surface:claude-code           (always)
#   - session:<session_note_key>    (groups all notes from this CC session)
#   - project:<project_id>          (cross-vault discoverability)
#   - git:branch=<branch>           (when git is resolvable)
#   - git:sha=<short-sha>           (when a commit exists)
#   - git:repo=<owner/name>         (when origin remote is set)
#   - git:dirty                     (presence-only, when working tree is dirty)
#   - claude:model=<model>          (cached from SessionStart)
#   - cc:plugin=<plugin-version>    (provenance during plugin upgrades)
#
# Defaults `background` to true when the agent didn't pass it explicitly.
# Defaults `vault_id` to the resolved active vault when the agent didn't pass
# it explicitly. Both are *defaults* — explicit caller values are preserved.
#
# Pre-existing tags supplied by the agent are preserved and merged.
#
# Outputs `hookSpecificOutput.updatedInput` JSON to stdout. On any failure,
# falls back to an empty JSON object so the original tool call proceeds.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Catch-all: emit empty JSON on unexpected error so the agent's call proceeds.
trap 'echo "{}"; exit 0' ERR

# Read hook payload from stdin
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

# Only act on memex_add_note. CC matchers are usually exact, but defend
# against config drift.
_tool_name=$(printf '%s' "$_payload" | jq -r '.tool_name // empty' 2>/dev/null || true)
case "$_tool_name" in
    mcp__memex__memex_add_note) ;;
    *)
        echo "{}"
        exit 0
        ;;
esac

STATE_DIR="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/.state}/memex"

# `resolve_config.sh` is only sourced when this hook actually needs to call
# `memex_resolve_project_id` (cache miss on `$STATE_DIR/project_id`).
# Sourcing it triggers a `uvx` availability check that's noisy in cold paths
# and pays off only when the cache misses, which is rare after SessionStart.
_resolver_loaded=0
_load_resolver_if_needed() {
    if [ "$_resolver_loaded" -eq 0 ]; then
        # shellcheck source=resolve_config.sh
        source "$SCRIPT_DIR/resolve_config.sh"
        _resolver_loaded=1
    fi
}

# ---------------------------------------------------------------------------
# Build the auto-tag list (one tag per line, then jq-converts to a JSON array).
# ---------------------------------------------------------------------------
_auto_tags=()
_auto_tags+=("surface:claude-code")

# Session tag — read from disk, fall back to nothing if SessionStart hasn't run.
_session_note_key=""
if [ -f "$STATE_DIR/session_note_key" ]; then
    _session_note_key=$(cat "$STATE_DIR/session_note_key" 2>/dev/null || true)
fi
[ -n "$_session_note_key" ] && _auto_tags+=("$_session_note_key")

# Project tag — prefer the cached value (avoids re-shelling git), fall back
# to live resolution (and only then pay the resolver's source cost).
_project_id=""
if [ -f "$STATE_DIR/project_id" ]; then
    _project_id=$(cat "$STATE_DIR/project_id" 2>/dev/null || true)
fi
if [ -z "$_project_id" ]; then
    _load_resolver_if_needed
    _project_id=$(memex_resolve_project_id)
fi
[ -n "$_project_id" ] && _auto_tags+=("project:$_project_id")

# Git tags — only emit when git context is actually resolvable. Each step
# is independent: a repo without a remote still produces git:branch and
# git:sha but not git:repo.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    _branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [ -n "$_branch" ] && [ "$_branch" != "HEAD" ]; then
        _auto_tags+=("git:branch=$_branch")
    fi

    _sha=$(git rev-parse --short HEAD 2>/dev/null || true)
    [ -n "$_sha" ] && _auto_tags+=("git:sha=$_sha")

    _remote=$(git remote get-url origin 2>/dev/null || true)
    if [ -n "$_remote" ]; then
        # Normalize: strip basic-auth, scheme, trailing .git → owner/name
        _repo=$(printf '%s' "$_remote" | sed 's/\.git$//; s|https://[^@]*@|https://|; s|[a-zA-Z][a-zA-Z0-9+.-]*://||; s|^[^:]*:||')
        # Try to extract owner/name from the tail of the path
        _repo_short=$(printf '%s' "$_repo" | awk -F/ 'NF>=2 {print $(NF-1) "/" $NF}')
        if [ -n "$_repo_short" ]; then
            _auto_tags+=("git:repo=$_repo_short")
        fi
    fi

    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        _auto_tags+=("git:dirty")
    fi
fi

# Model tag — cached from SessionStart payload.
if [ -f "$STATE_DIR/model" ]; then
    _model=$(cat "$STATE_DIR/model" 2>/dev/null || true)
    [ -n "$_model" ] && _auto_tags+=("claude:model=$_model")
fi

# Plugin version tag.
if [ -f "$PLUGIN_ROOT/.claude-plugin/plugin.json" ]; then
    _plugin_version=$(jq -r '.version // empty' "$PLUGIN_ROOT/.claude-plugin/plugin.json" 2>/dev/null || true)
    [ -n "$_plugin_version" ] && _auto_tags+=("cc:plugin=$_plugin_version")
fi

# Convert auto-tags array → JSON array
_auto_tags_json=$(printf '%s\n' "${_auto_tags[@]}" | jq -R . | jq -s .)

# ---------------------------------------------------------------------------
# Compose updatedInput: preserve everything from tool_input, merge tags,
# default background and vault_id when not explicitly set.
# ---------------------------------------------------------------------------
_active_vault=""
if [ -f "$STATE_DIR/active_vault" ]; then
    _active_vault=$(cat "$STATE_DIR/active_vault" 2>/dev/null || true)
fi

# jq logic:
#   - merged_tags = (existing_tags ∪ auto_tags), deduplicated, existing-first order.
#   - background: if absent OR null, default to true; if present (incl. false), preserve.
#     Cannot use jq's // operator — it treats `false` as falsy and would overwrite
#     an explicit `background: false`. Use an explicit null check instead.
#   - vault_id: if absent OR null OR empty string, fill with active_vault when set;
#     otherwise preserve the caller's value.
_updated_input=$(printf '%s' "$_payload" | jq \
    --argjson auto_tags "$_auto_tags_json" \
    --arg active_vault "$_active_vault" \
    '
    .tool_input as $ti
    | ($ti.tags // []) as $existing_tags
    | ($existing_tags + $auto_tags | unique) as $merged_tags
    | (if ($ti.background == null) then true else $ti.background end) as $bg
    | (if (($ti.vault_id // "") == "") and ($active_vault != "")
        then $active_vault
        else $ti.vault_id
       end) as $vault
    | $ti
        | .tags = $merged_tags
        | .background = $bg
        | (if $vault != null and $vault != "" then .vault_id = $vault else . end)
    ')

if [ -z "$_updated_input" ] || [ "$_updated_input" = "null" ]; then
    echo "{}"
    exit 0
fi

jq -n \
    --argjson updated "$_updated_input" \
    '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            updatedInput: $updated
        }
    }'
