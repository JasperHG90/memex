#!/usr/bin/env bash
# resolve_config.sh — Memex CLI wrapper + project / vault resolution helpers.
#
# Source this from hook scripts. Defines:
#   memex                           — wrapper around the uvx-pinned Memex CLI.
#   memex_resolve_project_id        — derive a portable project identifier.
#   memex_resolve_active_vault      — Hermes-style hierarchical vault lookup.
#   memex_kv_namespace_migrate      — one-time migration of the project vault key.

# When sourced from a hook that emits its own JSON on stdout (e.g., a
# PreToolUse hook returning ``updatedInput``), an early system message here
# would corrupt the hook output. Surface uvx-missing or bad-version errors
# *only* when the caller explicitly opts in via MEMEX_RESOLVE_VERBOSE=1, or
# from the SessionStart hook (where the message is the only output anyway).
_memex_emit_systemMessage() {
    if [ "${MEMEX_RESOLVE_VERBOSE:-0}" = "1" ]; then
        cat
    fi
}

if ! command -v uvx >/dev/null 2>&1; then
    _memex_emit_systemMessage <<'EOF'
{"systemMessage": "❌ `uvx` is not on PATH. Hooks require it to run the Memex CLI.\n\nInstall uv: https://docs.astral.sh/uv/getting-started/installation/"}
EOF
    # Stub out memex so callers can still source us safely; calls just fail.
    memex() { return 1; }
    return 0 2>/dev/null || exit 0
fi

_memex_ref="${MEMEX_PLUGIN_VERSION:-latest}"
_memex_pkg="memex-cli @ git+https://github.com/JasperHG90/memex.git@${_memex_ref}#subdirectory=packages/cli"

if [ "$_memex_ref" != "latest" ]; then
    if ! git ls-remote --tags --heads https://github.com/JasperHG90/memex.git "$_memex_ref" 2>/dev/null | grep -q .; then
        _memex_emit_systemMessage <<EOF
{"systemMessage": "❌ MEMEX_PLUGIN_VERSION='${_memex_ref}' does not exist as a tag or branch on github.com/JasperHG90/memex.\n\nAvailable tags: https://github.com/JasperHG90/memex/tags\n\nUnset the variable to use the default (latest)."}
EOF
        memex() { return 1; }
        return 0 2>/dev/null || exit 0
    fi
fi

# Wrap CLI calls in a hard timeout so a hung server can't block hooks
# indefinitely. SessionEnd / PreCompact are particularly sensitive: the
# Claude Code host may force-kill ``async`` hooks after ~10s, so we cap our
# call latency well below that. Individual call sites can override via
# MEMEX_CC_TIMEOUT (seconds).
memex() {
    local _timeout="${MEMEX_CC_TIMEOUT:-8}"
    # Validate: positive integer in [1..600]. Anything else falls back to 8s
    # so a misconfigured env var cannot turn `timeout` into an invalid-arg
    # error (exit 125), which is indistinguishable from a real CLI failure.
    case "$_timeout" in
        ''|*[!0-9]*) _timeout=8 ;;
    esac
    # Reject obvious overflow before bash's [ -gt ] hits its INT64 ceiling.
    [ "${#_timeout}" -gt 4 ] && _timeout=8
    [ "$_timeout" -lt 1 ]    && _timeout=8
    [ "$_timeout" -gt 600 ]  && _timeout=600

    if command -v timeout >/dev/null 2>&1; then
        timeout "$_timeout" uvx --from "$_memex_pkg" memex "$@"
    else
        uvx --from "$_memex_pkg" memex "$@"
    fi
}

# ---------------------------------------------------------------------------
# Project identifier
#
# Stable across machines: prefer the git remote (normalized: strip basic auth,
# scheme, and `.git`), fall back to a path relative to $HOME, then $PWD.
# ---------------------------------------------------------------------------
memex_resolve_project_id() {
    local _id=""
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local _remote
        _remote=$(git remote get-url origin 2>/dev/null) || true
        if [ -n "$_remote" ]; then
            _id=$(printf '%s' "$_remote" | sed 's/\.git$//; s|https://[^@]*@|https://|; s|[a-zA-Z][a-zA-Z0-9+.-]*://||')
        fi
    fi
    if [ -z "$_id" ]; then
        local _home="${HOME:-}"
        case "$PWD" in
            "${_home}"/*) _id="${PWD#"$_home"/}" ;;
            *)            _id="$PWD" ;;
        esac
    fi
    printf '%s' "$_id"
}

# ---------------------------------------------------------------------------
# KV helpers
# ---------------------------------------------------------------------------
_memex_kv_get_value() {
    # $1: key. Echoes the value (or empty string) on stdout, never errors.
    memex kv get "$1" --value-only 2>/dev/null || true
}

_memex_kv_write_value() {
    # $1: value, $2: key. Returns 0 on success, non-zero on failure.
    memex kv write "$1" --key "$2" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# KV namespace migration
#
# CC plugin historically wrote `project:<id>:vault`. The plugin now namespaces
# under `app:claude-code:project:<id>:vault` to mirror the Hermes plugin's
# `app:hermes:*` discipline and avoid collisions with other tools.
#
# On first call with a legacy bare key, copy its value to the new key. The
# bare key is left in place — deletions in KV are user-only — but the
# new key wins on subsequent reads.
#
# Echoes the resolved value on stdout (empty string if neither key is set).
# ---------------------------------------------------------------------------
memex_kv_namespace_migrate() {
    local _project_id="$1"
    [ -z "$_project_id" ] && return 0

    local _new_key="app:claude-code:project:${_project_id}:vault"
    local _old_key="project:${_project_id}:vault"

    local _new_val
    _new_val=$(_memex_kv_get_value "$_new_key")
    if [ -n "$_new_val" ]; then
        printf '%s' "$_new_val"
        return 0
    fi

    local _old_val
    _old_val=$(_memex_kv_get_value "$_old_key")
    if [ -n "$_old_val" ]; then
        # Forward-migrate. If the write fails (server down, missing perms),
        # the next session will retry; the legacy key remains as a safety
        # net. Log the failure to stderr so it surfaces in CC's hook trace.
        if ! _memex_kv_write_value "$_old_val" "$_new_key"; then
            echo "memex resolve_config: forward-migration of '${_old_key}' → '${_new_key}' failed; continuing with legacy value. Will retry next session." >&2
        fi
        printf '%s' "$_old_val"
        return 0
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Hierarchical active vault resolver
#
# Order (mirrors Hermes' chain at packages/hermes-plugin/src/memex_hermes_plugin/memex/project.py):
#   1. app:claude-code:project:<project_id>:vault   (project binding)
#   2. app:claude-code:user:$USER:vault             (per-user default)
#   3. app:claude-code:agent:<agent_id>:vault       (per-subagent — only if MEMEX_CC_AGENT_ID is set)
#   4. $MEMEX_VAULT environment variable
#   5. server-side default (echoes empty; the server picks the configured default)
#
# In-process result cached in MEMEX_CC_RESOLVED_VAULT to avoid repeated KV
# round-trips within a single hook invocation.
# ---------------------------------------------------------------------------
memex_resolve_active_vault() {
    if [ -n "${MEMEX_CC_RESOLVED_VAULT:-}" ]; then
        printf '%s' "$MEMEX_CC_RESOLVED_VAULT"
        return 0
    fi

    local _project_id
    _project_id=$(memex_resolve_project_id)

    local _vault=""

    # 1. Project-level (with one-time legacy migration)
    if [ -n "$_project_id" ]; then
        _vault=$(memex_kv_namespace_migrate "$_project_id")
    fi

    # 2. User-level
    if [ -z "$_vault" ] && [ -n "${USER:-}" ]; then
        _vault=$(_memex_kv_get_value "app:claude-code:user:${USER}:vault")
    fi

    # 3. Agent-level (only if subagent identity is set)
    if [ -z "$_vault" ] && [ -n "${MEMEX_CC_AGENT_ID:-}" ]; then
        _vault=$(_memex_kv_get_value "app:claude-code:agent:${MEMEX_CC_AGENT_ID}:vault")
    fi

    # 4. Environment override
    if [ -z "$_vault" ] && [ -n "${MEMEX_VAULT:-}" ]; then
        _vault="$MEMEX_VAULT"
    fi

    # 5. Server default — leave empty.

    export MEMEX_CC_RESOLVED_VAULT="$_vault"
    printf '%s' "$_vault"
}

# ---------------------------------------------------------------------------
# Session-note persistence (used by PreCompact and SessionEnd)
#
# memex_persist_session_delta <state_dir> <cc_session_id> <session_note_key>
#                             <vault> <title> <description> <delta_markdown>
#                             [<extra_tag> ...]
#
# Behavior:
#   - First call (state file absent): `memex note add` with full content.
#   - Subsequent calls: `memex note append` with the delta.
#   - State tracked via $state_dir/session_note_created_<safe_session_id>.
#
# Returns 0 on success, non-zero on failure. Caller decides what to surface.
# ---------------------------------------------------------------------------
memex_persist_session_delta() {
    local _state_dir="$1"; shift
    local _cc_session_id="$1"; shift
    local _note_key="$1"; shift
    local _vault="$1"; shift
    local _title="$1"; shift
    local _description="$1"; shift
    local _delta="$1"; shift
    # Remaining args are extra tags.

    if [ -z "$_note_key" ]; then
        return 1
    fi
    if [ -z "$_delta" ]; then
        return 1
    fi

    local _safe_session_id
    _safe_session_id=$(printf '%s' "$_cc_session_id" | tr -c 'A-Za-z0-9._-' '_')
    local _flag_file="${_state_dir}/session_note_created_${_safe_session_id}"

    if [ ! -f "$_flag_file" ]; then
        # First call: create the note.
        local _add_args=(note add "$_delta" --key "$_note_key" --background)
        [ -n "$_vault"       ] && _add_args+=(--vault "$_vault")
        [ -n "$_title"       ] && _add_args+=(--title "$_title")
        [ -n "$_description" ] && _add_args+=(--description "$_description")
        _add_args+=(--author "claude-code")
        for _tag in "$@"; do
            [ -n "$_tag" ] && _add_args+=(--tag "$_tag")
        done

        if memex "${_add_args[@]}" >/dev/null 2>&1; then
            : > "$_flag_file"
            return 0
        fi
        return 1
    fi

    # Subsequent calls: append delta.
    local _append_args=(note append --key "$_note_key" --quiet)
    [ -n "$_vault" ] && _append_args+=(--vault "$_vault")

    if printf '%s' "$_delta" | memex "${_append_args[@]}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}
